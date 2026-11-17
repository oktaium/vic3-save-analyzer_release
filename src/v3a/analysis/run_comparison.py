"""Run comparison: two building-series databases (e.g. vanilla vs a modded AI) -> one HTML report + comparison.sqlite.

Both series are cut to the in-game dates they share (same save dates), analysed exactly like run_analysis.py, and
shown in the economy report with a View switch: A, B, or A vs B (B drawn dashed / light next to A).

usage:
  python src/v3a/analysis/run_comparison.py                   # A = latest vanilla series, B = latest modded series
  python src/v3a/analysis/run_comparison.py --a outputs/extraction/building_series/2026-11-15_5/building_series.sqlite \
                                            --b outputs/extraction/building_series/2026-11-15_8/building_series.sqlite
  python src/v3a/analysis/run_comparison.py --until 1856-02-01 --open     # cut both at this date, open the report
  python src/v3a/analysis/run_comparison.py --label-a Vanilla --label-b "Kuromi's AI"

Default series: newest successful comprehensive building_series run whose input folder has (B) or lacks (A) a
MODDED.txt. Comparing two series of the same kind needs --a/--b.

Output: outputs/analysis/run_comparison/<date>_<n>/
  report.html               the economy report with the A / B / A vs B switch (self-contained)
  data/comparison.sqlite    compare_runs, compare_world, compare_countries, compare_types (documented in column_docs)
  logs/run.log, run.json, inputs.csv
"""
import argparse, os, sqlite3, sys, webbrowser
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))   # src/ -> import v3a.*

from v3a.analysis import economy as A
from v3a.analysis.run_analysis import analyse, report_data, write_html
from v3a.core.sqlout import DB, Table
from v3a.core.runlog import Run
from v3a.core.v3data import sha1_file

T_RUNS = Table('compare_runs', [
    ('run', 'TEXT', '', 'high', "'A' or 'B'"), ('label', 'TEXT', '', '', 'Name shown in the report'),
    ('input', 'TEXT', 'path', 'high', 'Save folder the series was extracted from'),
    ('modded', 'INTEGER', 'bool', 'high', '1 if the input folder has a MODDED.txt'),
    ('modded_note', 'TEXT', '', 'high', 'First line of MODDED.txt'),
    ('source_db', 'TEXT', 'path', 'high', 'building_series.sqlite that was compared'),
    ('source_sha1', 'TEXT', '', 'high', 'sha1 of source_db'),
    ('saves', 'INTEGER', 'count', 'high', 'Saves used (dates both series share)'),
    ('first_date', 'TEXT', 'date', 'high', 'First save used'), ('last_date', 'TEXT', 'date', 'high', 'Last save used')],
    'The two compared series.', primary_key=['run'])
T_WORLD = Table('compare_world', [
    ('date_iso', 'TEXT', 'date', 'high', 'In-game date (same in both series)')]
    + [(k + s, 'REAL', u, c, d + (' in A' if s == '_a' else ' in B'))
       for k, u, c, d in [('gdp', 'GBP', 'high', 'World GDP'), ('population', 'people', 'high', 'World population'),
                          ('building_levels', 'levels', 'med', 'World building levels'),
                          ('gp_gdp_share', 'share', 'high', 'Great-power share of world GDP')] for s in ('_a', '_b')],
    'World totals per date, side by side.', primary_key=['date_iso'])
T_COUNTRIES = Table('compare_countries', [
    ('tag', 'TEXT', '', 'high', 'Country tag'), ('metric', 'TEXT', '', '', 'Column of the source countries table'),
    ('date_iso', 'TEXT', 'date', 'high', 'Last shared date'),
    ('value_a', 'REAL', 'metric unit', '', 'Value in A at date_iso (confidence = that of the metric)'),
    ('value_b', 'REAL', 'metric unit', '', 'Value in B at date_iso'),
    ('diff', 'REAL', 'metric unit', '', 'value_b - value_a'), ('ratio', 'REAL', 'share', '', 'value_b / value_a - 1'),
    ('growth_a', 'REAL', 'share', '', 'A: last / first shared save - 1'),
    ('growth_b', 'REAL', 'share', '', 'B: last / first shared save - 1')],
    'Every country x metric at the last shared date, A vs B.', primary_key=['tag', 'metric'])
T_TYPES = Table('compare_types', [
    ('tag', 'TEXT', '', 'high', "Focus country or 'WORLD'"), ('building_type', 'TEXT', '', 'high', 'Building type'),
    ('levels_a', 'REAL', 'levels', 'med', 'Levels in A at the last shared date'),
    ('levels_b', 'REAL', 'levels', 'med', 'Levels in B at the last shared date'),
    ('diff', 'REAL', 'levels', 'med', 'levels_b - levels_a'),
    ('employed_a', 'INTEGER', 'people', 'high', 'Workers in A'), ('employed_b', 'INTEGER', 'people', 'high', 'Workers in B')],
    'Building levels per country (focus countries of either run) x type at the last shared date.',
    primary_key=['tag', 'building_type'])
