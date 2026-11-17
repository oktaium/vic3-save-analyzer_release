"""Run 2 - building series: a series of saves -> countries and buildings over time (one SQLite file).

usage:
  python src/v3a/extraction/run_building_series.py                          # asks for the mode (Enter = comprehensive)
  python src/v3a/extraction/run_building_series.py --mode comprehensive     # standard: great + major powers, their buildings, world totals
  python src/v3a/extraction/run_building_series.py --country GBR            # one country in full detail + global values
  python src/v3a/extraction/run_building_series.py --truncated              # first + every 12th (one per year) + last save
  python src/v3a/extraction/run_building_series.py --save path/to/one.v3    # a single save (quick test)
  python src/v3a/extraction/run_building_series.py --input inputs/other_run --workers 3
  python src/v3a/extraction/run_building_series.py --every 10 --limit 5     # any other sample
  python src/v3a/extraction/run_building_series.py --dates-from outputs/extraction/building_series/<run>/building_series.sqlite
                                                    # only saves on the in-game dates of another run (series db or save folder),
                                                    # e.g. monthly vanilla saves matched to a yearly modded run for run_comparison

Without --mode/--country the tool asks (in a terminal) for the mode, the country tag and truncated y/N; when it
cannot ask (no terminal) it runs comprehensive. Test small first (truncated or one save), then run the full series.

Output: outputs/extraction/building_series/<date>_<n>/building_series.sqlite + logs/ (run.log, run.json, inputs.csv)

Modes
  comprehensive  focus = countries ranked great_power, major_power or unrecognized_major_power in at least one save
                 (great powers and the tier below), followed through all saves: countries, buildings and
                 country_building_types hold these only; world / world_building_types still sum over ALL countries.
  country TAG    countries, states, pops, buildings, building_ownership, building_goods of that one country (all curated
                 columns), country_building_types for it; plus the global values below. Much smaller and faster to read.
Both modes: buildings are counted where they stand (state owner), not by who owns the shares. Skipped everywhere:
unique buildings (monuments such as Vatican City, canals), company headquarters, special buildings, and buildings
with 0 levels (not built yet); country buildings / building_levels count the kept buildings only.

Tables
  saves                    one row per save (date, file, sha1)
  countries                comprehensive: the focus countries, series columns; country mode: that country, all columns
  buildings                buildings of the focus countries per save (levels, employment, economics, PMs)
  country_building_types   per save x focus country x building type totals
  world                    per save, global values: countries, world population/GDP/levels/employment, GP shares
  world_building_types     per save x building type, all countries: levels, employment, goods sales, countries
  focus_countries          the countries kept (great powers + major powers ever, or the chosen tag)
  states, pops, building_ownership, building_goods   country mode only
Views (growth = change vs previous save / vs first save of the series)
  v_country_growth         focus countries: GDP, population, building levels, employment, goods_sales + growth
  v_type_growth            focus countries x building type: count, levels, employment, goods_sales + growth
  v_type_yearly            same, last save of each in-game year, with year-on-year growth
  v_world_share            focus countries' share of world building levels per type and save
"""
import argparse, os, sys, time
from collections import defaultdict
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))   # src/ -> import v3a.*

from v3a.core.v3data import SaveFile, series, find_saves, save_info, pick_saves, select_tokens, INPUTS
from v3a.core.v3parse import parse
from v3a.extraction import extract as X
from v3a.core.sqlout import DB, Table, tokens_table
from v3a.core.runlog import Run, available_memory_gb, peak_memory_mb

DEFAULT_INPUT = os.path.join(INPUTS, 'observer_run_1')
GB_PER_WORKER = 2.0         # measured peak ~1.5 GB per parsed save, plus margin

SERIES_COUNTRY_COLS = ['country_id', 'tag', 'is_great_power', 'rank', 'score', 'prestige', 'population',
                       'gdp', 'gdp_per_capita', 'credit', 'money', 'income', 'expense',
                       'states_owned', 'buildings', 'building_levels', 'technologies_known']
BUILDING_COLS = ['building_id', 'country_id', 'country_tag', 'building_type', 'state_id', 'region', 'levels',
                 'last_updated_level', 'staffing', 'employed', 'throughput', 'goods_sales', 'goods_cost',
                 'profit_after_reserves', 'salary_rate', 'cash_reserves', 'government_dividends', 'production_methods']
