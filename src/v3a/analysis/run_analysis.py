"""Analysis run: building-series database -> economy/building analysis tables + interactive HTML report.

usage:
  python src/v3a/analysis/run_analysis.py                     # latest successful vanilla comprehensive building_series run
  python src/v3a/analysis/run_analysis.py --db outputs/extraction/building_series/2026-11-12_18/building_series.sqlite
  python src/v3a/analysis/run_analysis.py --open              # also open report.html in the browser
  python src/v3a/analysis/run_analysis.py --csv               # also export every analysis table as CSV
  python src/v3a/analysis/run_analysis.py --countries 40      # how many countries beyond the focus countries (by GDP)

Output: outputs/analysis/economy_report/<date>_<n>/
  report.html             self-contained interactive report (no internet needed); open it in any browser
  data/analysis.sqlite    all analysis tables, documented in column_docs
  data/csv/*.csv          with --csv
  logs/run.log, run.json, inputs.csv
outputs/analysis/latest_report.html always opens the newest report.
The source database is opened read-only. Analysis functions live in economy.py (reusable).
"""
import argparse, datetime, json, os, re, sys, webbrowser
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))   # src/ -> import v3a.*

from v3a.analysis import economy as A
from v3a.core.sqlout import DB, Table
from v3a.core.runlog import Run
from v3a.core.v3data import sha1_file
from v3a.core.gamefiles import localisation, game_name

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, 'report_template.html')

# ---------------------------------------------------------------------------------------- tables
_ID = [('save_id', 'INTEGER', 'id', '', 'Save in the source series (see source saves table)'),
       ('date_iso', 'TEXT', 'date', 'high', 'In-game date YYYY-MM-DD')]
T_TYPES = Table('building_types', [
    ('building_type', 'TEXT', '', 'high', 'Building type key'),
    ('building_group', 'TEXT', '', 'high', 'building_group from game/common/buildings'),
    ('top_group', 'TEXT', '', 'high', 'Root of the parent_group chain (game/common/building_groups)'),
    ('sector', 'TEXT', '', 'med', 'Analysis sector (7 groups, see analysis.SECTORS); a grouping chosen by this tool'),
    ('category', 'TEXT', '', 'high', "The game's build-menu category of the group chain: development, rural or urban"),
    ('sub_group', 'TEXT', '', 'high', 'Building group right below the top group (e.g. bg_mining); used to list types like the game'),
    ('name', 'TEXT', '', 'high', "The game's English name of the building type (localisation)"),
    ('sub_group_name', 'TEXT', '', 'high', "The game's English name of sub_group"),
    ('confidence', 'TEXT', '', '', 'Confidence of the group/sector assignment'),
    ('evidence', 'TEXT', '', '', 'Where the assignment comes from')],
    'Building type -> game building group -> analysis sector.', primary_key=['building_type'])
T_WORLD = Table('world_series', _ID + [
    ('countries', 'INTEGER', 'count', 'high', 'Existing countries'),
    ('population', 'INTEGER', 'people', 'high', 'World population'),
    ('gdp', 'REAL', 'GBP', 'high', 'Sum of country GDP'),
    ('building_levels', 'REAL', 'levels', 'med', 'Sum of building levels'),
    ('money', 'REAL', 'GBP', 'med', 'Sum of treasuries'),
    ('income', 'REAL', 'GBP/week', 'high', 'Sum of tax income'),
    ('gp_gdp', 'REAL', 'GBP', 'high', 'GDP of countries that are great powers in this save'),
    ('gp_gdp_share', 'REAL', 'share', 'high', 'gp_gdp / world gdp'),
    ('gp_population_share', 'REAL', 'share', 'high', 'Great-power population / world population'),
    ('gdp_per_capita', 'REAL', 'GBP/person', 'high', 'World GDP / world population')],
    'World totals per save.', primary_key=['save_id'])
T_GROWTH = Table('country_growth', [
    ('tag', 'TEXT', '', 'high', 'Country tag'), ('metric', 'TEXT', '', '', 'Column of the source countries table'),
    ('first_date', 'TEXT', 'date', 'high', 'First save with a value'), ('last_date', 'TEXT', 'date', 'high', 'Last save with a value'),
    ('first_value', 'REAL', 'metric unit', '', 'Value in the first save (confidence = that of the metric)'),
    ('last_value', 'REAL', 'metric unit', '', 'Value in the last save'),
    ('change', 'REAL', 'metric unit', '', 'last - first'), ('growth', 'REAL', 'share', '', 'last / first - 1'),
    ('cagr', 'REAL', 'share/year', '', 'Compound annual growth rate between first and last save'),
    ('years', 'REAL', 'years', 'high', 'In-game years between first and last save')],
    'Growth of every country metric between its first and last save.', primary_key=['tag', 'metric'])
