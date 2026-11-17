"""Run folders and run logs: every tool run gets its own output folder with a log.

    outputs/
      INDEX.md                               latest run of every kind with links (rewritten after each run)
      run_history.csv                        one line per run (index of all runs)
      <group>/<kind>/<YYYY-MM-DD>_<n>/       one run; n = 1, 2, 3 ... per day and kind
          <results>                          main results at the top (database, report, csv/md)
          data/                              supporting data files (when a run has a main report)
          logs/run.log                       human-readable: what ran, on which inputs, outputs, timings, warnings
          logs/run.json                      the same, machine-readable
          logs/inputs.csv                    one row per input (path, size, sha1, game version, in-game date)
    group = extraction | analysis | research (see GROUPS). Set V3A_OUTPUTS to send runs elsewhere (tests).

usage:
    with Run('snapshot', 'One save -> general SQLite database', settings={...}) as run:
        run.add_input(save_info(path))
        with run.step('parse'):
            ...
        db_path = run.path('snapshot.sqlite')          # run.path('data', 'x.sqlite') for a sub folder
        run.add_output(db_path, tables={'countries': 449}, main=True)
"""
import csv, datetime, json, os, platform, re, sqlite3, subprocess, sys, time, traceback
from contextlib import contextmanager

from v3a.core.v3data import OUTPUTS, ROOT

def outputs_base():
    """Where run folders go: env V3A_OUTPUTS (e.g. for tests) or outputs/."""
    return os.environ.get('V3A_OUTPUTS') or OUTPUTS


# kind -> output group (folder under outputs/). Unknown kinds go to 'other'.
GROUPS = {'snapshot': 'extraction', 'building_series': 'extraction',
          'economy_report': 'analysis', 'run_comparison': 'analysis',
          'token_report': 'research', 'definition_match': 'research', 'history_match': 'research',   # retired tools (old runs)
          'game_inventory': 'research', 'token_certainty': 'research', 'text_alignment': 'research'}

# kind -> name of its <group>/latest_*.html redirect (default latest_report.html), so kinds don't overwrite each other
LATEST_HTML = {'run_comparison': 'latest_comparison.html'}

HISTORY_COLS = ['run_id', 'folder', 'kind', 'status', 'started', 'duration_s', 'inputs', 'first_input_date',
                'last_input_date', 'game_versions', 'outputs', 'main_output', 'git_commit', 'command']


def group_of(kind):
    """Output group of a run kind ('extraction' | 'analysis' | 'research' | 'other')."""
    return GROUPS.get(kind, 'other')


def read_history(base=None):
    """Rows of run_history.csv as dicts (empty list if there is none)."""
    p = os.path.join(base or outputs_base(), 'run_history.csv')
    if not os.path.exists(p):
        return []
    with open(p, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def latest_run(kind, base=None, need=None, accept=None):
    """Folder of the newest successful run of `kind` (optionally containing file `need`, and accept(folder) true), or None."""
    base = base or outputs_base()
    for r in sorted(read_history(base), key=lambda r: r['started'], reverse=True):
        if r['kind'] == kind and r['status'].startswith('OK') and r.get('folder'):
            d = os.path.join(base, r['folder'])
            if os.path.isdir(d) and (not need or os.path.exists(os.path.join(d, need))) and (not accept or accept(d)):
                return d
    return None


def _git(*args):
    try:
        return subprocess.run(['git', '-C', ROOT] + list(args), capture_output=True, text=True,
                              timeout=10).stdout.strip()
    except Exception:
        return ''


def code_version():
    """Git commit of the code, with '(+uncommitted changes)' when src/ or library/ differ."""
    commit = _git('rev-parse', '--short', 'HEAD') or 'unknown'
    dirty = bool(_git('status', '--porcelain', '--', 'src', 'library'))
    return commit + (' (+uncommitted changes)' if dirty else '')


def available_memory_gb():
    """Free physical memory in GB (None if unknown)."""
    try:
        if os.name == 'nt':
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong)] +                            [(n, ctypes.c_ulonglong) for n in ('total', 'avail', 'tpf', 'apf', 'tv', 'av', 'aev')]
            m = MS(); m.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return m.avail / 2**30
        return os.sysconf('SC_AVPHYS_PAGES') * os.sysconf('SC_PAGE_SIZE') / 2**30
    except Exception:
        return None


