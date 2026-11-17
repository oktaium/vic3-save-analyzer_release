"""Parse a Victoria 3 binary token stream (the `gamestate` / meta block) into a tree.

Rewritten 2026-11-12 from the interface the tools use; the original module was lost.

Tree model
  Obj(list)  a { } block with at least one `key = value`. Items are (key, value) tuples;
             bare values inside a keyed block get key None.
  list       a { } block with only bare values, e.g. { 1 2 3 } or { {..} {..} }.
  Empty { }  -> Obj().
  Keys       field-name token -> 'k_XXXX' (raw id, hex; names come from the token dictionary),
             integer -> int (database ids, sparse-array indices), string -> str.
  Values     int / float / bool / str / tuple (rgb) / Tok (a field-name token used as a value,
             e.g. `none`) / Obj / list.
Numbers are exact save values. Only *names* of tokens are reverse-engineered (see library/tokens/).
"""
import os, re, struct

from v3a.core.v3lex import (EQ, OPEN, CLOSE, I32, U32, F32, BOOL, QSTR, USTR, F64, U64, I64, RGB,
                   FIXED5_ZERO, FIXED5_POS, FIXED5_NEG, LOOKUP)

_HERE = os.path.dirname(os.path.abspath(__file__))
TOKENS_DIR = os.path.normpath(os.path.join(_HERE, '..', '..', '..', 'library', 'tokens'))


# ---------------------------------------------------------------------------------- tree types
class Tok(int):
    """A field-name token appearing as a value (e.g. `none`, `yes`-like flags)."""
    __slots__ = ()

    def __repr__(self):
        return '@' + tname(self)


class Obj(list):
    """Ordered (key, value) pairs; keys may repeat."""
    __slots__ = ()

    def get(self, key, default=None):
        """Value of the first item with this key (or default)."""
        for k, v in self:
            if k == key:
                return v
        return default

    def getall(self, key):
        """Values of all items with this key (keys may repeat)."""
        return [v for k, v in self if k == key]

    def keys(self):
        """Keys in order (may repeat)."""
        return [k for k, _ in self]

    def values(self):
        """Values in order."""
        return [v for _, v in self]

    def __getitem__(self, i):
        r = list.__getitem__(self, i)
        return Obj(r) if isinstance(i, slice) else r


# ---------------------------------------------------------------------------------- token names
def _version_key(path):
    m = re.search(r'tokens_([\d.]+)\.txt$', path)
    return tuple(int(x) for x in m.group(1).split('.')) if m else ()


def tokens_file(version=None):
    """Token dictionary for a game version (exact match, else the newest available)."""
    if os.environ.get('V3A_TOKENS'):
        return os.environ['V3A_TOKENS']
    files = sorted((os.path.join(TOKENS_DIR, f) for f in os.listdir(TOKENS_DIR) if f.endswith('.txt')),
                   key=_version_key) if os.path.isdir(TOKENS_DIR) else []
    if version:
        exact = [f for f in files if os.path.basename(f) == 'tokens_%s.txt' % version]
        if exact:
            return exact[0]
    return files[-1] if files else None


TOKEN_LINE = re.compile(r'\s*0x([0-9a-fA-F]{4})\s+(\S+)\s*(?:#\s*(?:\[(\w[\w-]*)\])?\s*(.*))?')


def read_tokens(path):
    """[(id, name, confidence, evidence)] from a token dictionary file."""
    out = []
    if path and os.path.isfile(path):
        for line in open(path, encoding='utf-8'):
            m = TOKEN_LINE.match(line)
            if m:
                out.append((int(m.group(1), 16), m.group(2), m.group(3) or '', (m.group(4) or '').strip()))
    return out


TOKENS = {}         # id -> game name (first non-alias entry wins)
_BY_NAME = {}       # name (with and without trailing '?') -> 'k_XXXX'; game names first, [alias] lines fill gaps


