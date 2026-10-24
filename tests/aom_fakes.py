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
            if len({tuple(sorted(it)) for it in items}) > 1:
                raise RuntimeError("PGRST102 All object keys must match")   # ako PostgREST bulk insert
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
            # trigger analyza_om_variants_chran_delete: chránený riadok DELETE potichu preskočí
            self.db[self.table] = [r for r in rows if not self._match(r) or r.get("is_protected")]
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


class APIError(Exception):
    """Tvar postgrest.exceptions.APIError (code + message)."""
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


class _Rpc:
    def __init__(self, sb, name, params):
        self.sb, self.name, self.params = sb, name, params
    def execute(self):
        self.sb.rpc_calls.append(self.name)
        if self.name != "aom_replace_engine_result" or not self.sb.aom_atomic_rpc:
            raise APIError("PGRST202", f"Could not find the function public.{self.name} in the schema cache")
        snapshot = copy.deepcopy(self.sb.db)
        try:
            return _Res(self.sb._aom_replace_engine_result(**self.params))
        except Exception:
            self.sb.db.clear(); self.sb.db.update(snapshot)    # transakcia → rollback
            raise


class FakeSB:
    def __init__(self, aom_atomic_rpc=False):
        self.db = {}
        self.storage = _Storage()
        self.aom_atomic_rpc = aom_atomic_rpc   # True = migrácia 20261024_aom_atomic_results aplikovaná
        self.rpc_calls = []
    def table(self, name):
        return _Q(self.db, name)
    def rpc(self, name, params=None):
        return _Rpc(self, name, params or {})

    def _aom_replace_engine_result(self, p_analyza_id, p_expected_updated_at, p_rows,
                                   p_sim_results, p_econ_results, p_selected_variant_id):
        """Emulácia migrations/20261024_aom_atomic_results.sql."""
        if not isinstance(p_rows, list) or not p_rows:
            raise APIError("P0001", "AOM: prázdny výsledok — nič sa nenahrádza")
        a = next(r for r in self.db["analyza_om"] if str(r["id"]) == str(p_analyza_id))
        if p_expected_updated_at is not None and a.get("updated_at") != p_expected_updated_at:
            raise APIError("40001", "AOM: analýza sa počas výpočtu zmenila, spusti prepočet znova")
        _Q(self.db, "analyza_om_variants").eq("analyza_id", p_analyza_id).delete().execute()
        defaults = {"fve_tilt_deg": 25, "fve_azimuth_deg": 0, "fve_topology": "south", "bess_kwh": 0,
                    "bess_kw": 0, "bess_arbitrage_enabled": False}
        for i, r in enumerate(copy.deepcopy(p_rows), start=1):
            r = {**defaults, **{k: v for k, v in r.items() if v is not None}}
            r.setdefault("position", i)
            r["analyza_id"] = p_analyza_id
            _Q(self.db, "analyza_om_variants").insert(r).execute()
        sel = p_selected_variant_id if any(
            str(v["id"]) == str(p_selected_variant_id) and str(v["analyza_id"]) == str(p_analyza_id)
            for v in self.db["analyza_om_variants"]) else None
        a.update({"status": "completed", "selected_variant_id": sel,
                  "sim_results": copy.deepcopy(p_sim_results), "econ_results": copy.deepcopy(p_econ_results),
                  "updated_at": f"rpc-{len(self.rpc_calls)}"})
        return {"ok": True, "variants_count": len(p_rows), "selected_variant_id": sel}