TABLES = [T_RUNS, T_WORLD, T_COUNTRIES, T_TYPES]


def series_info(path):
    """{input, name (input folder), modded, modded_note} of a building_series.sqlite."""
    inp, modded, note = A.series_input(path)
    return {'input': inp, 'name': os.path.basename(inp.rstrip('\\/')) or os.path.basename(os.path.dirname(path)),
            'modded': modded, 'modded_note': note}


def latest_by_kind(modded):
    """Newest successful comprehensive building_series.sqlite with a modded (True) or vanilla (False) input."""
    try:
        return A.latest_series(mode='comprehensive', modded=modded)
    except FileNotFoundError:
        raise SystemExit('no %s comprehensive building_series run found; pass --%s'
                         % ('modded' if modded else 'vanilla', 'b' if modded else 'a'))


def save_dates(path):
    """In-game dates of all saves of a series database."""
    con = sqlite3.connect('file:%s?mode=ro' % path.replace('\\', '/'), uri=True)
    try:
        return [r[0] for r in con.execute('SELECT date_iso FROM saves ORDER BY save_id')]
    finally:
        con.close()


def load_window(path, dates):
    """In-memory copy of a series database holding only the saves on these dates (source stays untouched)."""
    src = sqlite3.connect('file:%s?mode=ro' % path.replace('\\', '/'), uri=True)
    con = sqlite3.connect(':memory:')
    src.backup(con)
    src.close()
    con.execute('CREATE TEMP TABLE keep (d TEXT PRIMARY KEY)')
    con.executemany('INSERT INTO keep VALUES (?)', [(d,) for d in dates])
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
        if any(c[1] == 'date_iso' for c in con.execute('PRAGMA table_info("%s")' % t)):
            con.execute('DELETE FROM "%s" WHERE date_iso NOT IN (SELECT d FROM keep)' % t)
    con.commit()
    con.row_factory = sqlite3.Row
    return con


def compare_tables(con_a, con_b, res_a, res_b, focus):
    """Rows of compare_world, compare_countries, compare_types."""
    wb = {r['date_iso']: r for r in res_b['world_series']}
    world = [{'date_iso': r['date_iso'], **{k + '_a': r[k] for k in ('gdp', 'population', 'building_levels', 'gp_gdp_share')},
              **{k + '_b': (wb.get(r['date_iso']) or {}).get(k) for k in ('gdp', 'population', 'building_levels', 'gp_gdp_share')}}
             for r in res_a['world_series']]
    metrics = [m[0] for m in A.COUNTRY_METRICS]
    sa, sb = A.country_series(con_a, None, metrics), A.country_series(con_b, None, metrics)
    last_date = A.saves(con_a)[-1]['date_iso']

    def growth(v):
        f = next((x for x in v if x is not None), None)
        return v[-1] / f - 1 if f and v[-1] is not None else None
    countries = []
    for tag in sorted(set(sa) & set(sb)):
        for m in metrics:
            a, b = sa[tag][m][-1], sb[tag][m][-1]
            if a is None and b is None:
                continue
            countries.append({'tag': tag, 'metric': m, 'date_iso': last_date, 'value_a': a, 'value_b': b,
                              'diff': b - a if a is not None and b is not None else None,
                              'ratio': b / a - 1 if a and b is not None else None,
                              'growth_a': growth(sa[tag][m]), 'growth_b': growth(sb[tag][m])})
    types = {}
    for side, con, res in (('a', con_a, res_a), ('b', con_b, res_b)):
        last = con.execute('SELECT MAX(save_id) FROM saves').fetchone()[0]
        q = ('SELECT country_tag, building_type, levels, employed FROM country_building_types WHERE save_id = ? AND '
             'country_tag IN (%s)' % ','.join('?' * len(focus)), (last,) + tuple(focus))
        rows = list(con.execute(*q))
        rows += [('WORLD', r['building_type'], r['levels'], r['employed']) for r in res['world_type_series']
                 if r['save_id'] == last]
        for tag, bt, lv, emp in rows:
            d = types.setdefault((tag, bt), {'tag': tag, 'building_type': bt, 'levels_a': 0, 'levels_b': 0,
                                             'employed_a': 0, 'employed_b': 0})
            d['levels_' + side] += lv or 0
            d['employed_' + side] += emp or 0
    for d in types.values():
        d['diff'] = d['levels_b'] - d['levels_a']
    return world, countries, sorted(types.values(), key=lambda d: (d['tag'], d['building_type']))