def peak_memory_mb():
    """Peak memory of this process in MB (Windows working set / Unix maxrss); None if unavailable."""
    try:
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                            ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                            ('QuotaPeakPagedPoolUsage', ctypes.c_size_t), ('QuotaPagedPoolUsage', ctypes.c_size_t),
                            ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t), ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                            ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t)]
            c = PMC(); c.cb = ctypes.sizeof(PMC)
            k32 = ctypes.WinDLL('kernel32'); psapi = ctypes.WinDLL('psapi')
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
            if psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
                return round(c.PeakWorkingSetSize / 2**20)
            return None
        import resource
        r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(r / 1024 if sys.platform != 'darwin' else r / 2**20)
    except Exception:
        return None


def new_run_dir(kind, base=None, today=None):
    """Create outputs/<group>/<kind>/<date>_<n>, n = next free number for that date and kind."""
    base = base or outputs_base()
    today = today or datetime.date.today().isoformat()
    parent = os.path.join(base, group_of(kind), kind)
    os.makedirs(parent, exist_ok=True)
    pat = re.compile(r'^%s_(\d+)$' % re.escape(today))
    used = [int(m.group(1)) for d in os.listdir(parent) for m in [pat.match(d)] if m]
    n = max(used, default=0) + 1
    while True:
        path = os.path.join(parent, '%s_%d' % (today, n))
        try:
            os.makedirs(path)
            return path
        except FileExistsError:
            n += 1