T_SECTOR = Table('country_sector_series', _ID + [
    ('country_tag', 'TEXT', '', 'high', 'Country (state owner)'), ('sector', 'TEXT', '', 'med', 'See building_types'),
    ('levels', 'REAL', 'levels', 'med', 'Building levels in the sector'),
    ('employed', 'INTEGER', 'people', 'high', 'Workers in the sector'),
    ('value_added', 'REAL', 'GBP/week', 'high', 'Revenue - input cost (government buildings count 0)')],
    'Every country x sector per save (from country_building_types).', primary_key=['save_id', 'country_tag', 'sector'],
    indexes=[('country_tag', 'sector', 'save_id')])
T_WTYPE = Table('world_type_series', _ID + [
    ('building_type', 'TEXT', '', 'high', 'Building type'), ('levels', 'REAL', 'levels', 'med', 'World levels'),
    ('employed', 'INTEGER', 'people', 'high', 'World workers'), ('goods_sales', 'REAL', 'GBP/week', 'high', 'World goods_sales'),
    ('countries', 'INTEGER', 'count', 'high', 'Countries with at least one such building')],
    'World totals per building type and save.', primary_key=['save_id', 'building_type'])
T_TGROWTH = Table('type_growth', [
    ('tag', 'TEXT', '', 'high', "Great power tag or 'WORLD'"), ('building_type', 'TEXT', '', 'high', 'Building type'),
    ('first_levels', 'REAL', 'levels', 'med', 'Levels in the first save'), ('last_levels', 'REAL', 'levels', 'med', 'Levels in the last save'),
    ('change', 'REAL', 'levels', 'med', 'last - first'), ('growth', 'REAL', 'share', 'med', 'last / first - 1 (NULL if first = 0)'),
    ('first_employed', 'INTEGER', 'people', 'high', 'Workers in the first save'),
    ('last_employed', 'INTEGER', 'people', 'high', 'Workers in the last save')],
    'Building-type growth between first and last save for every focus country and the world.',
    primary_key=['tag', 'building_type'])
T_ECON = Table('type_economics', _ID + [
    ('country_tag', 'TEXT', '', 'high', 'Focus country'), ('building_type', 'TEXT', '', 'high', 'Building type'),
    ('sector', 'TEXT', '', 'med', 'See building_types'), ('buildings', 'INTEGER', 'count', 'high', 'Building objects (levels > 0)'),
    ('levels', 'REAL', 'levels', 'med', 'Levels'), ('staffing', 'REAL', 'levels', 'high', 'Staffed levels'),
    ('occupancy', 'REAL', 'share', 'high', 'staffing / levels'),
    ('employed', 'INTEGER', 'people', 'high', 'Workers'), ('employed_private', 'INTEGER', 'people', 'high', 'Workers in buildings with goods_sales'),
    ('revenue_week', 'REAL', 'GBP/week', 'high', 'Revenue'), ('input_cost_week', 'REAL', 'GBP/week', 'high', 'Input cost'),
    ('wages_week', 'REAL', 'GBP/week', 'med', 'Derived: goods_sales - input cost - weekly balance'),
    ('balance_week', 'REAL', 'GBP/week', 'high', 'Weekly balance (profit after wages)'),
    ('government_expense_week', 'REAL', 'GBP/week', 'med', 'Cost of government buildings'),
    ('cash_reserves', 'REAL', 'GBP', 'high', 'Cash reserves'), ('avg_throughput', 'REAL', 'multiplier', 'high', 'Mean throughput of the buildings'),
    ('value_added_week', 'REAL', 'GBP/week', 'high', 'goods_sales - input cost'),
    ('value_added_per_worker_year', 'REAL', 'GBP/person/year', 'med', 'value added x 52 / employed_private'),
    ('wage_per_worker_year', 'REAL', 'GBP/person/year', 'med', 'wages x 52 / employed_private'),
    ('margin', 'REAL', 'share', 'high', 'balance / goods_sales')],
    'Economics per focus country x building type in the last save.', primary_key=['country_tag', 'building_type'])
