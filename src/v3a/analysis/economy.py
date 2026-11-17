"""Generic economy / building analysis on a building-series database (output of run_building_series.py).

Read-only on the source database. Every function takes an open sqlite3 connection to a
building_series.sqlite and returns plain Python rows (list of dicts), so it can be reused from
notebooks, other tools, or run_analysis.py (which writes them to analysis.sqlite + report.html).

    from v3a.analysis import economy as A
    con = A.open_series()                       # latest successful building_series run
    A.country_growth(con, ['gdp', 'population'])
    A.top_countries(con, 10, 'gdp')
    A.query(con, 'SELECT ...')                  # anything else

Definitions (all money in GBP, as the game shows it):
  value added      = goods_sales - goods_cost                     (GBP/week, private buildings only)
  wage bill        = goods_sales - goods_cost - profit_after_reserves    (GBP/week)
  per worker/year  = weekly value x 52 / employed
  margin           = profit_after_reserves / goods_sales
Government buildings have no goods_sales (their cost is in government_dividends) -> economics columns NULL.
"""
import glob, math, os, sqlite3
from collections import defaultdict

from v3a.core.v3data import GAME, OUTPUTS, ROOT

WEEKS_PER_YEAR = 52

COUNTRY_METRICS = [  # (column, label, unit)
    ('gdp', 'GDP', 'GBP'), ('population', 'Population', 'people'), ('gdp_per_capita', 'GDP per capita', 'GBP/person'),
    ('building_levels', 'Building levels', 'levels'), ('money', 'Treasury', 'GBP'),
    ('income', 'Tax income', 'GBP/week'), ('expense', 'Expenses', 'GBP/week'),
    ('credit', 'Credit limit', 'GBP'), ('prestige', 'Prestige score', 'points'),
    ('score', 'Rank position', 'position'), ('states_owned', 'States owned', 'count'),
    ('technologies_known', 'Technologies', 'count')]

# Seven sectors (fixed order = chart colour order). Built from the game's building-group tree.
# 'Services & ownership' = bg_service, bg_urban_facilities (urban centres, arts, owner buildings: manor houses,
# financial districts, company HQs) and bg_power.
SECTORS = ['Subsistence', 'Agriculture', 'Extraction', 'Manufacturing', 'Infrastructure',
           'Services & ownership', 'Government & military']
_TOP_GROUP_SECTOR = {
    'bg_agriculture': 'Agriculture', 'bg_ranching': 'Agriculture', 'bg_plantations': 'Agriculture',
    'bg_extraction': 'Extraction', 'bg_manufacturing': 'Manufacturing', 'bg_infrastructure': 'Infrastructure',
    'bg_service': 'Services & ownership', 'bg_urban_facilities': 'Services & ownership', 'bg_power': 'Services & ownership',
    'bg_government': 'Government & military', 'bg_military': 'Government & military',
    'bg_monuments_hidden': 'Government & military'}   # unique monuments; bg_monuments sits under bg_government
_SUBSISTENCE_GROUPS = {'bg_subsistence_agriculture', 'bg_subsistence_ranching'}


# ------------------------------------------------------------------------------------------- sources
def latest_series(outputs=None, mode='comprehensive', modded=False):
    """Path of the newest successful building_series database of this mode ('comprehensive', 'country' or None = any);
    modded=False: vanilla input only (default), True: modded only, None: any."""
    from v3a.core.runlog import latest_run, outputs_base
    outputs = outputs or outputs_base()

    def ok(d):
        p = os.path.join(d, 'building_series.sqlite')
        if modded is not None and series_input(p)[1] != modded:
            return False
        if not mode:
            return True
        con = sqlite3.connect(p)
        try:
            return series_mode(con)[0] == mode
        finally:
            con.close()
    d = latest_run('building_series', outputs, need='building_series.sqlite', accept=ok)
    if d:
        return os.path.join(d, 'building_series.sqlite')
    found = sorted(glob.glob(os.path.join(outputs, 'extraction', 'building_series', '*', 'building_series.sqlite')),
                   key=os.path.getmtime, reverse=True)
    found = [p for p in found if modded is None or series_input(p)[1] == modded]
    if found:
        return found[0]
    raise FileNotFoundError('no %sbuilding_series.sqlite under %s - run src/v3a/extraction/run_building_series.py first'
                            % ({False: 'vanilla ', True: 'modded '}.get(modded, ''), outputs))