def write_index(base=None):
    """outputs/INDEX.md: latest successful run of every kind (with link to its main result) + recent runs.
    Also writes <group>/latest_report.html (redirect; name per kind in LATEST_HTML) for kinds whose main output is HTML."""
    base = base or outputs_base()
    rows = read_history(base)
    latest = {}
    for r in sorted(rows, key=lambda r: r['started']):
        if r['status'].startswith('OK') and r.get('folder'):
            latest[r['kind']] = r
    L = ['# Outputs', '', 'Generated after every run by `src/v3a/core/runlog.py` - do not edit. '
         'Full list: `run_history.csv`. Each run folder: results at the top, `logs/run.log` explains the run.', '']
    for g in ('extraction', 'analysis', 'research', 'other'):
        ks = sorted(k for k, r in latest.items() if group_of(k) == g)
        if not ks:
            continue
        L += ['## ' + g, '', '| Kind | Latest run | Main result | Inputs | Started |', '|---|---|---|---|---|']
        for k in ks:
            r = latest[k]
            folder = r['folder'].replace('\\', '/')
            main = r.get('main_output') or ''
            link = '[%s](%s/%s)' % (main, folder, main) if main else ''
            span = r['first_input_date'] if r['first_input_date'] == r['last_input_date'] else \
                '%s → %s' % (r['first_input_date'], r['last_input_date'])
            inputs = r['inputs'] + (' (%s)' % span if r['first_input_date'] else '')
            L.append('| %s | [%s](%s/) | %s | %s | %s |' % (k, folder, folder, link, inputs, r['started']))
            if main.endswith('.html'):
                with open(os.path.join(base, g, LATEST_HTML.get(k, 'latest_report.html')), 'w', encoding='utf-8') as f:
                    f.write('<!doctype html><meta charset="utf-8"><title>Latest report</title>'
                            '<meta http-equiv="refresh" content="0; url=%s/%s">'
                            '<p>Opening <a href="%s/%s">%s</a> ...</p>\n' % (
                                os.path.relpath(os.path.join(base, folder), os.path.join(base, g)).replace('\\', '/'), main,
                                os.path.relpath(os.path.join(base, folder), os.path.join(base, g)).replace('\\', '/'), main,
                                folder + '/' + main))
        L.append('')
    L += ['## Recent runs', '', '| Run | Kind | Status | Duration (s) |', '|---|---|---|---|']
    for r in sorted(rows, key=lambda r: r['started'], reverse=True)[:15]:
        folder = (r.get('folder') or '').replace('\\', '/')
        L.append('| %s | %s | %s | %s |' % ('[%s](%s/)' % (r['run_id'], folder) if folder else r['run_id'],
                                            r['kind'], r['status'], r['duration_s']))
    with open(os.path.join(base, 'INDEX.md'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(L) + '\n')


def _fmt_size(b):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if b < 1024 or unit == 'GB':
            return '%.1f %s' % (b, unit) if unit != 'B' else '%d B' % b
        b /= 1024.0


def _fmt_s(s):
    return '%dh %02dm %02ds' % (s // 3600, s % 3600 // 60, s % 60) if s >= 3600 else \
           '%dm %02ds' % (s // 60, s % 60) if s >= 60 else '%.1fs' % s


class Run:
    """Context manager for one tool run: own folder, input/output records, timed steps, logs/run.log + run.json."""
    def __init__(self, kind, description, settings=None, base=None):
        self.kind, self.description = kind, description
        self.settings = dict(settings or {})
        self.base = base or outputs_base()
        self.dir = new_run_dir(kind, self.base)
        self.folder = os.path.relpath(self.dir, self.base).replace('\\', '/')
        self.history = os.path.join(self.base, 'run_history.csv')
        self.id = '%s_%s' % (os.path.basename(self.dir), kind)       # e.g. 2026-11-13_1_building_series
        self.main_output = None
        self.inputs, self.outputs, self.steps, self.warnings, self.notes = [], [], [], [], []
        self.status, self.error = 'RUNNING', None
        self.started = datetime.datetime.now()
        self._t0 = time.perf_counter()
        print('run folder:', self.dir)

    # ---------------------------------------------------------------- recording
    def path(self, *parts):
        """Path inside the run folder; sub folders (e.g. run.path('data', 'x.sqlite')) are created."""
        p = os.path.join(self.dir, *parts)
        if len(parts) > 1:
            os.makedirs(os.path.dirname(p), exist_ok=True)
        return p

    def log_path(self, name):
        """Path inside the run's logs/ folder."""
        return self.path('logs', name)

    def add_input(self, info):
        """Record an input save (dict from v3data.save_info)."""
        self.inputs.append(dict(info))

    def add_output(self, path, description='', tables=None, main=False):
        """Record an output file; main=True puts it in the summary and INDEX.md."""
        rel = os.path.relpath(path, self.dir).replace('\\', '/')
        self.outputs.append({'file': rel, 'description': description,
                             'size_bytes': os.path.getsize(path) if os.path.exists(path) else None,
                             'tables': dict(tables or {})})
        if main or self.main_output is None:
            self.main_output = rel

    def warn(self, msg):
        """Print and record a warning (status becomes "OK (with warnings)")."""
        print('WARNING:', msg)
        self.warnings.append(str(msg))

    def note(self, msg):
        """Record a note for run.log."""
        self.notes.append(str(msg))

    @contextmanager
    def step(self, name):
        """Context manager: time a named step for run.log."""
        t = time.perf_counter()
        print('...', name)
        try:
            yield
        finally:
            self.steps.append({'step': name, 'seconds': round(time.perf_counter() - t, 2)})

    # ---------------------------------------------------------------- lifecycle
    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        if et is None:
            self.status = 'OK' if not self.warnings else 'OK (with warnings)'
        else:
            self.status = 'FAILED' if et is not KeyboardInterrupt else 'CANCELLED'
            self.error = ''.join(traceback.format_exception(et, ev, tb))
        self.finish()
        return False

    def info(self):
        """Everything about the run as a dict (written to run.json)."""
        dur = time.perf_counter() - self._t0
        dates = [i.get('date_iso') for i in self.inputs if i.get('date_iso')]
        tf = os.environ.get('V3A_TOKENS') or ''
        try:
            from v3a.core.v3parse import tokens_file
            tf = tf or tokens_file(self.inputs[0].get('game_version') if self.inputs else None) or ''
        except Exception:
            pass
        return {
            'run_id': self.id, 'folder': self.folder, 'kind': self.kind, 'description': self.description, 'status': self.status,
            'started': self.started.isoformat(timespec='seconds'),
            'finished': datetime.datetime.now().isoformat(timespec='seconds'),
            'duration_s': round(dur, 1), 'command': ' '.join([os.path.basename(sys.executable)] + sys.argv),
            'git_commit': code_version(), 'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version,
            'platform': platform.platform(terse=True), 'cpus': os.cpu_count(),
            'peak_memory_mb_main_process': peak_memory_mb(),
            'token_dictionary': os.path.relpath(tf, ROOT) if tf else None,
            'settings': self.settings,
            'inputs_summary': {'count': len(self.inputs), 'first_date': min(dates, default=None),
                               'last_date': max(dates, default=None),
                               'game_versions': sorted({str(i.get('game_version')) for i in self.inputs}),
                               'total_bytes': sum(i.get('size_bytes') or 0 for i in self.inputs)},
            'inputs': self.inputs, 'outputs': self.outputs, 'steps': self.steps,
            'warnings': self.warnings, 'notes': self.notes, 'error': self.error,
        }

    def finish(self):
        """Write run.json, inputs.csv, run.log, append run_history.csv and refresh INDEX.md."""
        I = self.info()
        with open(self.log_path('run.json'), 'w', encoding='utf-8') as f:
            json.dump(I, f, indent=2, default=str)
        if self.inputs:
            cols = ['file', 'game_date', 'date_iso', 'game_version', 'size_bytes', 'sha1', 'save_type', 'path']
            with open(self.log_path('inputs.csv'), 'w', newline='', encoding='utf-8') as f:
                w = csv.DictWriter(f, cols, extrasaction='ignore'); w.writeheader(); w.writerows(self.inputs)
        with open(self.log_path('run.log'), 'w', encoding='utf-8') as f:
            f.write(self.render(I))
        s = I['inputs_summary']
        row = dict(zip(HISTORY_COLS, [
            I['run_id'], self.folder, I['kind'], I['status'], I['started'], I['duration_s'], s['count'],
            s['first_date'], s['last_date'], ' '.join(s['game_versions']), ' '.join(o['file'] for o in self.outputs),
            self.main_output or '', I['git_commit'], I['command']]))
        rows = read_history(self.base)
        old_header = rows and list(rows[0].keys()) != HISTORY_COLS
        with open(self.history, 'w' if old_header or not rows else 'a', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, HISTORY_COLS, extrasaction='ignore', restval='')
            if old_header or not rows:          # new file, or older column layout -> rewrite with current columns
                w.writeheader(); w.writerows(rows)
            w.writerow(row)
        try:
            write_index(self.base)
        except Exception as e:      # the index is a convenience; never fail a run because of it
            print('WARNING: could not write INDEX.md:', e)
        print('run %s: %s in %s -> %s' % (self.id, self.status, _fmt_s(I['duration_s']), self.log_path('run.log')))

    @staticmethod
    def render(I):
        """run.json dict -> human-readable run.log text."""
        L = []
        a = L.append
        a('Vic3 Save Analyzer - run log')
        a('=' * 78)
        for k, v in [('Run', I['run_id']), ('Folder', 'outputs/' + I.get('folder', '')), ('Kind', '%s - %s' % (I['kind'], I['description'])),
                     ('Status', I['status']), ('Started', I['started']), ('Finished', I['finished']),
                     ('Duration', _fmt_s(I['duration_s'])), ('Command', I['command']),
                     ('Code version', I['git_commit']),
                     ('Environment', 'Python %s, SQLite %s, %s, %s CPUs' % (I['python'], I['sqlite'], I['platform'], I['cpus'])),
                     ('Peak memory', '%s MB (main process)' % I['peak_memory_mb_main_process']),
                     ('Token dictionary', I['token_dictionary'])]:
            a('%-17s %s' % (k + ':', v))
        if I['settings']:
            a('Settings:'); [a('  %-15s %s' % (k, v)) for k, v in I['settings'].items()]
        s = I['inputs_summary']
        a(''); a('INPUTS (%d save files, %s, in-game %s -> %s, game %s)' % (
            s['count'], _fmt_size(s['total_bytes']), s['first_date'], s['last_date'], ', '.join(s['game_versions'])))
        a('-' * 78)
        for i in I['inputs']:
            a('  %-34s %-11s %9s  %s  sha1 %s' % (i.get('file'), i.get('game_date'), _fmt_size(i.get('size_bytes') or 0),
                                                  i.get('game_version'), (i.get('sha1') or '')[:12]))
        if I['inputs']:
            a('  folder: %s' % os.path.dirname(I['inputs'][0].get('path', '')))
        a(''); a('OUTPUTS'); a('-' * 78)
        for o in I['outputs']:
            a('  %s  (%s)%s' % (o['file'], _fmt_size(o['size_bytes'] or 0), '  - ' + o['description'] if o['description'] else ''))
            for t, n in o['tables'].items():
                a('      %-34s %12s rows' % (t, format(n, ',') if isinstance(n, int) else n))
        a(''); a('STEPS'); a('-' * 78)
        for st in I['steps']:
            a('  %-50s %s' % (st['step'], _fmt_s(st['seconds'])))
        if I['notes']:
            a(''); a('NOTES'); a('-' * 78); [a('  - ' + n) for n in I['notes']]
        if I['warnings']:
            a(''); a('WARNINGS'); a('-' * 78); [a('  - ' + w) for w in I['warnings']]
        if I['error']:
            a(''); a('ERROR'); a('-' * 78); a(I['error'])
        return '\n'.join(L) + '\n'