T_PM = Table('pm_adoption', _ID + [
    ('country_tag', 'TEXT', '', 'high', 'Focus country'), ('building_type', 'TEXT', '', 'high', 'Building type'),
    ('production_method', 'TEXT', '', 'high', 'Active production method'),
    ('levels', 'REAL', 'levels', 'med', 'Levels of buildings using this method'),
    ('type_levels', 'REAL', 'levels', 'med', 'All levels of this type in the country'),
    ('share', 'REAL', 'share', 'med', 'levels / type_levels')],
    'Production-method adoption at the last save of every in-game year.',
    primary_key=['save_id', 'country_tag', 'building_type', 'production_method'])
TABLES = [T_TYPES, T_WORLD, T_GROWTH, T_SECTOR, T_WTYPE, T_TGROWTH, T_ECON, T_PM]


def nice(key):
    """Readable label from a game key: 'building_textile_mill' -> 'Textile mill'."""
    for p in ('building_', 'pm_'):
        if key.startswith(p):
            key = key[len(p):]
    return key.replace('_', ' ').capitalize()


# ---------------------------------------------------------------------------------------- analysis
def analyse(con, run=None):
    """All analysis tables of one series: {table name: rows, 'type_map': {...}}; steps are timed in run if given."""
    from contextlib import nullcontext
    step = run.step if run else (lambda name: nullcontext())
    res = {}
    with step('building types -> sectors (game files)'):
        res['type_map'] = A.building_type_map(con)
        low = [t for t, v in res['type_map'].items() if v['confidence'] == 'low']
        if low and run:
            run.warn('%d building type(s) without a reliable sector: %s' % (len(low), ', '.join(low[:10])))
    steps = [('world_series', lambda: A.world_series(con)),
             ('country_growth', lambda: A.country_growth(con)),
             ('country_sector_series', lambda: A.sector_series(con, res['type_map'])),
             ('world_type_series', lambda: A.world_type_series(con)),
             ('type_growth', lambda: A.type_growth(con)),
             ('type_economics', lambda: A.type_economics(con, res['type_map'])),
             ('pm_adoption', lambda: A.pm_adoption(con))]
    for name, f in steps:
        with step(name):
            res[name] = f()
    return res