FOCUS_RANKS = ('great_power', 'major_power', 'unrecognized_major_power')   # comprehensive mode keeps these (ever)
SKIP_GROUPS = {'bg_monuments', 'bg_monuments_hidden', 'bg_canals', 'bg_company_headquarter',
               'bg_company_regional_headquarter', 'bg_cossack_host'}
SKIP_PREFIXES = ('building_company_', 'building_regional_company_')   # per-company headquarter types
TYPE_SUMS = ['levels', 'staffing', 'employed', 'goods_sales', 'goods_cost', 'profit_after_reserves', 'cash_reserves']

WORLD_COLUMNS = [
    ('countries', 'INTEGER', 'count', 'high', 'Existing countries'),
    ('great_powers', 'TEXT', '', 'high', "Tags ranked 'great_power' in this save, space separated"),
    ('population', 'INTEGER', 'people', 'high', 'World population (sum over countries)'),
    ('gdp', 'REAL', 'GBP', 'high', 'Sum of country GDP'),
    ('gdp_per_capita', 'REAL', 'GBP/person', 'med', 'gdp / population'),
    ('building_levels', 'REAL', 'levels', 'med', 'Sum of building levels in all countries (kept building types)'),
    ('money', 'REAL', 'GBP', 'med', 'Sum of treasuries'),
    ('income', 'REAL', 'GBP/week', 'high', 'Sum of country income (last week)'),
    ('gp_gdp', 'REAL', 'GBP', 'high', 'GDP of the great powers of this save'),
    ('gp_gdp_share', 'REAL', 'share', 'high', 'gp_gdp / gdp'),
    ('gp_population_share', 'REAL', 'share', 'high', 'Great powers\' share of world population'),
    ('buildings', 'INTEGER', 'count', 'high', 'Kept building objects in states with an owner'),
    ('employed', 'INTEGER', 'people', 'high', 'Workers in all buildings'),
    ('goods_sales', 'REAL', 'GBP/week', 'high', 'Sum of building goods_sales'),
    ('states', 'INTEGER', 'count', 'high', 'States (all owners)'),
    ('pops', 'INTEGER', 'count', 'high', 'Pop objects with size > 0'),
]
WORLD_TYPE_COLUMNS = [('building_type', 'TEXT', '', 'high', 'Building type'),
                      ('buildings', 'INTEGER', 'count', 'high', 'Building objects with levels > 0'),
                      ('levels', 'REAL', 'levels', 'med', 'Sum of levels'), ('staffing', 'REAL', 'levels', 'high', 'Sum of staffing'),
                      ('employed', 'INTEGER', 'people', 'high', 'Workers'),
                      ('goods_sales', 'REAL', 'GBP/week', 'high', 'Sum of goods_sales'),
                      ('goods_cost', 'REAL', 'GBP/week', 'high', 'Sum of goods_cost'),
                      ('profit_after_reserves', 'REAL', 'GBP/week', 'high', 'Sum of profit_after_reserves'),
                      ('cash_reserves', 'REAL', 'GBP', 'high', 'Sum of cash reserves'),
                      ('countries', 'INTEGER', 'count', 'high', 'Countries that have this type')]


# ------------------------------------------------------------------------------------------ worker
_SKIP = None


def skipped_types():
    """Building types not recorded: unique ones and SKIP_GROUPS in common/buildings (cached per process)."""
    global _SKIP
    if _SKIP is None:
        from v3a.core.gamefiles import definitions
        try:
            defs = definitions().get('buildings')
        except OSError:                                   # no game files: only the prefix rule applies
            defs = None
        _SKIP = {k for k, v, _ in (defs.defs if defs else [])
                 if v.get('unique') == 'yes' or v.get('building_group') in SKIP_GROUPS}
    return _SKIP


def keep_building(r):
    """True for a building worth recording: built (levels > 0), not unique / headquarter / special."""
    bt = r['building_type'] or ''
    return (r['levels'] or 0) > 0 and not bt.startswith(SKIP_PREFIXES) and bt not in skipped_types()