def name_lookup(rows):
    """{name (with and without trailing '?'): id} from dictionary rows; game names win over [alias] lines."""
    look = {}
    for alias_pass in (False, True):
        for t, name, conf, _ in rows:
            if (conf == 'alias') == alias_pass:
                for n in (name, name.rstrip('?')):
                    look.setdefault(n, t)
    return look


def use_tokens(path):
    """Load a token dictionary file into TOKENS (id -> game name) and the reverse name lookup."""
    TOKENS.clear(); _BY_NAME.clear()
    rows = read_tokens(path)
    for t, name, conf, _ in rows:
        if conf != 'alias':
            TOKENS.setdefault(t, name)
    for t, name, _, _ in rows:          # ids that only have alias lines still get a display name
        TOKENS.setdefault(t, name)
    _BY_NAME.update({n: 'k_%04x' % t for n, t in name_lookup(rows).items()})


class _Keys:
    """K.<game name> -> raw key 'k_XXXX' of the loaded dictionary, e.g. K.workforce == 'k_2fd5' (game 1.15.1).
    Code names fields by their game names; ids are looked up per game version. AttributeError if unknown."""
    __slots__ = ()

    def __getattr__(self, name):
        try:
            return _BY_NAME[name]
        except KeyError:
            raise AttributeError('token name %r is not in the loaded dictionary' % name) from None


K = _Keys()


use_tokens(tokens_file())


def tname(t):
    """Name of a token id (int/Tok) or 'k_XXXX' if unknown."""
    return TOKENS.get(int(t), 'k_%04x' % int(t))


def is_num(v):
    """True for a plain number value (bools and tokens excluded)."""
    return isinstance(v, (int, float)) and not isinstance(v, (bool, Tok))


def num(v):
    """The value if it is a plain number, else None (bools and tokens excluded)."""
    return v if is_num(v) else None


def label(k):
    """Readable name for a raw key 'k_XXXX' (unchanged if unknown or not a raw key)."""
    if isinstance(k, str) and k.startswith('k_') and len(k) == 6:
        try:
            return TOKENS.get(int(k[2:], 16), k)
        except ValueError:
            return k
    return str(k)


def raw(name):
    """'pops' -> 'k_3160'. Raw keys pass through. KeyError if the name is unknown."""
    if isinstance(name, str) and name.startswith('k_'):
        return name
    return _BY_NAME[name]


# ---------------------------------------------------------------------------------- dates
_MONTH_DAYS = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def decode_date(x, lo=1700, hi=2200):
    """Paradox date int (hours since 1 Jan 5000 BC, 365-day years) -> 'Y.M.D', or None if implausible."""
    if not isinstance(x, int) or isinstance(x, (bool, Tok)) or x <= 0 or x % 24:
        return None
    days = x // 24
    year, doy = divmod(days, 365)
    year -= 5000
    if not lo <= year <= hi:
        return None
    m = 0
    while doy >= _MONTH_DAYS[m]:
        doy -= _MONTH_DAYS[m]; m += 1
    return '%d.%d.%d' % (year, m + 1, doy + 1)


# ---------------------------------------------------------------------------------- parser
_u16 = struct.Struct('<H').unpack_from
_i32 = struct.Struct('<i').unpack_from
_u32 = struct.Struct('<I').unpack_from
_f32 = struct.Struct('<f').unpack_from
_i64 = struct.Struct('<q').unpack_from
_u64 = struct.Struct('<Q').unpack_from


class ParseError(ValueError):
    """Malformed binary token stream."""
    pass