# ---------------------------------------------------------------------------------------- report data
def report_data(con, run_id, src, res, n_countries, extra_tags=()):
    """Collect every chart and table of the report into one JSON-able dict (extra_tags: more countries to include)."""
    S = A.sig
    sv = A.saves(con)
    dates = [s['date_iso'] for s in sv]
    pos = {s['save_id']: i for i, s in enumerate(sv)}
    n = len(sv)
    gps = A.great_powers(con)
    focus = A.focus_countries(con)
    first, last = sv[0]['save_id'], sv[-1]['save_id']
    tags = list(dict.fromkeys(focus + A.top_countries(con, n_countries, 'gdp', last)
                              + A.top_countries(con, n_countries, 'gdp', first) + list(extra_tags)))
    metrics = [m[0] for m in A.COUNTRY_METRICS]
    cs = A.country_series(con, tags, metrics)
    countries = {t: {'gp': t in gps, 's': {m: [S(v) for v in cs[t][m]] for m in metrics}} for t in tags if t in cs}

    world = {k: [None] * n for k in ('gdp', 'population', 'building_levels', 'gp_gdp_share', 'countries', 'gdp_per_capita')}
    for r in res['world_series']:
        for k in world:
            world[k][pos[r['save_id']]] = S(r[k])

    sectors = A.SECTORS
    sidx = {s: i for i, s in enumerate(sectors)}
    sec = {}
    world_rows = A.world_sector_series(con, res['type_map'])     # all countries; older series: sum of country rows
    for r in res['country_sector_series'] + world_rows:
        if r['sector'] not in sidx:
            continue
        for tag in ((r['country_tag'],) if world_rows else (r['country_tag'], 'WORLD')):
            if tag != 'WORLD' and tag not in countries:
                continue
            d = sec.setdefault(tag, {k: [[0] * n for _ in sectors] for k in ('levels', 'employed', 'value_added')})
            for k in d:
                d[k][sidx[r['sector']]][pos[r['save_id']]] += r[k] or 0
    sec = {t: {k: [[S(x) for x in arr] for arr in v] for k, v in d.items()} for t, d in sec.items()}

    tmap = res['type_map']
    tl = {'WORLD': {}}
    for r in res['world_type_series']:
        tl['WORLD'].setdefault(r['building_type'], [0] * n)[pos[r['save_id']]] = S(r['levels'])
    for sid, tag, bt, lv in con.execute('SELECT save_id, country_tag, building_type, levels FROM country_building_types '
                                        'WHERE country_tag IN (%s)' % ','.join('?' * len(focus)), focus):
        tl.setdefault(tag, {}).setdefault(bt, [0] * n)[pos[sid]] = S(lv)

    pm = {}
    ys = A.yearly_saves(con)
    ypos = {y['save_id']: i for i, y in enumerate(ys)}
    for r in res['pm_adoption']:
        d = pm.setdefault(r['country_tag'], {}).setdefault(r['building_type'], {})
        d.setdefault(r['production_method'], [None] * len(ys))[ypos[r['save_id']]] = S(r['share'], 3)
    for d in (b for t in pm.values() for b in t.values()):   # a method not used in a year the type exists -> 0
        present = [any(v[i] is not None for v in d.values()) for i in range(len(ys))]
        for v in d.values():
            for i, p in enumerate(present):
                if p and v[i] is None:
                    v[i] = 0

    # production-method groups of every type with PM data, in the game's order (report: groups A, B, C ...)
    pm_types = {b for t in pm.values() for b in t}
    loc = localisation(A.GAME)                       # {} without game files -> readable key as name
    gname = lambda k, d: game_name(k, loc, d)       # noqa: E731
    pmg = A.pm_groups(pm_types)
    pm_groups = {}
    for bt in pm_types:
        groups = [[g, gname(g, nice(g.replace('pmg_', '').split('_building_')[0])), ms] for g, ms in pmg.get(bt, [])]
        seen = {m for _, _, ms in groups for m in ms}
        used = sorted({p for t in pm.values() for p in t.get(bt, {})} - seen)
        if used:   # methods in the saves but in no group of the game files (or no game files): one extra group
            groups.append(['other', 'Other methods' if groups else 'All methods', used])
        pm_groups[bt] = groups
    pm_labels = {m: gname(m, nice(m)) for g in pm_groups.values() for _, _, ms in g for m in ms}

    econ_cols = ['country_tag', 'building_type', 'sector', 'levels', 'occupancy', 'employed', 'revenue_week',
                 'value_added_week', 'value_added_per_worker_year', 'wage_per_worker_year', 'balance_week', 'margin',
                 'avg_throughput']
    return {
        'meta': {'run_id': run_id, 'source': src, 'source_run': '/'.join(os.path.normpath(os.path.dirname(src)).split(os.sep)[-3:]),
                 'created': datetime.datetime.now().isoformat(timespec='minutes'), 'saves': n,
                 'first_date': dates[0], 'last_date': dates[-1], 'great_powers': gps, 'focus_countries': focus,
                 'mode': A.series_mode(con)[0], 'country': A.series_mode(con)[1],
                 'game_version': con.execute('SELECT MAX(game_version) FROM saves').fetchone()[0]},
        'dates': dates, 'years': [y['date_iso'] for y in ys],
        'metrics': [list(m) for m in A.COUNTRY_METRICS],
        'countries': countries, 'world': world, 'sectors': sectors, 'sector': sec,
        'types': {t: dict(zip(('category', 'group_label'), A.list_group(v)), label=v.get('name') or nice(t), sector=v['sector'])
                  for t, v in tmap.items()},
        'list_categories': A.LIST_CATEGORIES,
        'type_levels': tl,
        'type_growth': [[r['tag'], r['building_type'], S(r['first_levels']), S(r['last_levels']), S(r['change']),
                         S(r['growth'], 3), S(r['last_employed'])] for r in res['type_growth']],
        'growth': [[r['tag'], r['metric'], S(r['first_value']), S(r['last_value']), S(r['growth'], 3), S(r['cagr'], 3)]
                   for r in res['country_growth'] if r['tag'] in countries],
        'econ_cols': econ_cols, 'econ': [[S(r[c]) if c not in ('margin', 'occupancy') else S(r[c], 3) for c in econ_cols]
                                         for r in res['type_economics']],
        'pm': pm, 'pm_labels': pm_labels, 'pm_groups': pm_groups,
    }


