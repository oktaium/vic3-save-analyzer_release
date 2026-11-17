"""Run 1 - snapshot: one save -> one general-purpose SQLite database.

usage:
  python src/v3a/extraction/run_snapshot.py                              # latest save (by in-game date) in inputs/observer_run_1
  python src/v3a/extraction/run_snapshot.py --input inputs/other_run     # latest save of another folder
  python src/v3a/extraction/run_snapshot.py --save path/to/file.v3       # a specific save

Output: outputs/extraction/snapshot/<date>_<n>/snapshot.sqlite + logs/ (run.log, run.json, inputs.csv)

Tables
  save_meta          key/value facts about the save (version, date, DLCs, game rules)
  sections           every top-level section of the gamestate: name, size, entries
  countries          one row per existing country (curated variables)
  states, pops       one row per state / per pop with size > 0
  buildings          one row per building with economics and employment
  building_ownership who owns how many levels of each building
  building_goods     input/output goods per building
  facts + fact_paths every scalar field of countries, states and buildings (raw, incl. unnamed tokens)
  tokens, column_docs, db_info   self-documentation
"""
import argparse, os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))   # src/ -> import v3a.*

from v3a.core.v3data import SaveFile, save_info, series, select_tokens, INPUTS
from v3a.core.v3parse import Obj, K, parse, label, top_index
from v3a.extraction import extract as X
from v3a.core.sqlout import DB, Table, tokens_table
from v3a.core.runlog import Run

DEFAULT_INPUT = os.path.join(INPUTS, 'observer_run_1')


def pick_latest(folder):
    """Latest save of a folder by in-game date -> (save_info, number of saves)."""
    infos = series(folder)
    if not infos:
        raise SystemExit('no .v3 saves found in %s' % folder)
    return infos[-1], len(infos)


def write_meta(db, info, meta):
    """save_meta table: file, version, date, sha1, DLCs, game rules."""
    rows = [(k, info.get(k)) for k in ('file', 'game_version', 'game_date', 'date_iso', 'sha1', 'size_bytes')]
    dlcs = meta.get(K.dlcs)
    if isinstance(dlcs, list):
        rows.append(('dlcs', ' | '.join(map(str, dlcs))))
    gr = meta.get(K.game_rules)
    if isinstance(gr, Obj) and isinstance(gr.get(K.settings), list):
        rows.append(('game_rules', ' | '.join(map(str, gr.get(K.settings)))))
    db.write(Table('save_meta', [('key', 'TEXT', '', '', 'Property'), ('value', 'TEXT', '', '', 'Value')],
                   'Facts about the input save.', primary_key=['key']), rows)


def write_sections(db, buf, g):
    """sections table: every top-level section with size and entry count."""
    rows = []
    for k, s, e in top_index(buf):
        v = g.get(k)
        entries = None
        if isinstance(v, Obj) and v.get(K.database) is not None and isinstance(v.get(K.database), (Obj, list)):
            entries = sum(1 for _, x in v.get(K.database) if isinstance(x, Obj)) if isinstance(v.get(K.database), Obj) else len(v.get(K.database))
        rows.append((k, label(k), e - s, 'database' if entries is not None else type(v).__name__, entries))
    db.write(Table('sections', [
        ('raw_key', 'TEXT', '', 'high', 'Section token, k_XXXX'),
        ('name', 'TEXT', '', 'see tokens', "Name from the token dictionary (= raw_key when unknown; '?' = hypothesis)"),
        ('bytes', 'INTEGER', 'bytes', 'high', 'Size inside the decompressed gamestate'),
        ('kind', 'TEXT', '', 'high', "'database' (id = {...} container) or the value type"),
        ('entries', 'INTEGER', 'count', 'high', 'Live entries in a database section')],
        'Top-level sections of the gamestate in file order.'), rows)


def write_facts(db, ix):
    """facts + fact_paths tables: every scalar field of countries, states and buildings (incl. unnamed)."""
    paths, rows = {}, []

    def add(entity_type, eid, block):
        for p, v in X.flatten(block, '', []):
            key = (entity_type, p)
            pid = paths.get(key)
            if pid is None:
                pid = paths[key] = len(paths) + 1
            rows.append((entity_type, eid, pid, v))

    for cid, c in ix.countries.items():
        if c.get(K.is_main_tag):
            add('country', cid, c)
    for sid, s in ix.states.items():
        add('state', sid, s)
    for bid, b in ix.buildings.items():
        add('building', bid, b)
    top = Obj((k, v) for k, v in ix.g if not isinstance(v, (Obj, list)))
    add('gamestate', 0, top)
    db.write(Table('fact_paths', [
        ('path_id', 'INTEGER', 'id', '', 'Path id'),
        ('entity_type', 'TEXT', '', '', 'country / state / building / gamestate'),
        ('raw_path', 'TEXT', '', 'high', "Path of raw keys inside the entity, e.g. k_3171/k_3f17 ([i] = list index, #len = list length)"),
        ('readable_path', 'TEXT', '', 'see tokens', 'Same path with known token names')],
        'Distinct field paths used in facts.', primary_key=['path_id']),
        [(pid, et, p, X.readable_path(p)) for (et, p), pid in paths.items()])
    db.write(Table('facts', [
        ('entity_type', 'TEXT', '', '', 'country / state / building / gamestate'),
        ('entity_id', 'INTEGER', 'id', 'high', 'country_id / state_id / building_id'),
        ('path_id', 'INTEGER', 'id', '', 'See fact_paths'),
        ('value', '', '', 'high', 'Exact value from the save (number, text, or @token)')],
        'Every scalar field of every country, state and building - including fields not yet understood.',
        indexes=[('entity_type', 'entity_id'), ('path_id',)]), rows)
    db.view('v_facts', 'SELECT f.entity_type, f.entity_id, p.readable_path, p.raw_path, f.value '
            'FROM facts f JOIN fact_paths p USING (path_id)', 'facts with readable paths')
    return len(rows)


