"""Game script files: definition folders (cached per game version) and start-state history blocks.

Shared by the research tools (game inventory, certainty: game keys and start-state matching).
  definitions()            {folder: Folder} for game/common/* and map_data/state_regions, parsed once, cached
  localisation(), game_name(key, loc)   names the game shows (English localisation), cached
  history_blocks(sub, top) (key, block) items inside the TOP = { } wrappers of game/common/history/<sub>
  unwrap(v), strip_scope(s), script_num(v)   small helpers for script values
Caches go to outputs/research/cache/<name>_<game version>.pkl (V3A_OUTPUTS respected).
"""
import json, os, pickle, re

from v3a.core.v3data import GAME
from v3a.core.v3parse import Obj
from v3a.core.pdxscript import parse_file
from v3a.core.runlog import outputs_base

OPS = {'=', '?=', '>', '<', '>=', '<=', '!=', '=='}
HISTORY = os.path.join(GAME, 'common', 'history')


# ------------------------------------------------------------------------------------------ versions + caches
def game_version():
    """Installed game version from the launcher settings ('' if unknown) - caches are kept per version."""
    try:
        with open(os.path.join(GAME, '..', 'launcher', 'launcher-settings.json'), encoding='utf-8') as f:
            return json.load(f).get('rawVersion', '')
    except (OSError, ValueError):
        return ''


def cache_path(name):
    """outputs/research/cache/<name>_<game version>.pkl (folder created)."""
    d = os.path.join(outputs_base(), 'research', 'cache')
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, '%s_%s.pkl' % (name, game_version() or 'unknown'))


def script_files(path):
    """All *.txt files below a folder (recursive), sorted by full path."""
    out = []
    for d, _, fs in os.walk(path):
        out += [os.path.join(d, f) for f in fs if f.endswith('.txt')]
    return sorted(out)


# ------------------------------------------------------------------------------------------ definitions
class Folder:
    """One definition folder: its files, parse errors, and top-level definitions [(key, block, file)] in file order."""
    __slots__ = ('path', 'files', 'errors', 'defs')

    def __init__(self, path):
        self.path, self.files, self.errors, self.defs = path, [], 0, []


def definition_folders(game=GAME):
    """[(name, path)]: every game/common/<name> folder (sorted) plus ('state_regions', map_data/state_regions)."""
    common = os.path.join(game, 'common')
    out = [(d, os.path.join(common, d)) for d in sorted(os.listdir(common)) if os.path.isdir(os.path.join(common, d))]
    out.append(('state_regions', os.path.join(game, 'map_data', 'state_regions')))
    return out


def read_folder(path):
    """Parse one folder -> Folder. Definitions are `key = { ... }` items at file level (keys starting with @ skipped)."""
    fo = Folder(path)
    fo.files = script_files(path)
    for f in fo.files:
        try:
            tree = parse_file(f)
        except Exception:
            fo.errors += 1; continue
        if not isinstance(tree, Obj):
            continue
        rel = os.path.relpath(f, path)
        for k, v in tree:
            if isinstance(k, str) and isinstance(v, Obj) and not k.startswith('@'):
                fo.defs.append((k, v, rel))
    return fo


_DEFS = {}


def definitions(game=GAME):
    """{folder name: Folder} for all definition folders; parsed once per game version, then read from the cache."""
    if game in _DEFS:
        return _DEFS[game]
    path = cache_path('game_definitions') if game == GAME else None
    if path and os.path.isfile(path):
        with open(path, 'rb') as f:
            _DEFS[game] = pickle.load(f)
        return _DEFS[game]
    out = {name: read_folder(p) for name, p in definition_folders(game)}
    if path:
        with open(path, 'wb') as f:
            pickle.dump(out, f, protocol=pickle.HIGHEST_PROTOCOL)
    _DEFS[game] = out
    return out


# ------------------------------------------------------------------------------------------ localisation (names)
_LOC = {}
_LOC_LINE = re.compile(r'^\s*([\w.\-]+):\d*\s*"(.*)"')


def localisation(game=GAME, language='english'):
    """{key: text} from game/localization/<language>/**/*.yml (the names the game shows); {} if not found.
    Parsed once per game version, then read from the cache. Texts may still hold $key$ references: see game_name."""
    if (game, language) in _LOC:
        return _LOC[(game, language)]
    folder = os.path.join(game, 'localization', language)
    path = cache_path('loc_%s' % language) if game == GAME else None
    if path and os.path.isfile(path):
        with open(path, 'rb') as f:
            out = pickle.load(f)
    else:
        out = {}
        for d, _, fs in os.walk(folder):
            for fn in sorted(fs):
                if not fn.endswith('.yml'):
                    continue
                with open(os.path.join(d, fn), encoding='utf-8-sig', errors='replace') as f:
                    for line in f:
                        m = _LOC_LINE.match(line)
                        if m:
                            out.setdefault(m.group(1), m.group(2))
        if path and out:
            with open(path, 'wb') as f:
                pickle.dump(out, f, protocol=pickle.HIGHEST_PROTOCOL)
    _LOC[(game, language)] = out
    return out


def game_name(key, loc, default=None):
    """Name the game shows for a key ($ref$ resolved, formatting codes dropped); default if missing or not plain text."""
    t = loc.get(key)
    for _ in range(4):
        if t is None or '$' not in t:
            break
        t = re.sub(r'\$([\w.\-]+)\$', lambda m: loc.get(m.group(1), m.group(0)), t)
    if not t or '$' in t or '[' in t:
        return default
    t = re.sub(r'#\w+\s?|#!', '', t).strip()
    return t or default


# ------------------------------------------------------------------------------------------ start-state history
def unwrap(v):
    """`x ?= {...}` (or any other operator) is stored as (op, value); return the value, like `x = {...}`."""
    if isinstance(v, tuple) and len(v) == 2 and v[0] in OPS:
        return v[1]
    return v


def strip_scope(s):
    """Drop a scope prefix such as 'c:', 's:', 'g:', 'cu:' from a script value; non-text is returned unchanged."""
    return s.split(':', 1)[1] if isinstance(s, str) and ':' in s else s


def script_num(v):
    """Number from a script value (text/int/float) or None."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def history_blocks(sub, top=None):
    """[(key, value)] inside the file-level wrapper blocks of common/history/<sub> (only wrappers named `top` if given).
    Values are unwrapped (`?=` treated as `=`); files in sorted path order."""
    out = []
    hist = definitions().get('history')
    for k, v, rel in (hist.defs if hist else []):
        if rel.replace('\\', '/').split('/')[0] != sub or (top is not None and k != top):
            continue
        out += [(kk, unwrap(vv)) for kk, vv in v]
    return out