def world_rows(ix, crows, brows):
    """Global values of one save (one dict) and world totals per building type."""
    S, _ = ix.pop_agg
    pop = sum(r['population'] or 0 for r in crows)
    gdp = sum(r['gdp'] or 0 for r in crows)
    gps = [r for r in crows if r['is_great_power']]
    gp_gdp = sum(r['gdp'] or 0 for r in gps)
    w = {'countries': len(crows), 'great_powers': ' '.join(sorted(r['tag'] for r in gps)), 'population': pop, 'gdp': gdp,
         'gdp_per_capita': gdp / pop if pop else None, 'building_levels': sum(r['building_levels'] or 0 for r in crows),
         'money': sum(r['money'] or 0 for r in crows), 'income': sum(r['income'] or 0 for r in crows),
         'gp_gdp': gp_gdp, 'gp_gdp_share': gp_gdp / gdp if gdp else None,
         'gp_population_share': sum(r['population'] or 0 for r in gps) / pop if pop else None,
         'buildings': len(brows), 'employed': sum(r['employed'] or 0 for r in brows),
         'goods_sales': sum(r['goods_sales'] or 0 for r in brows), 'states': len(ix.states),
         'pops': int(sum(a.get('pops', 0) for a in S.values()))}
    types = defaultdict(lambda: dict({k: 0.0 for k in TYPE_SUMS}, buildings=0, tags=set()))
    for r in brows:
        if (r['levels'] or 0) <= 0:
            continue
        t = types[r['building_type']]
        t['buildings'] += 1; t['tags'].add(r['country_tag'])
        for k in TYPE_SUMS:
            t[k] += r[k] or 0
    wt = [dict({k: v for k, v in t.items() if k != 'tags'}, building_type=bt, countries=len(t['tags']))
          for bt, t in sorted(types.items())]
    return w, wt


def extract_one(info, country=None):
    """Worker: parse one save, return small row lists (runs in a separate process when workers > 1).
    country=None: comprehensive rows; country='GBR': all curated rows of that country (+ world values in both)."""
    t0 = time.perf_counter()
    select_tokens(info['game_version'])
    g = parse(SaveFile(info['path']).gamestate)
    t_parse = time.perf_counter() - t0
    ix = X.Index(g)
    crows = X.country_rows(ix)
    brows = [{k: r.get(k) for k in BUILDING_COLS} for r in X.building_rows(ix)
             if r['country_id'] is not None and keep_building(r)]
    per = defaultdict(lambda: [0, 0.0])
    for r in brows:
        per[r['country_id']][0] += 1; per[r['country_id']][1] += r['levels'] or 0
    for r in crows:                                       # totals over the kept buildings only
        r['buildings'], r['building_levels'] = per[r['country_id']]
    world, world_types = world_rows(ix, crows, brows)
    out = {'info': info, 'world': world, 'world_types': world_types, 'date_check': ix.date}
    if country is None:
        out['countries'] = [{k: r[k] for k in SERIES_COUNTRY_COLS} for r in crows]
        out['buildings'] = brows                          # trimmed to the focus countries after all saves are in
    else:
        cids = {r['country_id'] for r in crows if r['tag'] == country}
        out['countries'] = [r for r in crows if r['tag'] == country]
        out['buildings'] = [r for r in brows if r['country_tag'] == country]
        out['states'] = [r for r in X.state_rows(ix) if r['owner_id'] in cids]
        out['pops'] = [r for r in X.pop_rows(ix) if r['country_tag'] == country]
        bids = {r['building_id'] for r in out['buildings']}
        out['building_ownership'] = [r for r in X.ownership_rows(ix)
                                     if r['building_id'] in bids or r['owner_country_tag'] == country]
        out['building_goods'] = [r for r in X.goods_io_rows(ix) if r['building_id'] in bids]
    out.update(parse_s=round(t_parse, 1), total_s=round(time.perf_counter() - t0, 1), peak_mb=peak_memory_mb())
    return out


# ------------------------------------------------------------------------------------------ tables
KEY = [('save_id', 'INTEGER', 'id', '', 'See saves'), ('date_iso', 'TEXT', 'date', 'high', 'In-game date YYYY-MM-DD')]