def series_input(path):
    """(input folder, modded, first line of MODDED.txt) of a building-series database; modded = the input folder has
    a MODDED.txt (CLAUDE.md: modded runs are marked so)."""
    con = sqlite3.connect('file:%s?mode=ro' % path.replace('\\', '/'), uri=True)
    try:
        info = dict(con.execute('SELECT key, value FROM db_info').fetchall()) if has_table(con, 'db_info') else {}
    finally:
        con.close()
    inp = info.get('input') or ''
    marker = os.path.join(inp, 'MODDED.txt')
    if inp and os.path.isfile(marker):
        with open(marker, encoding='utf-8-sig') as f:
            return inp, True, f.readline().strip()
    return inp, False, ''


def open_series(path=None):
    """Read-only connection to a building-series database (latest run if path is None)."""
    path = path or latest_series()
    con = sqlite3.connect('file:%s?mode=ro' % path.replace('\\', '/'), uri=True)
    con.row_factory = sqlite3.Row
    return con


def query(con, sql, params=()):
    """Run SQL on the series database -> list of dict rows."""
    return [dict(r) for r in con.execute(sql, params)]


def saves(con):
    """All saves of the series in order: [{save_id, date_iso, year, file}]."""
    return query(con, 'SELECT save_id, date_iso, year, file FROM saves ORDER BY save_id')


def yearly_saves(con):
    """Last save of every in-game year: [{year, save_id, date_iso}]."""
    return query(con, '''SELECT s.year, s.save_id, s.date_iso FROM saves s
        JOIN (SELECT year, MAX(save_id) AS save_id FROM saves GROUP BY year) l USING (save_id) ORDER BY s.year''')


def focus_countries(con):
    """Countries the series is about: its focus_countries table (great + major powers, or one country); older series:
    great powers."""
    if has_table(con, 'focus_countries'):
        return [r[0] for r in con.execute('SELECT tag FROM focus_countries ORDER BY tag')]
    return great_powers(con)


def great_powers(con):
    """Tags that were a great power in at least one save of the series."""
    return [r[0] for r in con.execute('SELECT DISTINCT tag FROM countries WHERE is_great_power = 1 ORDER BY tag')]


# ------------------------------------------------------------------------------------------- sectors
def building_type_map(con, game=GAME):
    """building_type -> {building_group, top_group, sector, category, sub_group, name, sub_group_name, confidence,
    evidence} for every type in the series. category = the game's build-menu category (development / rural / urban),
    sub_group = group right below the top group (e.g. bg_mining), names = the game's English names.

    Source: game/common/buildings (building_group) + game/common/building_groups (parent_group tree).
    Company buildings are generated per company type and not in common/buildings -> mapped by name pattern."""
    src = 'world_building_types' if has_table(con, 'world_building_types') else 'country_building_types'
    types = [r[0] for r in con.execute('SELECT DISTINCT building_type FROM %s ORDER BY 1' % src)]
    group_of, parent, category, loc = {}, {}, {}, {}
    ok = os.path.isdir(os.path.join(game, 'common', 'buildings'))
    if ok:
        from v3a.core.gamefiles import definitions, localisation          # cached per game version
        defs = definitions(game)
        group_of = {k: v.get('building_group') for k, v, _ in defs['buildings'].defs}
        parent = {k: v.get('parent_group') for k, v, _ in defs['building_groups'].defs}
        category = {k: v.get('category') for k, v, _ in defs['building_groups'].defs if v.get('category')}
        loc = localisation(game)
    from v3a.core.gamefiles import game_name
    out = {}
    for t in types:
        g, conf, ev = group_of.get(t), 'high', 'game/common/buildings building_group'
        if g is None and t.startswith('building_regional_company_'):
            g, conf, ev = 'bg_company_regional_headquarter', 'med', 'name pattern building_regional_company_*'
        elif g is None and t.startswith('building_company_'):
            g, conf, ev = 'bg_company_headquarter', 'med', 'name pattern building_company_*'
        chain, x = [], g
        while x and x not in chain:
            chain.append(x); x = parent.get(x)
        top = chain[-1] if chain else None
        if g in _SUBSISTENCE_GROUPS:
            sector = 'Subsistence'
        else:
            sector = _TOP_GROUP_SECTOR.get(top)
        if sector is None:
            sector, conf = 'Services & ownership' if g else 'Unknown', 'low'
            ev += '; no sector rule for top group %s' % top if g else ('game files not found at %s' % game if not ok
                                                                       else 'type not in game files')
        sub = chain[-2] if len(chain) >= 2 else (chain[0] if chain else None)   # group right below the top group
        out[t] = {'building_type': t, 'building_group': g, 'top_group': top, 'sector': sector,
                  'category': next((category[x] for x in chain if x in category), None),
                  'sub_group': sub, 'name': game_name(t, loc), 'sub_group_name': game_name(sub, loc) if sub else None,
                  'confidence': conf, 'evidence': ev}
    return out