def _hide_paths(obj):
    """Copy of the report data where absolute file paths keep only their last 3 parts (no user/folder names in a shared report)."""
    if isinstance(obj, dict):
        return {k: _hide_paths(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_hide_paths(v) for v in obj]
    if isinstance(obj, str) and re.match(r'^([A-Za-z]:[\\/]|/|\\\\)', obj):
        return '.../' + '/'.join(re.split(r'[\\/]+', obj.rstrip('/\\'))[-3:])
    return obj


def write_html(path, data):
    """Fill report_template.html with the report data -> self-contained report.html (absolute paths shortened)."""
    data = _hide_paths(data)
    with open(TEMPLATE, encoding='utf-8') as f:
        html = f.read()
    blob = json.dumps(data, separators=(',', ':')).replace('</', '<\\/')
    html = html.replace('/*__DATA__*/null', blob, 1)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(html)


# ---------------------------------------------------------------------------------------- main
def main(argv=None):
    """Command line: latest (or given) building series -> analysis.sqlite + report.html in a run folder."""
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--db', help='building_series.sqlite (default: latest successful vanilla comprehensive building_series run)')
    ap.add_argument('--countries', type=int, default=30, help='countries beyond the focus countries: top N by GDP (first and last save)')
    ap.add_argument('--csv', action='store_true', help='also export every analysis table as CSV')
    ap.add_argument('--open', action='store_true', help='open report.html in the default browser')
    a = ap.parse_args(argv)
    src = os.path.abspath(a.db or A.latest_series())

    with Run('economy_report', 'Building series -> economy/building analysis + HTML report',
             settings={'source_db': src, 'countries': a.countries, 'csv': a.csv, 'game_dir': A.GAME}) as run:
        con = A.open_series(src)
        sv = A.saves(con)
        with run.step('hash source database'):
            run.add_input({'file': os.path.basename(src), 'path': src, 'size_bytes': os.path.getsize(src),
                           'sha1': sha1_file(src), 'date_iso': sv[0]['date_iso'],
                           'game_date': '%s -> %s (%d saves)' % (sv[0]['date_iso'], sv[-1]['date_iso'], len(sv)),
                           'game_version': con.execute('SELECT MAX(game_version) FROM saves').fetchone()[0]})
        res = analyse(con, run)

        db_path = run.path('data', 'analysis.sqlite')
        with run.step('write analysis.sqlite'):
            db = DB(db_path)
            db.info('run_id', run.id); db.info('kind', 'economy_report'); db.info('source_db', src)
            for t in TABLES:
                rows = list(res['type_map'].values()) if t is T_TYPES else res[t.name]
                db.write(t, rows)
            counts = db.counts()
            db.close()
        run.add_output(db_path, 'Analysis tables (SQLite)', counts)

        if a.csv:
            import csv
            with run.step('CSV export'):
                for t in TABLES:
                    p = run.path('data', 'csv', t.name + '.csv')
                    rows = list(res['type_map'].values()) if t is T_TYPES else res[t.name]
                    with open(p, 'w', newline='', encoding='utf-8') as f:
                        w = csv.DictWriter(f, t.names, extrasaction='ignore'); w.writeheader(); w.writerows(rows)
                    run.add_output(p, 'CSV export of ' + t.name)

        html_path = run.path('report.html')
        with run.step('report.html'):
            write_html(html_path, report_data(con, run.id, src, res, a.countries))
        run.add_output(html_path, 'Interactive report - open in a browser', main=True)
        run.note('Source: %s (%d saves, %s -> %s)' % (src, len(sv), sv[0]['date_iso'], sv[-1]['date_iso']))
        mode, country = A.series_mode(con)
        run.note('Series mode: %s%s' % (mode, ' (%s; world values from the world tables)' % country if country else ''))
        run.note('Sectors are this tool\'s grouping of the game\'s building groups (see building_types).')
        run.note('Value added = goods_sales - input cost; wages derived as goods_sales - input cost - weekly balance.')
        con.close()
    print('report:', html_path)
    if a.open:
        webbrowser.open('file:///' + html_path.replace('\\', '/'))
    return run


if __name__ == '__main__':
    main()
