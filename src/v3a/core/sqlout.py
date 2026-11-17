"""Small SQLite writer: typed tables, column documentation, views. Standard library only.

Every database written by the project contains:
  column_docs(table_name, column_name, type, unit, confidence, description)  - what each column means
  db_info(key, value)                                                       - run id, versions, created
"""
import datetime, sqlite3


class Table:
    """Column spec: list of (name, sql_type, unit, confidence, description)."""

    def __init__(self, name, columns, description='', primary_key=None, indexes=()):
        self.name, self.columns, self.description = name, columns, description
        self.primary_key, self.indexes = primary_key, indexes

    @property
    def names(self):
        """Column names in order."""
        return [c[0] for c in self.columns]


class DB:
    """SQLite database writer: tables from Table specs, views, indexes, column_docs and db_info."""
    def __init__(self, path):
        self.path = path
        self.con = sqlite3.connect(path)
        self.con.execute('PRAGMA journal_mode=OFF')
        self.con.execute('PRAGMA synchronous=OFF')
        self.con.execute('PRAGMA temp_store=MEMORY')
        self.con.execute('CREATE TABLE IF NOT EXISTS column_docs (table_name TEXT, column_name TEXT, type TEXT, '
                         'unit TEXT, confidence TEXT, description TEXT, PRIMARY KEY (table_name, column_name))')
        self.con.execute('CREATE TABLE IF NOT EXISTS db_info (key TEXT PRIMARY KEY, value TEXT)')
        self.info('created', datetime.datetime.now().isoformat(timespec='seconds'))
        self.tables = {}

    def info(self, key, value):
        """Store a key/value pair in db_info."""
        self.con.execute('INSERT OR REPLACE INTO db_info VALUES (?, ?)', (key, None if value is None else str(value)))

    def create(self, t):
        """Create (replace) a table and record its column docs."""
        cols = ', '.join('"%s" %s' % (c[0], c[1]) for c in t.columns)
        if t.primary_key:
            cols += ', PRIMARY KEY (%s)' % ', '.join(t.primary_key)
        self.con.execute('DROP TABLE IF EXISTS "%s"' % t.name)
        self.con.execute('CREATE TABLE "%s" (%s)' % (t.name, cols))
        self.con.execute('INSERT OR REPLACE INTO column_docs VALUES (?, ?, ?, ?, ?, ?)',
                         (t.name, '*', 'TABLE', '', '', t.description))
        self.con.executemany('INSERT OR REPLACE INTO column_docs VALUES (?, ?, ?, ?, ?, ?)',
                             [(t.name, c[0], c[1], c[2], c[3], c[4]) for c in t.columns])
        self.tables[t.name] = t
        return t

    def insert(self, t, rows):
        """rows: iterable of dicts (missing keys -> NULL) or sequences in column order."""
        name = t.name if isinstance(t, Table) else t
        t = self.tables[name]
        sql = 'INSERT INTO "%s" VALUES (%s)' % (name, ', '.join('?' * len(t.columns)))
        cols = t.names

        def conv(r):
            vals = [r.get(c) for c in cols] if isinstance(r, dict) else list(r)
            return [_sql(v) for v in vals]
        self.con.executemany(sql, (conv(r) for r in rows))

    def write(self, t, rows):
        """Create a table and insert rows in one call."""
        self.create(t); self.insert(t, rows)

    def view(self, name, sql, description=''):
        """Create (replace) a view and document it in column_docs."""
        self.con.execute('DROP VIEW IF EXISTS "%s"' % name)
        self.con.execute('CREATE VIEW "%s" AS %s' % (name, sql))
        self.con.execute('INSERT OR REPLACE INTO column_docs VALUES (?, ?, ?, ?, ?, ?)',
                         (name, '*', 'VIEW', '', '', description))

    def finish_indexes(self):
        """Create the indexes declared on the tables."""
        for t in self.tables.values():
            for i, cols in enumerate(t.indexes):
                self.con.execute('CREATE INDEX IF NOT EXISTS "ix_%s_%d" ON "%s" (%s)'
                                 % (t.name, i, t.name, ', '.join('"%s"' % c for c in cols)))

    def counts(self):
        """{table: row count} for every table."""
        names = [r[0] for r in self.con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {n: self.con.execute('SELECT COUNT(*) FROM "%s"' % n).fetchone()[0] for n in names}

    def close(self, vacuum=False):
        """Indexes, commit, ANALYZE (and VACUUM if asked), close."""
        self.finish_indexes()
        self.con.commit()
        self.con.execute('ANALYZE')
        if vacuum:
            self.con.execute('VACUUM')
        self.con.commit()
        self.con.close()


def _sql(v):
    if v is None or isinstance(v, (str, float)):
        return v
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return int(v) if -2**63 <= v < 2**63 else str(v)   # plain int (also unwraps Tok); u64 overflow -> text
    if isinstance(v, (list, tuple)):
        return ' | '.join(str(x) for x in v)
    return str(v)


def tokens_table(db, path):
    """Copy the token dictionary used for this run into the database."""
    from v3a.core.v3parse import read_tokens
    t = Table('tokens', [
        ('token_hex', 'TEXT', '', '', 'Token id as in the binary save, e.g. 0x3160'),
        ('raw_key', 'TEXT', '', '', 'How the id appears in paths and the facts table, e.g. k_3160'),
        ('name', 'TEXT', '', '', "Reverse-engineered name; trailing '?' = hypothesis"),
        ('confidence', 'TEXT', '', '', 'high / med / low'),
        ('evidence', 'TEXT', '', '', 'Why we believe the name')],
        'Token dictionary used to name fields in this database.', primary_key=['raw_key'])
    rows, seen = [], set()
    for tid, name, conf, ev in read_tokens(path):
        k = 'k_%04x' % tid
        if k not in seen:
            seen.add(k); rows.append(('0x%04x' % tid, k, name, conf, ev))
    db.write(t, rows)