LIST_CATEGORIES = ['development', 'agriculture', 'extraction', 'urban', 'other']   # report order


def list_group(t):
    """(category, group label) used to list a building type in the report (owner's choice, 2026-11-17):
    Development (one group, incl. ownership buildings); Agriculture: Subsistence, Plantations, Traditional agriculture
    (staple crops, ranches, vineyards); Extraction: Mining (mines + gold fields), Other extraction (logging, fishing,
    whaling, oil, rubber); Urban: the game's groups, with services and arts merged. t: building_type_map row."""
    cat, top, sub = t.get('category'), t.get('top_group'), t.get('sub_group')
    name = (t.get('sub_group_name') or (sub or 'other').replace('bg_', '').replace('_', ' ')).strip()

    def out(c):   # a group named like its category (vineyards sit directly in bg_agriculture) -> 'Other'
        return c, 'Other' if name.lower() in (c, 'other') else name
    if cat == 'development' or sub == 'bg_owner_buildings':
        return 'development', 'Development'
    if sub in _SUBSISTENCE_GROUPS or t.get('building_group') in _SUBSISTENCE_GROUPS:
        return 'agriculture', 'Subsistence'
    if top == 'bg_plantations':
        return out('agriculture')
    if top in ('bg_agriculture', 'bg_ranching'):           # staple crops, ranches, vineyards
        return 'agriculture', 'Traditional agriculture'
    if top == 'bg_extraction':
        return 'extraction', 'Mining' if sub in ('bg_mining', 'bg_gold_fields') else 'Other extraction'
    if sub in ('bg_service', 'bg_arts'):
        return 'urban', 'Services & arts'
    if cat in LIST_CATEGORIES:
        return out(cat)
    return 'other', 'Other'


def pm_groups(types, game=GAME):
    """{building_type: [(production_method_group, [production methods])]} in the game's order (game/common/buildings
    production_method_groups + game/common/production_method_groups); {} without game files."""
    if not os.path.isdir(os.path.join(game, 'common', 'production_method_groups')):
        return {}
    from v3a.core.gamefiles import definitions
    defs = definitions(game)
    pmg = {k: list(v.get('production_methods') or []) for k, v, _ in defs['production_method_groups'].defs}
    out = {}
    for k, v, _ in defs['buildings'].defs:
        if k in types:
            out[k] = [(g, [p for p in pmg.get(g, []) if isinstance(p, str)])
                      for g in (v.get('production_method_groups') or []) if isinstance(g, str)]
    return out


# ------------------------------------------------------------------------------------------- countries
def top_countries(con, n=20, metric='gdp', save_id=None):
    """Top n country tags by metric in a save (default: last save)."""
    save_id = save_id or con.execute('SELECT MAX(save_id) FROM saves').fetchone()[0]
    return [r[0] for r in con.execute('SELECT tag FROM countries WHERE save_id = ? AND "%s" IS NOT NULL '
                                      'ORDER BY "%s" DESC LIMIT ?' % (metric, metric), (save_id, n))]