def main(argv=None):
    """Command line: two building series -> comparison.sqlite + report.html (A / B / A vs B) in a run folder."""
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--a', help='building_series.sqlite of run A (default: latest vanilla comprehensive series)')
    ap.add_argument('--b', help='building_series.sqlite of run B (default: latest modded comprehensive series)')
    ap.add_argument('--label-a', help='name of run A in the report (default: its input folder, + "vanilla")')
    ap.add_argument('--label-b', help='name of run B in the report (default: its input folder, + "modded")')
    ap.add_argument('--until', help='last in-game date to use, YYYY-MM-DD (default: last date both series share)')
    ap.add_argument('--countries', type=int, default=30, help='countries beyond the focus countries: top N by GDP')
    ap.add_argument('--open', action='store_true', help='open report.html in the default browser')
    a = ap.parse_args(argv)
    paths = [os.path.abspath(a.a or latest_by_kind(False)), os.path.abspath(a.b or latest_by_kind(True))]
    infos = [series_info(p) for p in paths]
    labels = [a.label_a, a.label_b]
    for i, inf in enumerate(infos):
        labels[i] = labels[i] or '%s (%s)' % (inf['name'], 'modded' if inf['modded'] else 'vanilla')

    dates_b = set(save_dates(paths[1]))
    common = [d for d in save_dates(paths[0]) if d in dates_b and (not a.until or d <= a.until)]
    if len(common) < 2:
        raise SystemExit('the two series share %d save date(s); need at least 2' % len(common))

    with Run('run_comparison', 'Two building series (A vs B) -> comparison report',
             settings={'a': paths[0], 'b': paths[1], 'label_a': labels[0], 'label_b': labels[1], 'until': a.until,
                       'shared_dates': '%s -> %s (%d saves)' % (common[0], common[-1], len(common)),
                       'countries': a.countries}) as run:
        with run.step('hash sources'):
            for p, inf in zip(paths, infos):
                run.add_input({'file': os.path.basename(p), 'path': p, 'size_bytes': os.path.getsize(p),
                               'sha1': sha1_file(p), 'date_iso': common[0],
                               'game_date': '%s -> %s (%d shared saves)' % (common[0], common[-1], len(common)),
                               'note': inf['modded_note'] or 'vanilla'})
        with run.step('load shared dates'):
            cons = [load_window(p, common) for p in paths]
        res = []
        for side, con in zip('AB', cons):
            with run.step('analyse ' + side):
                res.append(analyse(con))
        focus = sorted(set(A.focus_countries(cons[0])) | set(A.focus_countries(cons[1])))
        gps = sorted(set(A.great_powers(cons[0])) | set(A.great_powers(cons[1])))
        with run.step('comparison tables'):
            world, countries, types = compare_tables(cons[0], cons[1], res[0], res[1], focus)
        runs = [{'run': side, 'label': labels[i], 'input': infos[i]['input'], 'modded': int(infos[i]['modded']),
                 'modded_note': infos[i]['modded_note'], 'source_db': paths[i], 'source_sha1': run.inputs[i]['sha1'],
                 'saves': len(common), 'first_date': common[0], 'last_date': common[-1]} for i, side in enumerate('AB')]

        db_path = run.path('data', 'comparison.sqlite')
        with run.step('write comparison.sqlite'):
            db = DB(db_path)
            db.info('run_id', run.id); db.info('kind', 'run_comparison')
            for t, rows in zip(TABLES, (runs, world, countries, types)):
                db.write(t, rows)
            counts = db.counts()
            db.close()
        run.add_output(db_path, 'Comparison tables (SQLite)', counts)

        html_path = run.path('report.html')
        with run.step('report.html'):
            data = [report_data(cons[i], run.id, paths[i], res[i], a.countries, extra_tags=focus + gps) for i in range(2)]
            data[0]['cmp'] = {'labels': labels, 'runs': runs, 'B': data[1]}
            write_html(html_path, data[0])
        run.add_output(html_path, 'Comparison report (View: A / B / A vs B) - open in a browser', main=True)
        for r in runs:
            run.note('%s = %s: %s%s' % (r['run'], r['label'], r['source_db'], ' [MODDED: %s]' % r['modded_note'] if r['modded'] else ''))
        run.note('Shared dates: %s -> %s, %d saves; saves on other dates are left out.' % (common[0], common[-1], len(common)))
        for c in cons:
            c.close()
    print('report:', html_path)
    if a.open:
        webbrowser.open('file:///' + html_path.replace('\\', '/'))
    return run


if __name__ == '__main__':
    main()