def tables(country=None):
    """Table specs {name: Table} for the series database (country mode adds the detail tables)."""
    doc = {c[0]: c for c in X.COUNTRY_COLUMNS + X.BUILDING_COLUMNS}
    T = {'saves': Table('saves', [
        ('save_id', 'INTEGER', 'id', '', 'Order in the series (1 = earliest in-game date)'),
        ('date_iso', 'TEXT', 'date', 'high', 'In-game date YYYY-MM-DD'),
        ('game_date', 'TEXT', 'date', 'high', 'In-game date as the game writes it'),
        ('year', 'INTEGER', 'year', 'high', 'In-game year'),
        ('file', 'TEXT', '', '', 'Save file name'), ('sha1', 'TEXT', '', '', 'File hash'),
        ('game_version', 'TEXT', '', 'high', 'Game version')], 'Saves in this series.', primary_key=['save_id'])}
    ccols = X.COUNTRY_COLUMNS if country else [doc[c] for c in SERIES_COUNTRY_COLS]
    T['countries'] = Table('countries', KEY + list(ccols),
                           'Country %s in every save (all columns).' % country if country else 'Every existing country in every save.',
                           primary_key=['save_id', 'country_id'], indexes=[('tag', 'save_id')])
    bcols = [doc[c] if c != 'country_id' else ('country_id', 'INTEGER', 'id', 'high', 'Owner of the state') for c in BUILDING_COLS]
    T['buildings'] = Table('buildings', KEY + bcols,
                           'Built buildings (levels > 0, no monuments / headquarters) of %s in every save.' % (country or 'the focus countries'),
                           primary_key=['save_id', 'building_id'], indexes=[('country_tag', 'building_type', 'save_id')])
    T['world'] = Table('world', KEY + WORLD_COLUMNS, 'Global values per save (all countries).', primary_key=['save_id'])
    T['world_building_types'] = Table('world_building_types', KEY + WORLD_TYPE_COLUMNS,
                                      'World totals per save x building type (all countries).',
                                      primary_key=['save_id', 'building_type'])
    if country:
        T['states'] = Table('states', KEY + X.STATE_COLUMNS, 'States of %s per save.' % country,
                            primary_key=['save_id', 'state_id'])
        T['pops'] = Table('pops', KEY + X.POP_COLUMNS, 'Pops (size > 0) in states of %s per save.' % country,
                          primary_key=['save_id', 'pop_id'], indexes=[('state_id', 'save_id')])
        T['building_ownership'] = Table('building_ownership', KEY + X.OWNERSHIP_COLUMNS,
                                        'Ownership shares of %s buildings, and shares owned from %s.' % (country, country),
                                        primary_key=['save_id', 'ownership_id'])
        T['building_goods'] = Table('building_goods', KEY + X.GOODS_IO_COLUMNS,
                                    'Input and output goods of %s buildings per save.' % country,
                                    indexes=[('building_id', 'save_id')])
    return T


