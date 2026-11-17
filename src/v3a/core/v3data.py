"""Open .v3 save files and access their data.

Rewritten 2026-11-12 from the interface the tools use; the original module was lost.

Save container (observed, game 1.15.1):
  24-byte ASCII header  'SAV' + format(2 hex) + type(2 hex) + checksum?(8 hex) + meta_length(8 hex) + '\\n'
                        e.g. SAV01 03 a9c65171 000002f8 ; type 03 = binary + zip
  meta block            meta_length bytes, binary tokens: meta_data = { version, game_date, ... }
  ZIP archive           entry 'gamestate' = binary token stream (contains meta_data again)
"""
import io, os, re, zipfile

from v3a.core.v3parse import Obj, K, parse, raw, decode_date, tokens_file, use_tokens

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..'))
INPUTS = os.path.join(ROOT, 'inputs')
OUTPUTS = os.path.join(ROOT, 'outputs')
# Game install ('game' folder): set VIC3_GAME if it is not in the default Steam location. Read-only.
GAME = os.environ.get('VIC3_GAME', r'C:/Program Files (x86)/Steam/steamapps/common/Victoria 3/game')
POP_SCALE = 100000          # pop sizes are stored divided by 100,000

HEADER = re.compile(rb'SAV(?P<fmt>[0-9a-f]{2})(?P<type>[0-9a-f]{2})(?P<check>[0-9a-f]{8})(?P<meta_len>[0-9a-f]{8})\n')
BINARY_TYPES = {'01', '03'}  # observed: 03; 01 assumed (binary, uncompressed)


class SaveFile:
    """Raw pieces of one .v3 file: header fields, meta bytes, gamestate bytes."""

    def __init__(self, path):
        self.path = path
        data = open(path, 'rb').read()
        m = HEADER.match(data)
        if not m:
            raise ValueError('%s: not a Victoria 3 save (header %r)' % (path, data[:24]))
        self.header = {k: v.decode() for k, v in m.groupdict().items()}
        body = m.end()
        meta_len = int(self.header['meta_len'], 16)
        self.meta_bytes = data[body:body + meta_len]
        rest = data[body + meta_len:]
        if self.header['type'] not in BINARY_TYPES:
            raise ValueError('%s: save type %s is not binary (text saves: research/run_text_alignment.read_text_save)'
                             % (path, self.header['type']))
        self._rest = rest
        self._gamestate = None
        self._meta = None
        self.size = len(data)

    @property
    def gamestate(self):
        """Decompressed gamestate bytes (unzipped on first access)."""
        if self._gamestate is None:
            if self._rest[:4] == b'PK\x03\x04':
                self._gamestate = zipfile.ZipFile(io.BytesIO(self._rest)).read('gamestate')
            else:
                self._gamestate = self._rest
            self._rest = None
        return self._gamestate

    @property
    def meta(self):
        """meta_data block (small, fast). Keys: k_00ee version, k_3245 game_date, ..."""
        if self._meta is None:
            top = parse(self.meta_bytes)
            self._meta = top.get(K.meta_data) or top
        return self._meta

    @property
    def version(self):
        """Game version from the meta block, e.g. "1.15.1"."""
        return self.meta.get(K.version)

    @property
    def game_date(self):
        """In-game date from the meta block as 'Y.M.D'."""
        return decode_date(self.meta.get(K.game_date))


# ---------------------------------------------------------------------------------- save lists
def find_saves(folder=None):
    """[(name, path)] of all .v3 files under folder (default: env V3A_SAVES or inputs/), sorted by path.
    name = file name without extension."""
    folder = folder or os.environ.get('V3A_SAVES') or INPUTS
    out = []
    for d, _, files in os.walk(folder):
        for f in files:
            if f.lower().endswith('.v3'):
                out.append((os.path.splitext(f)[0], os.path.join(d, f)))
    return sorted(out, key=lambda x: x[1])


# ---------------------------------------------------------------------------------- tokens
_TOKENS_FOR = None


def select_tokens(ver):
    """Load the token dictionary matching game version `ver` (warns when falling back). Returns its path."""
    global _TOKENS_FOR
    tf = tokens_file(ver)
    if ver != _TOKENS_FOR:
        if tf and os.path.basename(tf) != 'tokens_%s.txt' % ver:
            print('WARNING: no token dictionary for game %s, using %s' % (ver, os.path.basename(tf)))
        use_tokens(tf); _TOKENS_FOR = ver
    return tf


def date_iso(d):
    """'1836.2.1' -> '1836-02-01' (sortable)."""
    if not d:
        return None
    y, m, dd = (int(x) for x in d.split('.'))
    return '%04d-%02d-%02d' % (y, m, dd)


def sha1_file(path):
    """SHA-1 of a file (read in 1 MB chunks)."""
    import hashlib
    h = hashlib.sha1()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def save_info(path, sha1=True):
    """Cheap facts about a save (no full parse): path, size, sha1, game version, in-game date."""
    sf = SaveFile(path)
    info = {'file': os.path.basename(path), 'path': os.path.abspath(path), 'size_bytes': sf.size,
            'game_version': sf.version, 'game_date': sf.game_date, 'date_iso': date_iso(sf.game_date),
            'save_type': sf.header['type']}
    if sha1:
        info['sha1'] = sha1_file(path)
    return info


def series(folder, dedupe=True):
    """save_info() of every .v3 under folder, sorted by in-game date; identical files (sha1) kept once."""
    infos, seen = [], set()
    for _, p in find_saves(folder):
        i = save_info(p)
        if dedupe and i['sha1'] in seen:
            continue
        seen.add(i['sha1']); infos.append(i)
    return sorted(infos, key=lambda i: (i['date_iso'] or '', i['file']))


TRUNCATED_STEP = 12     # monthly autosaves -> one per in-game year


def pick_saves(infos, every=1, limit=0, truncated=False):
    """Sample of a date-sorted series: truncated = first + every 12th + last save; else every k-th, at most limit."""
    if truncated and infos:
        idx = sorted(set(range(0, len(infos), TRUNCATED_STEP)) | {len(infos) - 1})
        infos = [infos[i] for i in idx]
    else:
        infos = infos[::max(1, every)]
    return infos[:limit] if limit else infos


def db(g, section):
    """{id: Obj} of a database section, e.g. db(g, 'pops'). Deleted entries (`id = none`) are skipped."""
    sec = g.get(raw(section))
    d = sec.get(K.database) if isinstance(sec, Obj) else None
    if not isinstance(d, Obj):
        return {}
    return {i: x for i, x in d if isinstance(x, Obj)}


def game_date(g):
    """In-game date of a parsed gamestate as 'Y.M.D'."""
    return decode_date(g.get(K.date))


def version(g):
    """Game version of a parsed gamestate (from its meta_data block)."""
    m = g.get(K.meta_data)
    return m.get(K.version) if isinstance(m, Obj) else None