def country_series(con, tags=None, metrics=None):
    """{tag: {metric: [value per save in save order, None where the country did not exist]}}."""
    metrics = metrics or [m[0] for m in COUNTRY_METRICS]
    sids = [r[0] for r in con.execute('SELECT save_id FROM saves ORDER BY save_id')]
    pos = {s: i for i, s in enumerate(sids)}
    where, params = '', ()
    if tags:
        where = 'WHERE tag IN (%s)' % ','.join('?' * len(tags)); params = tuple(tags)
    out = {}
    for r in con.execute('SELECT save_id, tag, %s FROM countries %s' % (', '.join('"%s"' % m for m in metrics), where), params):
        d = out.setdefault(r[1], {m: [None] * len(sids) for m in metrics})
        for j, m in enumerate(metrics):
            d[m][pos[r[0]]] = r[2 + j]
    return out


def _years_between(d0, d1):
    y0, m0, dd0 = map(int, d0.split('-')); y1, m1, dd1 = map(int, d1.split('-'))
    return (y1 - y0) + (m1 - m0) / 12 + (dd1 - dd0) / 365


def country_growth(con, metrics=None):
    """Per country x metric: first/last value (first and last save the country exists in), change, growth, CAGR."""
    metrics = metrics or [m[0] for m in COUNTRY_METRICS]
    out = []
    for m in metrics:
        rows = con.execute('''SELECT tag,
              FIRST_VALUE(date_iso) OVER w, FIRST_VALUE("%s") OVER w, LAST_VALUE(date_iso) OVER w, LAST_VALUE("%s") OVER w
            FROM countries WHERE "%s" IS NOT NULL
            WINDOW w AS (PARTITION BY tag ORDER BY save_id ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)''' % (m, m, m))
        seen = set()
        for tag, d0, v0, d1, v1 in rows:
            if tag in seen:
                continue
            seen.add(tag)
            years = _years_between(d0, d1)
            growth = v1 / v0 - 1 if v0 else None
            cagr = (v1 / v0) ** (1 / years) - 1 if v0 and v1 and v0 > 0 and v1 > 0 and years > 0 else None
            out.append({'tag': tag, 'metric': m, 'first_date': d0, 'last_date': d1, 'first_value': v0, 'last_value': v1,
                        'change': v1 - v0 if v0 is not None and v1 is not None else None,
                        'growth': growth, 'cagr': cagr, 'years': round(years, 2)})
    return out


def has_table(con, name):
    """True if the database has this table."""
    return con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def series_mode(con):
    """('comprehensive' | 'country', country tag or None) of a building-series database (older files: comprehensive)."""
    info = dict(con.execute('SELECT key, value FROM db_info').fetchall()) if has_table(con, 'db_info') else {}
    return info.get('mode') or 'comprehensive', info.get('country') or None


def world_series(con):
    """Per save: world totals and great-power share (GP = great power in that save); from the world table if present."""
    if has_table(con, 'world'):
        return query(con, '''SELECT save_id, date_iso, countries, population, gdp, building_levels, money, income,
              gp_gdp, gp_gdp_share, gp_population_share, gdp_per_capita FROM world ORDER BY save_id''')
    return query(con, '''SELECT save_id, date_iso, COUNT(*) AS countries, SUM(population) AS population, SUM(gdp) AS gdp,
          SUM(building_levels) AS building_levels, SUM(money) AS money, SUM(income) AS income,
          SUM(CASE WHEN is_great_power = 1 THEN gdp END) AS gp_gdp,
          SUM(CASE WHEN is_great_power = 1 THEN gdp END) / SUM(gdp) AS gp_gdp_share,
          SUM(CASE WHEN is_great_power = 1 THEN population END) * 1.0 / SUM(population) AS gp_population_share,
          SUM(gdp) / SUM(population) AS gdp_per_capita
        FROM countries GROUP BY save_id, date_iso ORDER BY save_id''')