VIEWS = [
    ('v_country_growth', '''
WITH t AS (SELECT save_id, country_tag, SUM(employed) AS employed, SUM(goods_sales) AS goods_sales, SUM(goods_cost) AS goods_cost,
             SUM(profit_after_reserves) AS profit_after_reserves FROM buildings GROUP BY save_id, country_tag),
c AS (SELECT c.save_id, c.date_iso, c.tag, c.is_great_power, c.score, c.prestige, c.population, c.gdp,
             c.gdp_per_capita, c.money, c.buildings, c.building_levels, t.employed, t.goods_sales, t.goods_cost,
             t.profit_after_reserves
      FROM countries c JOIN focus_countries f ON f.tag = c.tag LEFT JOIN t ON t.save_id = c.save_id AND t.country_tag = c.tag)
SELECT *,
  gdp - LAG(gdp) OVER w AS gdp_change_prev,
  gdp / FIRST_VALUE(gdp) OVER w - 1 AS gdp_growth_since_first,
  population / CAST(FIRST_VALUE(population) OVER w AS REAL) - 1 AS population_growth_since_first,
  building_levels - LAG(building_levels) OVER w AS levels_change_prev,
  building_levels - FIRST_VALUE(building_levels) OVER w AS levels_change_since_first,
  building_levels / FIRST_VALUE(building_levels) OVER w - 1 AS levels_growth_since_first,
  employed - FIRST_VALUE(employed) OVER w AS employed_change_since_first
FROM c WINDOW w AS (PARTITION BY tag ORDER BY save_id)''',
     'Focus countries: economy and building totals per save, with growth vs previous and first save'),
    ('v_type_growth', '''
WITH s AS (SELECT t.* FROM country_building_types t JOIN focus_countries f ON f.tag = t.country_tag)
SELECT *,
  levels - LAG(levels) OVER w AS levels_change_prev,
  levels - FIRST_VALUE(levels) OVER w AS levels_change_since_first,
  employed - LAG(employed) OVER w AS employed_change_prev,
  goods_sales - LAG(goods_sales) OVER w AS goods_sales_change_prev,
  CASE WHEN employed > 0 THEN (COALESCE(goods_sales,0) - COALESCE(goods_cost,0)) * 52.0 / employed END AS productivity_per_worker_year
FROM s WINDOW w AS (PARTITION BY country_tag, building_type ORDER BY save_id)''',
     'Focus countries x building type per save with growth (types absent in a save have no row there)'),
    ('v_type_yearly', '''
WITH last AS (SELECT year, MAX(save_id) AS save_id FROM saves GROUP BY year),
s AS (SELECT l.year, t.* FROM country_building_types t JOIN last l USING (save_id) JOIN focus_countries f ON f.tag = t.country_tag)
SELECT *,
  levels - LAG(levels) OVER w AS levels_change_yoy,
  levels / LAG(levels) OVER w - 1 AS levels_growth_yoy,
  employed - LAG(employed) OVER w AS employed_change_yoy
FROM s WINDOW w AS (PARTITION BY country_tag, building_type ORDER BY year)''',
     'Focus countries x building type, last save of each in-game year, year-on-year growth'),
    ('v_world_share', '''
SELECT t.save_id, t.date_iso, t.country_tag, t.building_type, t.levels, w.levels AS world_levels,
       t.levels / w.levels AS share_of_world_levels
FROM country_building_types t JOIN focus_countries f ON f.tag = t.country_tag
JOIN world_building_types w USING (save_id, building_type)''',
     'Share of world building levels held by each focus country, per type and save'),
]


# ------------------------------------------------------------------------------------------ main
def ask(a):
    """Fill mode / country / truncated interactively when they were not given (only in a terminal)."""
    if a.country or a.mode or not sys.stdin.isatty():
        return
    try:
        _ask(a)
    except EOFError:                     # no real terminal (on Windows NUL looks like one): keep the defaults
        print('(no answer: comprehensive)')
        a.country = None


def _ask(a):
    """The questions of ask()."""
    print('Building series mode:\n  1  comprehensive (standard): great and major powers + world totals\n'
          '  2  one country: full detail of one country + global values')
    if input('Choose 1 or 2 [1]: ').strip() == '2':
        while not a.country:
            a.country = input('Country tag (e.g. GBR): ').strip().upper() or None
    if not (a.truncated or a.save or a.every > 1 or a.limit):
        a.truncated = input('Truncated input (first + every 12th + last save) [y/N]: ').strip().lower().startswith('y')


def dates_of(path):
    """In-game dates (YYYY-MM-DD) of another run: its building_series.sqlite (saves table) or its folder of saves."""
    if os.path.isdir(path):
        return {i['date_iso'] for i in series(path)}
    import sqlite3
    con = sqlite3.connect('file:%s?mode=ro' % os.path.abspath(path).replace(os.sep, '/'), uri=True)
    try:
        return {r[0] for r in con.execute('SELECT date_iso FROM saves')}
    finally:
        con.close()


