"""Parser for Paradox script text (game/common, game/history, mods; later plain-text saves).

Rewritten 2026-11-12 from the interface the tools use; the original module was lost.

Same tree model as v3parse: Obj (keyed block, list of (key, value)), list (bare values), scalars.
Scalars are kept as strings (as written in the file; convert with float()/int() where needed).
  key = value            -> (key, 'value')
  key > value            -> (key, ('>', 'value'))      any operator other than '='
  color = rgb { 1 2 3 }  -> (color, ('rgb', ['1', '2', '3']))
  x = @[ a * 2 ]         -> (x, '@[ a * 2 ]')
"""
import os, re

from v3a.core.v3parse import Obj

_TOKEN = re.compile(r'''
    (?P<ws>\s+|\#[^\n]*)
  | (?P<str>"(?:[^"\\]|\\.)*")
  | (?P<math>@\[[^\]]*\])
  | (?P<op><=|>=|!=|\?=|==|=|<|>)
  | (?P<open>\{)
  | (?P<close>\})
  | (?P<word>[^\s=<>!?{}"\#]+)
''', re.X)

COLOR_PREFIX = {'rgb', 'hsv', 'hsv360', 'hex'}


def tokenize(text):
    """Split script text into (kind, text) tokens: word, op, open, close (comments and whitespace dropped)."""
    pos, n = 0, len(text)
    while pos < n:
        m = _TOKEN.match(text, pos)
        if not m:
            raise SyntaxError('unexpected character %r at offset %d' % (text[pos], pos))
        pos = m.end()
        kind = m.lastgroup
        if kind == 'ws':
            continue
        v = m.group()
        if kind == 'str':
            v = re.sub(r'\\(.)', r'\1', v[1:-1])
            kind = 'word'
        elif kind == 'math':
            kind = 'word'
        yield kind, v


def _parse(toks):
    toks = list(toks)
    n = len(toks)

    def value(i):
        kind, v = toks[i]
        if kind == 'open':
            return block(i + 1)
        if kind == 'word':
            if v in COLOR_PREFIX and i + 1 < n and toks[i + 1][0] == 'open':
                inner, j = block(i + 2)
                return (v, inner), j
            return v, i + 1
        raise SyntaxError('expected a value, got %r (token %d)' % (v, i))

    def block(i, top=False):
        items, keyed = [], False
        while i < n:
            kind, v = toks[i]
            if kind == 'close':
                if top:                 # stray '}' at file level: the game ignores it, so do we
                    i += 1; continue
                i += 1; break
            if kind == 'op':            # stray operator - skip
                i += 1; continue
            x, i = value(i)
            if i < n and toks[i][0] == 'op':
                op = toks[i][1]
                if i + 1 >= n or toks[i + 1][0] == 'close':
                    items.append((x if isinstance(x, str) else repr(x), None)); keyed = True; i += 1
                    continue
                val, i = value(i + 1)
                items.append((x if isinstance(x, str) else repr(x), val if op == '=' else (op, val)))
                keyed = True
            else:
                items.append((None, x))
        if keyed or not items:
            return Obj(items), i
        return [v for _, v in items], i

    return block(0, top=True)[0]


def parse_text(text):
    """Parse script text -> Obj (or list if the text has only bare values)."""
    return _parse(tokenize(text))


def parse_file(path):
    """Parse one script file (UTF-8 with BOM) -> Obj."""
    with open(path, encoding='utf-8-sig', errors='replace') as f:
        return parse_text(f.read())


def parse_dir(path, recursive=False):
    """All *.txt files of a folder merged into one Obj, in file-name order (the game's load order)."""
    out = Obj()
    files = []
    if recursive:
        for d, _, fs in os.walk(path):
            files += [os.path.join(d, f) for f in fs if f.endswith('.txt')]
    elif os.path.isdir(path):
        files = [os.path.join(path, f) for f in os.listdir(path) if f.endswith('.txt')]
    for f in sorted(files, key=lambda p: os.path.basename(p).lower()):
        tree = parse_file(f)
        if isinstance(tree, Obj):
            out.extend(tree)
        else:
            out.extend((None, x) for x in tree)
    return out
