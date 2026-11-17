"""Victoria 3 save comparison: folders of saves -> one HTML report.

usage:
  python compare.py saves/run_A saves/run_B              # two runs -> comparison report (A vs B)
  python compare.py saves/run_A                          # one run  -> report of that run
  python compare.py saves/run_A saves/run_B --label-a Vanilla --label-b "My mod"
  python compare.py saves/run_A saves/run_B --limit 3    # quick test: first 3 saves of each run
  python compare.py saves/run_A saves/run_B --open       # open the report in the browser when done
  python compare.py ... --game "D:/Steam/steamapps/common/Victoria 3/game"   # game not in the default Steam folder

Input:  one folder of .v3 saves per run (e.g. saves/run_A/). Yearly or monthly autosaves both work.
Output: reports/<A>_vs_<B>.html (or reports/<A>.html): a self-contained report, open it in any browser.
        Detailed data (SQLite databases, logs) for every step is kept in outputs/.
A run that was already read with the same saves is reused, so comparing A with C later does not read A again.
"""
import argparse, glob, os, re, shutil, sqlite3, sys, time, webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
REPORTS = os.path.join(HERE, 'reports')
DEFAULT_GAME = r'C:/Program Files (x86)/Steam/steamapps/common/Victoria 3/game'


def slug(text):
    """File-name safe version of a label."""
    return re.sub(r'[^A-Za-z0-9_.-]+', '_', text).strip('_') or 'run'


def save_count(folder):
    """Number of .v3 files in a folder (sub folders included)."""
    return sum(f.lower().endswith('.v3') for _, _, fs in os.walk(folder) for f in fs)


def reusable_series(folder, limit):
    """Path of an earlier complete extraction of exactly these saves (same folder, same files), else None."""
    from v3a.core.v3data import series
    from v3a.core.runlog import outputs_base
    if limit:
        return None
    want = {i['sha1'] for i in series(folder)}
    found = glob.glob(os.path.join(outputs_base(), 'extraction', 'building_series', '*', 'building_series.sqlite'))
    for p in sorted(found, key=os.path.getmtime, reverse=True):
        try:
            con = sqlite3.connect('file:%s?mode=ro' % p.replace('\\', '/'), uri=True)
            info = dict(con.execute('SELECT key, value FROM db_info'))
            have = {r[0] for r in con.execute('SELECT sha1 FROM saves')}
            con.execute('SELECT 1 FROM focus_countries LIMIT 1')          # written last: extraction finished
            con.close()
        except sqlite3.Error:
            continue
        if (info.get('mode') == 'comprehensive' and os.path.normcase(info.get('input') or '') == os.path.normcase(folder)
                and have == want):
            return p
    return None


def extract(folder, limit, workers):
    """Read every save of a folder -> building_series.sqlite (reused if already done). Returns its path."""
    from v3a.extraction import run_building_series
    old = reusable_series(folder, limit)
    if old:
        print('  already read before, reusing %s' % old)
        return old
    args = ['--mode', 'comprehensive', '--input', folder]
    if limit:
        args += ['--limit', str(limit)]
    if workers:
        args += ['--workers', str(workers)]
    run = run_building_series.main(args)
    return run.path('building_series.sqlite')


def main(argv=None):
    """Command line: one or two save folders -> reports/<name>.html."""
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split('\n', 2)[2])
    ap.add_argument('run_a', help='folder with the saves of run A')
    ap.add_argument('run_b', nargs='?', help='folder with the saves of run B (leave out for a one-run report)')
    ap.add_argument('--label-a', help='name of run A in the report (default: folder name)')
    ap.add_argument('--label-b', help='name of run B in the report (default: folder name)')
    ap.add_argument('--limit', type=int, default=0, help='quick test: use only the first N saves of each run')
    ap.add_argument('--workers', type=int, default=0, help='saves read in parallel (default: from free memory, ~2 GB each)')
    ap.add_argument('--game', help='Victoria 3 "game" folder (for building names; default: Steam location)')
    ap.add_argument('--open', action='store_true', help='open the report in the browser when done')
    a = ap.parse_args(argv)

    # settings must be in place before the analyzer modules are imported
    game = a.game or os.environ.get('VIC3_GAME') or DEFAULT_GAME
    os.environ['VIC3_GAME'] = game
    sys.path.insert(0, os.path.join(HERE, 'src'))
    if not os.path.isdir(game):
        print('NOTE: game folder not found (%s).\n      The report still works, but shows internal building keys instead of '
              'the game\'s names.\n      Use --game "<your Victoria 3 folder>/game" to fix this.\n' % game)

    folders = [os.path.abspath(f) for f in (a.run_a, a.run_b) if f]
    for f in folders:
        if not os.path.isdir(f):
            raise SystemExit('folder not found: %s' % f)
        if not save_count(f):
            raise SystemExit('no .v3 save files in %s' % f)
    labels = [a.label_a or os.path.basename(folders[0])]
    if len(folders) > 1:
        labels.append(a.label_b or os.path.basename(folders[1]))
        if labels[0] == labels[1]:
            labels = [labels[0] + ' (A)', labels[1] + ' (B)']

    t0 = time.time()
    dbs = []
    for n, (f, label) in enumerate(zip(folders, labels), 1):
        print('\n[%d/%d] Reading saves of %s: %s (%d files%s)' % (n, len(folders) + 1, label, f, save_count(f),
                                                                ', first %d only' % a.limit if a.limit else ''))
        dbs.append(extract(f, a.limit, a.workers))

    print('\n[%d/%d] Building the report' % (len(folders) + 1, len(folders) + 1))
    if len(dbs) == 2:
        from v3a.analysis import run_comparison
        run = run_comparison.main(['--a', dbs[0], '--b', dbs[1], '--label-a', labels[0], '--label-b', labels[1]])
        name = '%s_vs_%s.html' % (slug(labels[0]), slug(labels[1]))
    else:
        from v3a.analysis import run_analysis
        run = run_analysis.main(['--db', dbs[0]])
        name = '%s.html' % slug(labels[0])
    os.makedirs(REPORTS, exist_ok=True)
    out = os.path.join(REPORTS, name)
    shutil.copyfile(run.path('report.html'), out)

    print('\nDone in %.0f min.' % ((time.time() - t0) / 60))
    print('Report:       %s' % out)
    print('Details/logs: %s' % run.dir)
    if a.open:
        webbrowser.open('file:///' + out.replace('\\', '/'))


if __name__ == '__main__':
    main()