# ------------------------------------------------------------------------------------------- building types
def sector_series(con, type_map, tags=None):
    """Per save x country x sector: levels, employed, value added (GBP/week). All countries if tags is None."""
    where, params = '', ()
    if tags:
        where = 'WHERE country_tag IN (%s)' % ','.join('?' * len(tags)); params = tuple(tags)
    acc = defaultdict(lambda: [0.0, 0, 0.0])
    dates = {}
    for sid, d, tag, bt, lv, emp, rev, ic in con.execute(
            'SELECT save_id, date_iso, country_tag, building_type, levels, employed, goods_sales, goods_cost '
            'FROM country_building_types %s' % where, params):
        a = acc[(sid, tag, type_map.get(bt, {}).get('sector', 'Unknown'))]
        a[0] += lv or 0; a[1] += emp or 0; a[2] += (rev or 0) - (ic or 0) if rev is not None else 0
        dates[sid] = d
    return [{'save_id': s, 'date_iso': dates[s], 'country_tag': t, 'sector': sec, 'levels': v[0], 'employed': v[1],
             'value_added': v[2]} for (s, t, sec), v in sorted(acc.items())]


def world_sector_series(con, type_map):
    """Per save x sector, all countries (world_building_types): levels, employed, value added; [] for older series."""
    if not has_table(con, 'world_building_types'):
        return []
    acc, dates = defaultdict(lambda: [0.0, 0, 0.0]), {}
    for sid, d, bt, lv, emp, rev, ic in con.execute(
            'SELECT save_id, date_iso, building_type, levels, employed, goods_sales, goods_cost FROM world_building_types'):
        a = acc[(sid, type_map.get(bt, {}).get('sector', 'Unknown'))]
        a[0] += lv or 0; a[1] += emp or 0; a[2] += (rev or 0) - (ic or 0) if rev is not None else 0
        dates[sid] = d
    return [{'save_id': s, 'date_iso': dates[s], 'country_tag': 'WORLD', 'sector': sec, 'levels': v[0], 'employed': v[1],
             'value_added': v[2]} for (s, sec), v in sorted(acc.items())]


def world_type_series(con):
    """Per save x building type, all countries: levels, employed, number of countries that have it."""
    if has_table(con, 'world_building_types'):
        return query(con, '''SELECT save_id, date_iso, building_type, levels, employed, goods_sales, countries
            FROM world_building_types ORDER BY save_id, building_type''')
    return query(con, '''SELECT save_id, date_iso, building_type, SUM(levels) AS levels, SUM(employed) AS employed,
          SUM(goods_sales) AS goods_sales, COUNT(DISTINCT country_tag) AS countries
        FROM country_building_types GROUP BY save_id, date_iso, building_type ORDER BY save_id, building_type''')


def type_growth(con, tags=None):
    """Per country (or 'WORLD') x building type: levels in the first and last save, change, growth."""
    first, last = con.execute('SELECT MIN(save_id), MAX(save_id) FROM saves').fetchone()
    tags = tags or focus_countries(con)
    sql = '''SELECT country_tag AS tag, building_type, SUM(CASE WHEN save_id = ? THEN levels END) AS l0,
               SUM(CASE WHEN save_id = ? THEN levels END) AS l1,
               SUM(CASE WHEN save_id = ? THEN employed END) AS e0, SUM(CASE WHEN save_id = ? THEN employed END) AS e1
             FROM country_building_types WHERE save_id IN (?, ?) AND country_tag IN (%s)
             GROUP BY country_tag, building_type''' % ','.join('?' * len(tags))
    rows = query(con, sql, (first, last, first, last, first, last) + tuple(tags))
    world = 'world_building_types' if has_table(con, 'world_building_types') else 'country_building_types'
    rows += query(con, '''SELECT 'WORLD' AS tag, building_type, SUM(CASE WHEN save_id = ? THEN levels END) AS l0,
               SUM(CASE WHEN save_id = ? THEN levels END) AS l1,
               SUM(CASE WHEN save_id = ? THEN employed END) AS e0, SUM(CASE WHEN save_id = ? THEN employed END) AS e1
             FROM %s WHERE save_id IN (?, ?) GROUP BY building_type''' % world,
                  (first, last, first, last, first, last))
    out = []
    for r in rows:
        l0, l1 = r['l0'] or 0, r['l1'] or 0
        out.append({'tag': r['tag'], 'building_type': r['building_type'], 'first_levels': l0, 'last_levels': l1,
                    'change': l1 - l0, 'growth': l1 / l0 - 1 if l0 else None,
                    'first_employed': r['e0'] or 0, 'last_employed': r['e1'] or 0})
    return out


