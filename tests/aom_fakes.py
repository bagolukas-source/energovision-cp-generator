"""In-memory Supabase klient pre offline testy AOM (analyza_om_v2) — žiadna sieť, žiadna DB.
Prevzaté z auditu 2026-10-24 (06-orchestracia-posudok/fakes.py)."""
import copy, uuid


class _Res:
    def __init__(self, data, count=None):
        self.data = data
        self.count = count


class _Q:
    def __init__(self, db, table):
        self.db, self.table, self.op = db, table, "select"
        self.filters, self._order, self._limit, self._single, self._maybe = [], None, None, False, False
        self.payload = None
        self._count = None

    # builders
    def select(self, cols="*", count=None):
        self.op = "select"; self._count = count
        self._cols = None if (cols.strip() == "*" or "(" in cols) else [c.strip() for c in cols.split(",")]
        return self
    def eq(self, k, v):
        self.filters.append((k, v)); return self
    def order(self, col, desc=False):
        self._order = (col, desc); return self
    def limit(self, n):
        self._limit = n; return self
    def single(self):
        self._single = True; return self
    def maybe_single(self):
        self._maybe = True; return self
    def insert(self, rows):
        self.op = "insert"; self.payload = rows; return self
    def update(self, d):
        self.op = "update"; self.payload = d; return self
    def delete(self):
        self.op = "delete"; return self
    def upsert(self, row, on_conflict=None):
        self.op = "insert"; self.payload = row; return self

    def _match(self, r):
        return all(str(r.get(k)) == str(v) for k, v in self.filters)

    def execute(self):
        rows = self.db.setdefault(self.table, [])
        if self.op == "insert":
            if self.db.get("_fail_insert", {}).get(self.table):
                raise RuntimeError("simulated insert failure")
            items = self.payload if isinstance(self.payload, list) else [self.payload]
            out = []
            for it in items:
                it = dict(it)
                it.setdefault("id", str(uuid.uuid4()))
                it.setdefault("created_at", f"t{len(rows):05d}")
                rows.append(it); out.append(copy.deepcopy(it))
            return _Res(out)
        if self.op == "update":
            for r in rows:
                if self._match(r):
                    r.update(copy.deepcopy(self.payload))
            return _Res([copy.deepcopy(r) for r in rows if self._match(r)])
        if self.op == "delete":
            self.db[self.table] = [r for r in rows if not self._match(r)]
            return _Res([])
        sel = [copy.deepcopy(r) for r in rows if self._match(r)]
        if getattr(self, "_cols", None):
            sel = [{c: r.get(c) for c in self._cols} for r in sel]
        if self._order:
            c, d = self._order
            sel.sort(key=lambda r: (r.get(c) is None, r.get(c)), reverse=d)
        if self._limit:
            sel = sel[: self._limit]
        if self._single:
            if not sel:
                raise RuntimeError("single(): no rows")
            return _Res(sel[0])
        if self._maybe:
            return _Res(sel[0] if sel else None)
        return _Res(sel, count=len(sel) if self._count else None)


class _Bucket:
    def __init__(self, store, name):
        self.store, self.name = store, name
    def download(self, path):
        if (self.name, path) not in self.store:
            raise RuntimeError("not found")
        return self.store[(self.name, path)]
    def upload(self, path, content, opts=None):
        self.store[(self.name, path)] = content
    def get_public_url(self, path):
        return f"https://fake/{self.name}/{path}"


class _Storage:
    def __init__(self):
        self.store = {}
    def from_(self, b):
        return _Bucket(self.store, b)


class FakeSB:
    def __init__(self):
        self.db = {}
        self.storage = _Storage()
    def table(self, name):
        return _Q(self.db, name)