def main(argv=None):
    """Command line: one save (default: latest) -> snapshot.sqlite."""
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--input', default=DEFAULT_INPUT, help='folder; its latest save is used')
    ap.add_argument('--save', help='a specific .v3 file (overrides --input)')
    a = ap.parse_args(argv)

    with Run('snapshot', 'One save -> general SQLite database',
             settings={'input': a.save or a.input, 'selection': 'given file' if a.save else 'latest in-game date'}) as run:
        with run.step('select save'):
            if a.save:
                info = save_info(a.save)
            else:
                info, n = pick_latest(a.input)
                run.note('Picked the latest of %d saves in %s' % (n, a.input))
        run.add_input(info)
        tf = select_tokens(info['game_version'])
        with run.step('read + decompress'):
            sf = SaveFile(info['path']); buf = sf.gamestate
        with run.step('parse gamestate (%d MB)' % (len(buf) // 2**20)):
            g = parse(buf)
        ix = X.Index(g)
        if not X.goods_names():
            run.warn('Game files not found (set VIC3_GAME): goods are stored by index only')

        db_path = run.path('snapshot.sqlite')
        db = DB(db_path)
        db.info('run_id', run.id); db.info('kind', 'snapshot'); db.info('save', info['file'])
        db.info('game_version', info['game_version']); db.info('game_date', info['game_date'])
        with run.step('tables: meta, sections'):
            write_meta(db, info, sf.meta); write_sections(db, buf, g)
        del buf
        with run.step('tables: countries, states, pops'):
            db.write(Table('countries', X.COUNTRY_COLUMNS, 'One row per existing country.', primary_key=['country_id']),
                     X.country_rows(ix))
            db.write(Table('states', X.STATE_COLUMNS, 'One row per state.', primary_key=['state_id'],
                           indexes=[('owner_tag',)]), X.state_rows(ix))
            db.write(Table('pops', X.POP_COLUMNS, 'One row per pop with size > 0.', primary_key=['pop_id'],
                           indexes=[('state_id',), ('country_tag', 'pop_type')]), X.pop_rows(ix))
        with run.step('tables: buildings, ownership, goods'):
            db.write(Table('buildings', X.BUILDING_COLUMNS, 'One row per building.', primary_key=['building_id'],
                           indexes=[('country_tag', 'building_type'), ('state_id',)]), X.building_rows(ix))
            db.write(Table('building_ownership', X.OWNERSHIP_COLUMNS, 'Ownership shares of buildings.',
                           primary_key=['ownership_id'], indexes=[('building_id',), ('owner_building_id',)]),
                     X.ownership_rows(ix))
            db.write(Table('building_goods', X.GOODS_IO_COLUMNS, 'Input and output goods per building.',
                           indexes=[('building_id',), ('good_index',)]), X.goods_io_rows(ix))
        with run.step('table: facts (all scalar fields)'):
            write_facts(db, ix)
        with run.step('tokens, views, indexes'):
            tokens_table(db, tf)
            db.view('v_great_powers', 'SELECT * FROM countries WHERE is_great_power = 1 ORDER BY score',
                    'Great powers of this save')
            db.view('v_buildings_by_country_type',
                    'SELECT country_tag, building_type, COUNT(*) AS buildings, SUM(levels) AS levels, '
                    'SUM(employed) AS employed, SUM(goods_sales) AS goods_sales, SUM(goods_cost) AS goods_cost, '
                    'SUM(profit_after_reserves) AS profit_after_reserves FROM buildings GROUP BY country_tag, building_type',
                    'Building totals per country and building type')
            counts = db.counts()
            db.close()
        run.add_output(db_path, 'SQLite database', counts)
        run.note('Pop sizes are converted to people (stored /100000 in the save). Money is GBP.')
        run.note('Field names come from %s; columns marked low/med confidence in column_docs are hypotheses.'
                 % os.path.basename(tf or '?'))
    return run


if __name__ == '__main__':
    main()