def type_economics(con, type_map, save_id=None, tags=None):
    """Per country x building type in one save (default last), from individual buildings of the focus countries."""
    save_id = save_id or con.execute('SELECT MAX(save_id) FROM saves').fetchone()[0]
    tags = tags or [r[0] for r in con.execute('SELECT DISTINCT country_tag FROM buildings WHERE save_id = ?', (save_id,))]
    rows = query(con, '''SELECT save_id, date_iso, country_tag, building_type, COUNT(*) AS buildings, SUM(levels) AS levels,
          SUM(staffing) AS staffing, SUM(employed) AS employed,
          SUM(goods_sales) AS revenue_week, SUM(goods_cost) AS input_cost_week, SUM(profit_after_reserves) AS balance_week,
          SUM(CASE WHEN goods_sales IS NOT NULL THEN goods_sales - COALESCE(goods_cost, 0) - COALESCE(profit_after_reserves, 0) END) AS wages_week,
          SUM(government_dividends) AS government_expense_week, SUM(cash_reserves) AS cash_reserves,
          SUM(CASE WHEN goods_sales IS NOT NULL THEN employed END) AS employed_private,
          AVG(throughput) AS avg_throughput
        FROM buildings WHERE save_id = ? AND levels > 0 AND country_tag IN (%s)
        GROUP BY country_tag, building_type''' % ','.join('?' * len(tags)), (save_id,) + tuple(tags))
    for r in rows:
        r['sector'] = type_map.get(r['building_type'], {}).get('sector', 'Unknown')
        rev, ic, emp = r['revenue_week'], r['input_cost_week'] or 0, r['employed_private']
        r['value_added_week'] = rev - ic if rev is not None else None
        r['value_added_per_worker_year'] = (rev - ic) * WEEKS_PER_YEAR / emp if rev is not None and emp else None
        r['wage_per_worker_year'] = r['wages_week'] * WEEKS_PER_YEAR / emp if r['wages_week'] is not None and emp else None
        r['margin'] = r['balance_week'] / rev if rev and r['balance_week'] is not None else None
        r['occupancy'] = r['staffing'] / r['levels'] if r['levels'] and r['staffing'] is not None else None
    return rows


def pm_adoption(con, save_ids=None):
    """Per save x country x building type x production method: levels using it and share of the type's levels.

    Uses individual buildings (focus countries). Default saves: last save of every year."""
    save_ids = save_ids or [r['save_id'] for r in yearly_saves(con)]
    acc, tot, dates = defaultdict(float), defaultdict(float), {}
    q = '''SELECT save_id, date_iso, country_tag, building_type, production_methods, SUM(levels)
           FROM buildings WHERE save_id IN (%s) AND levels > 0
           GROUP BY save_id, country_tag, building_type, production_methods''' % ','.join('?' * len(save_ids))
    for sid, d, tag, bt, pms, lv in con.execute(q, tuple(save_ids)):
        dates[sid] = d
        tot[(sid, tag, bt)] += lv or 0
        for pm in (pms or '').split(' | '):
            if pm:
                acc[(sid, tag, bt, pm)] += lv or 0
    return [{'save_id': s, 'date_iso': dates[s], 'country_tag': t, 'building_type': b, 'production_method': p,
             'levels': v, 'type_levels': tot[(s, t, b)], 'share': v / tot[(s, t, b)] if tot[(s, t, b)] else None}
            for (s, t, b, p), v in sorted(acc.items())]


# ------------------------------------------------------------------------------------------- helpers
def sig(x, n=4):
    """Round to n significant digits (compact JSON); None/NaN -> None."""
    if x is None or isinstance(x, str):
        return x
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    if x == 0:
        return 0
    if isinstance(x, int) and abs(x) < 10 ** n:
        return x
    v = float('%.*g' % (n, x))
    return int(v) if v.is_integer() and abs(v) < 2 ** 53 else v