def _scalar(buf, t, p):
    """Value of token t whose payload starts at p -> (value, new_p). Field tokens -> Tok."""
    if t == I32: return _i32(buf, p)[0], p + 4
    if t == U32: return _u32(buf, p)[0], p + 4
    if t == F64: return _i64(buf, p)[0] / 100000.0, p + 8
    if t == QSTR or t == USTR:
        L = _u16(buf, p)[0]; p += 2
        return buf[p:p + L].decode('utf-8', 'replace'), p + L
    if t == BOOL: return buf[p] != 0, p + 1
    if t == F32: return _f32(buf, p)[0], p + 4
    if t == U64: return _u64(buf, p)[0], p + 8
    if t == I64: return _i64(buf, p)[0], p + 8
    if t == RGB and _u16(buf, p)[0] == OPEN:
        vals, q = [], p + 2
        while _u16(buf, q)[0] != CLOSE:
            tt = _u16(buf, q)[0]
            v, q = _scalar(buf, tt, q + 2)
            vals.append(v)
        return tuple(vals), q + 2
    if t == FIXED5_ZERO: return 0.0, p
    if t in FIXED5_POS:
        L = FIXED5_POS[t]; return int.from_bytes(buf[p:p + L], 'little') / 100000.0, p + L
    if t in FIXED5_NEG:
        L = FIXED5_NEG[t]; return -int.from_bytes(buf[p:p + L], 'little') / 100000.0, p + L
    if t in LOOKUP:
        # Not observed in 1.15.1 saves; kept visible rather than guessed.
        L = LOOKUP[t]; return ('lookup', int.from_bytes(buf[p:p + L], 'little')), p + L
    if t == EQ or t == CLOSE:
        raise ParseError('unexpected %s at byte %d' % ('=' if t == EQ else '}', p - 2))
    return Tok(t), p


def _key(v):
    if isinstance(v, Tok):
        return 'k_%04x' % v
    if isinstance(v, (int, str)) and not isinstance(v, bool):
        return v
    return repr(v)


def _block(buf, p, n, top=False):
    """Parse items until '}' (or end of buffer when top). Returns (Obj|list, p after '}')."""
    items = []
    keyed = False
    while p < n:
        t = _u16(buf, p)[0]; p += 2
        if t == CLOSE:
            if top:
                raise ParseError('unbalanced } at byte %d' % (p - 2))
            break
        if t == OPEN:
            v, p = _block(buf, p, n)
        else:
            v, p = _scalar(buf, t, p)
        if p < n and _u16(buf, p)[0] == EQ:
            p += 2
            t2 = _u16(buf, p)[0]; p += 2
            if t2 == OPEN:
                val, p = _block(buf, p, n)
            elif t2 == CLOSE:            # `key = }` - tolerate, value missing
                items.append((_key(v), None)); keyed = True
                break
            else:
                val, p = _scalar(buf, t2, p)
            items.append((_key(v), val)); keyed = True
        else:
            items.append((None, v))
    else:
        if not top:
            raise ParseError('unexpected end of data inside a block')
    if keyed or not items:
        return Obj(items), p
    return [v for _, v in items], p


def parse(buf):
    """Parse a whole token stream (gamestate or meta) -> top-level Obj."""
    v, _ = _block(buf, 0, len(buf), top=True)
    return v if isinstance(v, Obj) else Obj((None, x) for x in v)


def parse_value(buf, p):
    """Parse the single value starting at byte p (as returned by top_index)."""
    t = _u16(buf, p)[0]
    if t == OPEN:
        return _block(buf, p + 2, len(buf))[0]
    return _scalar(buf, t, p + 2)[0]


def _skip(buf, p):
    """Byte position after the value starting at p."""
    t = _u16(buf, p)[0]; p += 2
    if t != OPEN:
        return _scalar(buf, t, p)[1]
    depth = 1
    while depth:
        t = _u16(buf, p)[0]; p += 2
        if t == OPEN: depth += 1
        elif t == CLOSE: depth -= 1
        elif t != EQ: p = _scalar(buf, t, p)[1]
    return p


def top_index(buf):
    """Yield (key, value_start, value_end) for each top-level `key = value` without parsing values."""
    p, n = 0, len(buf)
    while p < n:
        t = _u16(buf, p)[0]
        k, p = _scalar(buf, t, p + 2)
        if p < n and _u16(buf, p)[0] == EQ:
            s = p + 2
            p = _skip(buf, s)
            yield _key(k), s, p