def main(argv=None):
    """Command line: a series of saves -> building_series.sqlite (comprehensive or one country, parallel workers, views)."""
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--input', default=DEFAULT_INPUT, help='folder with the series of saves')
    ap.add_argument('--mode', choices=['comprehensive', 'country'], help='comprehensive (standard) or country (needs --country)')
    ap.add_argument('--country', help='country tag for country mode, e.g. GBR (implies --mode country)')
    ap.add_argument('--truncated', action='store_true', help='first save + every 12th (one per in-game year) + last save')
    ap.add_argument('--save', help='use only this one save file')
    ap.add_argument('--workers', type=int, default=0, help='parallel processes (0 = auto from free RAM)')
    ap.add_argument('--every', type=int, default=1, help='use every k-th save (sampling)')
    ap.add_argument('--limit', type=int, default=0, help='use at most N saves (after --every)')
    ap.add_argument('--dates-from', help='keep only saves on the in-game dates of another run: a building_series.sqlite '
                                         'or a folder of saves (replaces --truncated/--every)')
    a = ap.parse_args(argv)
    ask(a)
    if a.mode == 'country' and not a.country:
        ap.error('--mode country needs --country TAG')
    country = a.country.upper() if a.country else None
    mode = 'country' if country else 'comprehensive'

    free = available_memory_gb()
    workers = a.workers or max(1, min(4, (os.cpu_count() or 2) - 1, int((free or 2) // GB_PER_WORKER)))
    with Run('building_series', 'Series of saves -> %s over time' % ('%s in full detail' % country if country
                                                                     else 'great/major powers and world totals'),
             settings={'input': a.save or a.input, 'mode': mode, 'country': country, 'truncated': a.truncated,
                       'workers': workers, 'every': a.every, 'limit': a.limit or 'all', 'dates_from': a.dates_from,
                       'free_memory_gb_at_start': round(free, 1) if free else None}) as run:
        with run.step('list saves, read dates, hash files'):
            if a.save:
                infos, total = [save_info(a.save)], 1
            else:
                infos = series(a.input)
                total = len(infos)
                files = len(find_saves(a.input))
                if files > total:
                    run.note('%d identical save file(s) skipped (same sha1)' % (files - total))
                if a.dates_from:
                    want = dates_of(a.dates_from)
                    infos = [i for i in infos if i['date_iso'] in want]
                    if a.limit:
                        infos = infos[:a.limit]
                    missing = sorted(want - {i['date_iso'] for i in infos})
                    if missing and not a.limit:
                        run.warn('%d date(s) of %s have no save here: %s' % (len(missing), a.dates_from, ', '.join(missing[:10])))
                else:
                    infos = pick_saves(infos, every=a.every, limit=a.limit, truncated=a.truncated)
        if not infos:
            raise SystemExit('no .v3 saves found in %s' % a.input + (' on the dates of %s' % a.dates_from if a.dates_from else ''))
        if len(infos) < total:
            run.note('Sample: %d of %d saves used (%s)' % (len(infos), total, 'dates of %s' % a.dates_from if a.dates_from else
                                                           'truncated: first + every 12th + last' if a.truncated
                                                           else '--every %d --limit %s' % (a.every, a.limit or '-')))
        workers = min(workers, len(infos))
        versions = {i['game_version'] for i in infos}
        if len(versions) > 1:
            run.warn('Series mixes game versions %s; token names follow each save\'s own version' % sorted(versions))
        for i in infos:
            run.add_input(i)
        tf = select_tokens(infos[-1]['game_version'])

        db_path = run.path('building_series.sqlite')
        db = DB(db_path)
        db.info('run_id', run.id); db.info('kind', 'building_series'); db.info('input', a.save or a.input)
        db.info('mode', mode); db.info('country', country or '')
        T = tables(country)
        for t in T.values():
            db.create(t)
        sid = {i['sha1']: n + 1 for n, i in enumerate(infos)}
        stats = []

        def store(res):
            info = res['info']; s = sid[info['sha1']]; d = info['date_iso']
            db.insert(T['saves'], [{'save_id': s, 'date_iso': d, 'game_date': info['game_date'], 'year': int(d[:4]),
                                    'file': info['file'], 'sha1': info['sha1'], 'game_version': info['game_version']}])
            db.insert(T['world'], [dict(res['world'], save_id=s, date_iso=d)])
            db.insert(T['world_building_types'], [dict(r, save_id=s, date_iso=d) for r in res['world_types']])
            for name in ('countries', 'buildings', 'states', 'pops', 'building_ownership', 'building_goods'):
                if name in res:
                    db.insert(T[name], [dict(r, save_id=s, date_iso=d) for r in res[name]])
            db.con.commit()
            stats.append(res)
            print('  [%d/%d] %s  %s  parse %.0fs total %.0fs  %d buildings'
                  % (len(stats), len(infos), info['file'], info['game_date'], res['parse_s'], res['total_s'],
                     len(res['buildings'])))

        with run.step('parse + extract %d saves (%d workers)' % (len(infos), workers)):
            if workers == 1:
                for i in infos:
                    store(extract_one(i, country))
            else:
                import multiprocessing as mp
                from concurrent.futures import ProcessPoolExecutor, as_completed
                with ProcessPoolExecutor(workers, mp_context=mp.get_context('spawn')) as ex:
                    futs = [ex.submit(extract_one, i, country) for i in infos]
                    for f in as_completed(futs):
                        store(f.result())

        with run.step('focus countries, summaries, views'):
            c = db.con
            if country:
                focus = [country] if c.execute('SELECT 1 FROM countries LIMIT 1').fetchone() else []
                if not focus:
                    raise SystemExit('country %s not found in any of the %d saves' % (country, len(infos)))
            else:
                focus = [r[0] for r in c.execute('SELECT DISTINCT tag FROM countries WHERE rank IN (%s) ORDER BY tag'
                                                 % ','.join('?' * len(FOCUS_RANKS)), FOCUS_RANKS)]
                q = ','.join('?' * len(focus))
                c.execute('DELETE FROM countries WHERE tag NOT IN (%s)' % q, focus)
                c.execute('DELETE FROM buildings WHERE country_tag NOT IN (%s)' % q, focus)
            c.execute('DROP TABLE IF EXISTS country_building_types')
            c.execute('''CREATE TABLE country_building_types AS
                SELECT save_id, date_iso, country_tag, building_type, COUNT(*) AS buildings, SUM(levels) AS levels,
                       SUM(staffing) AS staffing, SUM(employed) AS employed, SUM(goods_sales) AS goods_sales,
                       SUM(goods_cost) AS goods_cost, SUM(profit_after_reserves) AS profit_after_reserves,
                       SUM(cash_reserves) AS cash_reserves
                FROM buildings WHERE levels > 0 GROUP BY save_id, date_iso, country_tag, building_type''')
            c.execute('CREATE INDEX ix_cbt ON country_building_types (country_tag, building_type, save_id)')
            docs = [('save_id', 'INTEGER', 'id', '', 'See saves'), ('date_iso', 'TEXT', 'date', 'high', 'In-game date'),
                    ('country_tag', 'TEXT', '', 'high', 'Country (state owner)')] + WORLD_TYPE_COLUMNS[:-1]
            c.execute("INSERT OR REPLACE INTO column_docs VALUES ('country_building_types','*','TABLE','','',?)",
                      ('Totals per save x country x building type (%s; kept buildings only).' % (country or 'focus countries'),))
            c.executemany("INSERT OR REPLACE INTO column_docs VALUES ('country_building_types',?,?,?,?,?)", docs)
            focus_t = Table('focus_countries', [('tag', 'TEXT', '', 'high', 'Country tag')],
                            'Countries kept in countries, buildings and the views: %s.'
                            % ('the chosen country' if country else 'rank %s in at least one save' % ' / '.join(FOCUS_RANKS)),
                            primary_key=['tag'])
            db.write(focus_t, [{'tag': t} for t in focus])
            for name, sql, desc in VIEWS:
                db.view(name, sql, desc)
            tokens_table(db, tf)
            db.info('focus_countries', ' '.join(focus))
            run.note('%s: %s' % ('Country' if country else 'Focus countries (%d; %s ever)' % (len(focus), ' / '.join(FOCUS_RANKS)),
                                 ', '.join(focus)))
            run.note('Skipped building types: unique (monuments, canals), headquarters, special; buildings with 0 levels.')
            counts = db.counts()
            db.close(vacuum=True)

        run.add_output(db_path, 'SQLite database', counts)
        ps = [r['parse_s'] for r in stats]; ts = [r['total_s'] for r in stats]
        run.settings['per_save_parse_s_avg'] = round(sum(ps) / len(ps), 1)
        run.settings['per_save_total_s_avg'] = round(sum(ts) / len(ts), 1)
        run.settings['worker_peak_memory_mb_max'] = max((r['peak_mb'] or 0) for r in stats) or None
        run.note('Growth columns in views compare with the previous save and with the first save of the series.')
        run.note('Buildings are attributed to the owner of the state they stand in.')
    return run


if __name__ == '__main__':
    main()
