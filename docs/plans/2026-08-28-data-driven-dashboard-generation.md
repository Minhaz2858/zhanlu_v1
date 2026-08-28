# Data-Driven Dashboard Generation — Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Add a DB-agnostic `profile_data` tool so the agent profiles real data before designing dashboards (grounds chart types, excludes empty/sparse columns, surfaces freshness), with ok/empty/error status per table as a free validation byproduct.

**Architecture:** New `DashboardProfiler` service (`services/dashboard_profiler.py`) that runs bounded standard-SQL aggregate queries through the existing QueryService path, plus a `profile_data` tool handler registered in `db_tools.py` beside `describe_schema`. Shape inference (time_series/category/continuous/sparse/empty) drives agent chart-type rules. Zero hardcoded identifiers — every table/column name comes from the caller at runtime and is passed through `quote_ident()`.

**Tech Stack:** Python 3.11, SQLAlchemy, existing QueryService/quote_ident/SchemaService, pytest.

**DB-Agnostic HARD RULE:** No demo table/column names in profiler code, prompts, or defaults. Only standard SQL (`COUNT`, `COUNT(DISTINCT)`, `MIN`, `MAX`, `LIMIT`). Identifiers always quoted via `quote_ident()`. Date detection accepts the union of MySQL/Postgres type names plus ISO-parseable strings.

---

## Task 1: Create DashboardProfiler service — type/shape helpers (pure functions)

**Objective:** Pure, unit-testable helpers for column type classification and shape inference. No DB access yet.

**Files:**
- Create: `backend/app/services/dashboard_profiler.py`
- Test: `backend/tests/services/test_dashboard_profiler_helpers.py`

**Step 1: Write failing test**

```python
"""tests/services/test_dashboard_profiler_helpers.py"""
import pytest

from app.services.dashboard_profiler import (
    classify_column_type,
    infer_shape,
    looks_like_iso_date,
)

# ── classify_column_type ────────────────────────────────────────────────

def test_classify_date_types_union():
    for t in ("date", "datetime", "timestamp", "timestamp without time zone",
              "datetime(6)", "timestamp(3)"):
        assert classify_column_type(t) == "date", t


def test_classify_numeric_types():
    for t in ("int", "bigint", "decimal(18,2)", "numeric(10,4)", "float", "double"):
        assert classify_column_type(t) == "number", t


def test_classify_text_fallback():
    assert classify_column_type("varchar(255)") == "text"
    assert classify_column_type("nvarchar") == "text"
    assert classify_column_type("TEXT") == "text"


def test_classify_unknown_defaults_text():
    assert classify_column_type("geography") == "unknown"
    assert classify_column_type(None) == "unknown"


# ── looks_like_iso_date ─────────────────────────────────────────────────

def test_iso_date_strings():
    assert looks_like_iso_date("2026-08-26")
    assert looks_like_iso_date("2026-08-26T18:19:04")
    assert looks_like_iso_date("2026/08/26")
    assert not looks_like_iso_date("cracked_c5")
    assert not looks_like_iso_date("not-a-date")
    assert not looks_like_iso_date(None)


# ── infer_shape ─────────────────────────────────────────────────────────

def test_shape_empty_when_no_rows():
    assert infer_shape("date", 0, 0, 1.0, 10) == "empty"


def test_shape_sparse_when_null_heavy():
    assert infer_shape("text", 100, 3, 0.9, 100) == "sparse"


def test_shape_time_series_for_date():
    assert infer_shape("date", 100, 50, 0.0, 100) == "time_series"


def test_shape_category_low_cardinality():
    assert infer_shape("text", 100, 3, 0.0, 100) == "category"


def test_shape_continuous_high_cardinality_numeric():
    assert infer_shape("number", 100, 90, 0.0, 100) == "continuous"


def test_shape_high_cardinality_text_is_category():
    # text with many distinct values is still categorical (top-N), not continuous
    assert infer_shape("text", 1000, 500, 0.0, 1000) == "category"
```

**Step 2: Run test to verify failure**

Run: `cd backend && python -m pytest tests/services/test_dashboard_profiler_helpers.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.dashboard_profiler`

**Step 3: Write minimal implementation**

```python
"""services/dashboard_profiler.py

DB-agnostic data profiling for dashboard generation.

HARD RULE: this module contains ZERO hardcoded table/column identifiers.
Every identifier comes from the caller at runtime and is quoted with
``quote_ident`` before being interpolated into SQL. Only standard SQL is
used — no dialect-specific functions (no DATE_FORMAT, to_char, ::date).
"""
from __future__ import annotations

import re
from datetime import datetime

_DATE_TYPES = {"date", "datetime", "timestamp"}
_ISO_DATE_RE = re.compile(
    r"^\d{4}[/-]\d{1,2}[/-]\d{1,2}([T ]\d{1,2}:\d{2}(:\d{2}(\.\d+)?)?)?$"
)


def classify_column_type(sql_type: str | None) -> str:
    """Map a DB column type name to a coarse category.

    Dialect-tolerant: accepts the union of MySQL/Postgres/MSSQL type names.
    Unknown types default to 'unknown' (callers treat as text).
    """
    if not sql_type:
        return "unknown"
    t = sql_type.lower().strip()
    if t in _DATE_TYPES or t.startswith(("date", "datetime", "timestamp")):
        return "date"
    if t.startswith(("int", "bigint", "smallint", "decimal", "numeric",
                     "float", "double", "real", "money")):
        return "number"
    if t.startswith(("char", "varchar", "nchar", "nvarchar", "text", "clob",
                     "string")):
        return "text"
    return "unknown"


def looks_like_iso_date(value: object) -> bool:
    """True if a sample value parses as an ISO-ish date string."""
    if value is None:
        return False
    s = str(value).strip()
    return bool(_ISO_DATE_RE.match(s))


def infer_shape(
    col_type: str,
    row_count: int,
    cardinality: int,
    null_pct: float,
    sample_count: int,
) -> str:
    """Infer a column's analytical shape for chart-type selection.

    Shapes: empty | sparse | time_series | category | continuous
    """
    if row_count <= 0 or cardinality <= 0 or null_pct >= 1.0:
        return "empty"
    if null_pct > 0.5:
        return "sparse"
    if col_type == "date":
        return "time_series"
    if col_type == "number":
        # numeric with low distinct values behaves like a category (e.g. 0/1 flags)
        return "category" if cardinality <= 30 else "continuous"
    # text/unknown: categorical (top-N bar + detail table)
    return "category"
```

**Step 4: Run test to verify pass**

Run: `cd backend && python -m pytest tests/services/test_dashboard_profiler_helpers.py -v`
Expected: PASS (12 passed)

**Step 5: Commit**

```bash
git add backend/app/services/dashboard_profiler.py backend/tests/services/test_dashboard_profiler_helpers.py
git commit -m "feat(profiler): pure type/shape inference helpers"
```

---

## Task 2: Add profiler query building — bounded standard-SQL aggregates

**Objective:** Build the exact profile queries from caller-supplied identifiers using `quote_ident()`. Still no DB execution — pure string building, fully testable.

**Files:**
- Modify: `backend/app/services/dashboard_profiler.py`
- Test: `backend/tests/services/test_dashboard_profiler_helpers.py`

**Step 1: Write failing tests**

```python
def test_build_profile_queries_uses_quote_ident():
    from app.services.dashboard_profiler import build_profile_queries
    qs = build_profile_queries("sales_orders", ["product", "amount", "created_at"])
    joined = "\n".join(qs)
    # identifiers quoted, no raw interpolation of unsafe names
    assert "`sales_orders`" in joined or '"sales_orders"' in joined
    assert "COUNT(DISTINCT `product`)" in joined or "COUNT(DISTINCT \"product\")" in joined
    assert "MIN(`amount`)" in joined or "MIN(\"amount\")" in joined
    assert "MAX(`created_at`)" in joined or "MAX(\"created_at\")" in joined


def test_build_profile_queries_has_row_count():
    from app.services.dashboard_profiler import build_profile_queries
    qs = build_profile_queries("t", ["a"])
    assert any(q.strip().upper().startswith("SELECT COUNT(*)") for q in qs)


def test_build_profile_queries_rejects_unsafe_identifiers():
    from app.services.dashboard_profiler import build_profile_queries
    with pytest.raises(ValueError):
        build_profile_queries("t; DROP TABLE x", ["a"])
    with pytest.raises(ValueError):
        build_profile_queries("t", ["a; DELETE FROM t"])
```

**Step 2: Run to verify failure**

Run: `cd backend && python -m pytest tests/services/test_dashboard_profiler_helpers.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_profile_queries'`

**Step 3: Implement**

```python
from app.services.db.base import quote_ident


def build_profile_queries(table: str, columns: list[str], sample_limit: int = 3) -> list[str]:
    """Build the bounded profile SQL for one table.

    Returns a list of SQL statements: row count, per-column stats
    (cardinality/null/min/max), and sample values. Every identifier is
    validated + quoted by ``quote_ident`` (raises ValueError on unsafe names).
    Only standard SQL — dialect-agnostic by construction.
    """
    tq = quote_ident(table, "default")  # dialect fixed at execution time
    # NOTE: dialect-aware quoting is applied by _run at runtime; here we build
    # with a placeholder dialect and let Task 4 swap in the real one.
    queries = [f"SELECT COUNT(*) AS row_count FROM {tq}"]
    for col in columns:
        cq = quote_ident(col, "default")
        queries.append(
            f"SELECT COUNT(DISTINCT {cq}) AS cardinality, "
            f"COUNT({cq}) AS non_null, "
            f"MIN({cq}) AS min_value, MAX({cq}) AS max_value FROM {tq}"
        )
        queries.append(f"SELECT {cq} AS sample_value FROM {tq} LIMIT {sample_limit}")
    return queries
```

Note: `quote_ident(name, "default")` currently returns double-quoted ANSI for unknown dialects — acceptable for SQLite tests. Task 4 passes the real dialect from the KB.

**Step 4: Run to verify pass**

Run: `cd backend && python -m pytest tests/services/test_dashboard_profiler_helpers.py -v`
Expected: PASS (15 passed)

**Step 5: Commit**

```bash
git add backend/app/services/dashboard_profiler.py backend/tests/services/test_dashboard_profiler_helpers.py
git commit -m "feat(profiler): bounded profile query builder with quote_ident"
```

---

## Task 3: Profile execution + shape assembly (SQLite-backed unit tests)

**Objective:** Execute profile queries against a real (throwaway) DB and assemble the compact profile JSON. Test with in-memory SQLite so no external DB is needed. Add the DB-agnostic assertion: source must not contain demo identifiers.

**Files:**
- Modify: `backend/app/services/dashboard_profiler.py`
- Test: `backend/tests/services/test_dashboard_profiler_helpers.py` (extend) + new `backend/tests/services/test_dashboard_profiler_exec.py`

**Step 1: Write failing test**

```python
"""tests/services/test_dashboard_profiler_exec.py"""
import sqlite3

import pytest

from app.services.dashboard_profiler import profile_engine


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "profile.db"
    con = sqlite3.connect(p)
    con.executescript(
        """
        CREATE TABLE sales_orders (
            product TEXT,
            amount REAL,
            created_at TEXT,
            region TEXT
        );
        INSERT INTO sales_orders VALUES
            ('C5 Resin', 120.5, '2026-01-05', 'East'),
            ('C5 Resin', 99.0, '2026-02-10', 'East'),
            ('Isoprene', 250.0, '2026-03-15', 'West'),
            ('C5 Resin', NULL, '2026-04-20', NULL),
            ('C9 Resin', 88.0, '2026-05-25', 'East');
        """
    )
    con.commit()
    con.close()
    return str(p)


def test_profile_engine_returns_expected_shape(db_path):
    result = profile_engine(db_path, "sales_orders", ["product", "amount", "created_at", "region"])
    assert result["table"] == "sales_orders"
    assert result["row_count"] == 5
    assert result["status"] == "ok"
    cols = {c["name"]: c for c in result["columns"]}
    # product: 4 distinct, 5 non-null -> category
    assert cols["product"]["cardinality"] == 4
    assert cols["product"]["null_pct"] == 0.0
    assert cols["product"]["shape"] == "category"
    # amount: 5 rows, 1 null -> 0.2, numeric high-card? no: 4 distinct -> category
    assert cols["amount"]["null_pct"] == pytest.approx(0.2)
    assert cols["amount"]["shape"] == "category"
    # created_at parses as ISO date -> time_series
    assert cols["created_at"]["shape"] == "time_series"
    assert cols["created_at"]["min"] == "2026-01-05"
    # region: 2 distinct + 1 null (0.2) -> category, top_values present
    assert len(cols["region"]["top_values"]) >= 2


def test_profile_engine_missing_table_reports_error(db_path):
    result = profile_engine(db_path, "no_such_table", ["a"])
    assert result["status"] == "error"
    assert result["error_message"]


def test_profile_engine_unknown_column_reports_error(db_path):
    result = profile_engine(db_path, "sales_orders", ["nope"])
    assert result["status"] == "error"
    assert result["error_message"]
```

**Step 2: Run to verify failure**

Run: `cd backend && python -m pytest tests/services/test_dashboard_profiler_exec.py -v`
Expected: FAIL — `ImportError: cannot import name 'profile_engine'`

**Step 3: Implement**

Add to `services/dashboard_profiler.py`:

```python
import sqlite3

from app.services.db.base import quote_ident


def _safe_quote(name: str) -> str:
    # SQLite-safe ANSI quoting via the shared validator
    return quote_ident(name, "sqlite")


def profile_engine(
    db_uri_or_path: str,
    table: str,
    columns: list[str],
    sample_limit: int = 3,
    max_columns: int = 20,
) -> dict:
    """Profile one table through a raw DBAPI connection (sqlite/engine).

    Used by the tool handler with the KB's real connector; unit tests pass
    a sqlite path. Returns the compact profile dict with status/error fields.
    """
    cols = columns[:max_columns]
    table_q = _safe_quote(table)
    base = {
        "table": table,
        "row_count": 0,
        "status": "ok",
        "error_message": None,
        "columns": [],
    }
    try:
        con = sqlite3.connect(db_uri_or_path)
    except Exception as exc:
        base.update(status="error", error_message=str(exc)[:200])
        return base
    try:
        cur = con.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {table_q}")
        base["row_count"] = int(cur.fetchone()[0])
        for col in cols:
            col_q = _safe_quote(col)
            try:
                cur.execute(
                    f"SELECT COUNT(DISTINCT {col_q}), COUNT({col_q}), "
                    f"MIN({col_q}), MAX({col_q}) FROM {table_q}"
                )
                cardinality, non_null, mn, mx = cur.fetchone()
                cur.execute(f"SELECT {col_q} FROM {table_q} LIMIT {sample_limit}")
                samples = [r[0] for r in cur.fetchall()]
                null_pct = 1.0 - (non_null / base["row_count"]) if base["row_count"] else 1.0
                # infer type: use sample values + name heuristics (test DB has no
                # type metadata here; the tool handler passes real types via
                # describe_table — see Task 4)
                col_type = "text"
                base["columns"].append({
                    "name": col,
                    "type": col_type,
                    "cardinality": int(cardinality or 0),
                    "null_pct": round(null_pct, 3),
                    "min": mn,
                    "max": mx,
                    "top_values": [v for v in samples if v is not None][:3],
                    "shape": infer_shape(
                        col_type, base["row_count"], int(cardinality or 0),
                        null_pct, len([v for v in samples if v is not None]),
                    ),
                })
            except Exception as exc:
                base.update(status="error", error_message=f"column {col}: {exc}"[:200])
                break
    except Exception as exc:
        base.update(status="error", error_message=str(exc)[:200])
    finally:
        con.close()
    if base["row_count"] == 0 and base["status"] == "ok":
        base["status"] = "empty"
    return base
```

**Step 4: Run to verify pass**

Run: `cd backend && python -m pytest tests/services/test_dashboard_profiler_exec.py -v`
Expected: PASS (3 passed)

**Step 5: Commit**

```bash
git add backend/app/services/dashboard_profiler.py backend/tests/services/test_dashboard_profiler_exec.py
git commit -m "feat(profiler): execute profile queries and assemble JSON"
```

---

## Task 4: Dialect-aware profile execution via QueryService (DB-agnostic core)

**Objective:** Replace the sqlite-only path with the KB connector so profiling works against ANY bound datasource (MySQL/Postgres/MSSQL/Oracle/SQLite). Uses the existing `get_connector` + `quote_ident(dialect)` machinery — zero new DB code.

**Files:**
- Modify: `backend/app/services/dashboard_profiler.py`
- Test: extend `backend/tests/services/test_dashboard_profiler_exec.py`

**Step 1: Write failing test**

```python
def test_profile_uses_dialect_quote_ident(monkeypatch):
    """The executor must quote with the KB's dialect, not a hardcoded one."""
    import app.services.dashboard_profiler as mod

    seen = {}
    class FakeConn:
        def __init__(self, dialect): self.dialect = dialect
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, max_rows=5, timeout_s=10):
            seen["sql"] = sql
            if sql.strip().upper().startswith("SELECT COUNT(*)"):
                return [{"row_count": 3}]
            if "DISTINCT" in sql.upper():
                return [{"cardinality": 2, "non_null": 3, "min_value": "a", "max_value": "c"}]
            return [{"sample_value": "a"}, {"sample_value": "b"}]

    mod.get_connector = lambda kb: FakeConn("mysql")
    mod._load_kb = lambda db, kb_id: type("KB", (), {"db_type": "mysql"})()

    result = mod.profile_kb(None, "kb_1", "tbl", ["col_a"])
    assert "`tbl`" in seen["sql"] and "`col_a`" in seen["sql"]
    assert result["status"] == "ok"
    assert result["row_count"] == 3
```

**Step 2: Run to verify failure**

Run: `cd backend && python -m pytest tests/services/test_dashboard_profiler_exec.py -v`
Expected: FAIL — `ImportError: cannot import name 'profile_kb'`

**Step 3: Implement**

Replace the sqlite-only `profile_engine` body with a thin DBAPI wrapper, and add:

```python
def profile_kb(db: Session, kb_id: str, table: str, columns: list[str],
               sample_limit: int = 3, max_columns: int = 20,
               timeout_s: int = 12) -> dict:
    """Profile a table on a bound KB through the shared connector path.

    DB-agnostic: dialect comes from the KB; identifiers are quoted with the
    KB's dialect; only standard SQL is issued. Never raises — returns a
    profile dict with status/error fields.
    """
    from app.services.db.base import get_connector
    kb = _load_kb(db, kb_id)
    dialect = kb.db_type or "default"
    cols = columns[:max_columns]
    base = {"table": table, "row_count": 0, "status": "ok",
            "error_message": None, "columns": []}
    try:
        with get_connector(kb) as conn:
            tq = quote_ident(table, dialect)
            cur = conn.execute(f"SELECT COUNT(*) FROM {tq}", max_rows=5, timeout_s=timeout_s)
            base["row_count"] = int((cur or [{}])[0].get("row_count", 0) or 0)
            for col in cols:
                cq = quote_ident(col, dialect)
                try:
                    stats = conn.execute(
                        f"SELECT COUNT(DISTINCT {cq}) AS cardinality, "
                        f"COUNT({cq}) AS non_null, MIN({cq}) AS min_value, "
                        f"MAX({cq}) AS max_value FROM {tq}",
                        max_rows=5, timeout_s=timeout_s,
                    )
                    s0 = (stats or [{}])[0]
                    cardinality = int(s0.get("cardinality") or 0)
                    non_null = int(s0.get("non_null") or 0)
                    samples = conn.execute(
                        f"SELECT {cq} AS sample_value FROM {tq} LIMIT {sample_limit}",
                        max_rows=sample_limit, timeout_s=timeout_s,
                    )
                    sample_vals = [r.get("sample_value") for r in (samples or [])]
                    null_pct = 1.0 - (non_null / base["row_count"]) if base["row_count"] else 1.0
                    col_type = _infer_col_type(db, kb_id, table, col, sample_vals)
                    base["columns"].append({
                        "name": col,
                        "type": col_type,
                        "cardinality": cardinality,
                        "null_pct": round(null_pct, 3),
                        "min": s0.get("min_value"),
                        "max": s0.get("max_value"),
                        "top_values": [v for v in sample_vals if v is not None][:3],
                        "shape": infer_shape(col_type, base["row_count"], cardinality,
                                             null_pct, len(sample_vals)),
                    })
                except Exception as exc:
                    base.update(status="error", error_message=f"column {col}: {exc}"[:200])
                    break
    except Exception as exc:
        base.update(status="error", error_message=str(exc)[:200])
    if base["row_count"] == 0 and base["status"] == "ok":
        base["status"] = "empty"
    return base


def _load_kb(db: Session, kb_id: str):
    from app.models.knowledge_base import KnowledgeBase
    kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id,
                                        KnowledgeBase.is_deleted == False).first()
    if not kb:
        raise ValueError(f"KnowledgeBase not found: {kb_id}")
    return kb


def _infer_col_type(db, kb_id, table, col, samples) -> str:
    """Best-effort type from schema metadata, falling back to sample values."""
    try:
        from app.services.db import SchemaService
        svc = SchemaService(db)
        desc = svc.describe_table(kb_id, table)
        for c in (desc.get("columns") or []):
            if (c.get("name") or "").lower() == col.lower():
                return classify_column_type(c.get("type") or c.get("data_type"))
    except Exception:
        pass
    if any(looks_like_iso_date(v) for v in samples):
        return "date"
    return "text"
```

**Step 4: Run to verify pass**

Run: `cd backend && python -m pytest tests/services/test_dashboard_profiler_exec.py -v`
Expected: PASS (4 passed)

**Step 5: Commit**

```bash
git add backend/app/services/dashboard_profiler.py backend/tests/services/test_dashboard_profiler_exec.py
git commit -m "feat(profiler): dialect-aware KB profiling via connector path"
```

---

## Task 5: Register `profile_data` tool handler

**Objective:** Expose `profile_data` to the data agent beside describe_schema, with KB resolution + policy checks. Max 8 tables per call (schema cap consistency), max 20 columns per table.

**Files:**
- Modify: `backend/app/services/tool_handlers/db_tools.py`
- Test: `backend/tests/services/dashboard_app/test_dashboard_describe_schema_cap.py` (add cap consistency test) OR new `backend/tests/services/test_profile_data_tool.py`

**Step 1: Write failing test**

```python
"""tests/services/test_profile_data_tool.py"""
import pytest

from app.services.tool_handlers.db_tools import _profile_data


async def test_profile_data_rejects_unknown_kb():
    result = await _profile_data(
        {"data_source_id": "missing-kb", "table": "orders", "columns": ["amount"]},
        db=None, user_id="u1", context={},
    )
    assert result.get("success") is False
    assert "data source" in (result.get("error") or "").lower()


async def test_profile_data_schema_validates_columns():
    from app.services.tool_handlers.db_tools import PROFILE_DATA_SCHEMA
    props = PROFILE_DATA_SCHEMA["function"]["parameters"]["properties"]
    assert "table" in props
    assert "columns" in props
    assert "data_source_id" in props
```

**Step 2: Run to verify failure**

Run: `cd backend && python -m pytest tests/services/test_profile_data_tool.py -v`
Expected: FAIL — `ImportError: cannot import name '_profile_data'`

**Step 3: Implement**

In `db_tools.py`, after `_describe_schema` (reuse its `_require_kb_id` + `_resolve_user_policy` helpers):

```python
async def _profile_data(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    """Profile real data in a bound data source before designing dashboards.

    DB-agnostic: works against any bound datasource. Returns per-table
    row counts, per-column cardinality/null-pct/min/max/top-values, and a
    shape hint (time_series/category/continuous/sparse/empty) plus an
    ok/empty/error status the agent uses to avoid building on bad tables.
    """
    kb_id, err = _require_kb_id(args, context)
    if err:
        return err
    table = args.get("table")
    columns = args.get("columns") or []
    max_tables = min(int(args.get("max_tables", 8)), 8)

    policy = _resolve_user_policy(db, user_id, context)
    if policy.is_kb_fully_denied(kb_id):
        return {"success": False, "error": "Access to this data source is restricted."}

    try:
        if table:
            tables = [table]
        else:
            from app.services.db import SchemaService
            svc = SchemaService(db)
            listing = svc.list_tables(kb_id)
            tables = (listing.get("tables") or [])[:max_tables]
    except Exception as exc:
        return {"success": False, "error": str(exc)[:300]}

    results = []
    for t in tables:
        try:
            cols = columns
            if not cols:
                from app.services.db import SchemaService
                svc = SchemaService(db)
                desc = svc.describe_table(kb_id, t)
                cols = [(c.get("name") or "") for c in (desc.get("columns") or [])][:20]
            prof = profile_kb(db, kb_id, t, cols)
            results.append(prof)
        except Exception as exc:
            results.append({"table": t, "status": "error",
                            "error_message": str(exc)[:200], "row_count": 0, "columns": []})

    return {"success": True, "tables": results}
```

Register in the tuple at the bottom (add after describe_schema):

```python
("profile_data", PROFILE_DATA_SCHEMA, _profile_data,
 "Profile real data (row counts, cardinality, null ratio, shapes) of a bound data source."),
```

Add the schema next to `DESCRIBE_SCHEMA_SCHEMA`:

```python
PROFILE_DATA_SCHEMA = {
    "type": "function",
    "function": {
        "name": "profile_data",
        "description": (
            "Profile real data in a bound data source to ground dashboard design. "
            "Returns per-table row count and per-column cardinality, null ratio, "
            "min/max, top sample values, and a shape hint "
            "(time_series/category/continuous/sparse/empty) plus an ok/empty/error "
            "status. Call with a `table` you intend to query, optionally listing "
            "`columns`; omit `columns` to profile the first 20 columns. If a table "
            "comes back empty or error, do NOT build a dashboard on it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "data_source_id": {"type": "string",
                                   "description": "The bound data source id."},
                "table": {"type": "string",
                          "description": "Optional. Profile just this table."},
                "columns": {"type": "array", "items": {"type": "string"},
                            "description": "Optional. Limit to these columns."},
                "max_tables": {"type": "integer", "default": 8,
                               "description": "Cap on tables when no table given."},
            },
            "required": ["data_source_id"],
        },
    },
}
```

**Step 4: Run to verify pass**

Run: `cd backend && python -m pytest tests/services/test_profile_data_tool.py -v`
Expected: PASS (2 passed)

**Step 5: Commit**

```bash
git add backend/app/services/tool_handlers/db_tools.py backend/tests/services/test_profile_data_tool.py
git commit -m "feat(tools): register profile_data beside describe_schema"
```

---

## Task 6: Agent prompt — data-driven chart rules

**Objective:** Teach the agent to call `profile_data` before building and to pick chart types from profile shapes.

**Files:**
- Modify: `backend/app/services/agent_prompts.py`
- Test: `backend/tests/services/agent_prompts/` (existing prompt tests)

**Step 1: Add the rule to the BUILD step** (after the DATA CONTRACT paragraph, before the "4. ITERATE" item):

```text
DATA-DRIVEN CHART RULES (HARD RULE — call `profile_data` before building):
After describe_schema and before create_fullstack_dashboard, call
`profile_data(table=...)` for each table you will query. Use the profile to
choose chart types and to exclude unusable data:
- time_series column -> line/area trend chart
- category (2-8 distinct values) -> bar or donut breakdown
- category (>8 distinct values) -> top-N bar + detail table
- continuous numeric -> KPI card (with delta) or histogram
- sparse (>50% null) or empty column -> EXCLUDE it; tell the user why
- table status "empty" or "error" -> do NOT build on it; propose the closest
  real alternative or ask for another data source
The profile's min/max on a date column gives you freshness: if the latest
date is stale, say so and prefer the newest slice.
```

**Step 2: Run the existing prompt tests**

Run: `cd backend && python -m pytest tests/services/agent_prompts/ -v`
Expected: PASS (existing tests still green)

**Step 3: Commit**

```bash
git add backend/app/services/agent_prompts.py
git commit -m "feat(prompts): data-driven chart rules via profile_data"
```

---

## Task 7: Backend config guard — DB-agnostic sanity test

**Objective:** Guard against accidental hardcoding. A test asserts no demo identifiers appear in the profiler source or the prompt rule.

**Files:**
- Test: `backend/tests/services/test_dashboard_profiler_helpers.py` (extend)

**Step 1: Add the guard test**

```python
def test_profiler_source_has_no_demo_identifiers():
    """HARD RULE: profiler must be DB-agnostic — no demo table/column names."""
    import pathlib
    src = pathlib.Path(__file__).parents[2] / "app" / "services" / "dashboard_profiler.py"
    text = src.read_text()
    for bad in ("erp_v_sale_orderentry", "PLANDATE", "FALLAMOUNT", "FDATE", "forgid"):
        assert bad not in text, f"hardcoded identifier leaked into profiler: {bad}"


def test_prompt_rule_mentions_profile_data():
    """The build prompt must instruct calling profile_data before building."""
    from app.services import agent_prompts
    blob = agent_prompts.FULLSTACK_DASHBOARD_BUILD_GUIDANCE if hasattr(
        agent_prompts, "FULLSTACK_DASHBOARD_BUILD_GUIDANCE") else str(agent_prompts.__dict__)
    assert "profile_data" in blob
```

**Step 2: Run to verify**

Run: `cd backend && python -m pytest tests/services/test_dashboard_profiler_helpers.py -v`
Expected: PASS (17 passed)

**Step 3: Commit**

```bash
git add backend/tests/services/test_dashboard_profiler_helpers.py
git commit -m "test(profiler): DB-agnostic hardcoding guard"
```

---

## Task 8: e2e verification against the bound datasource

**Objective:** Prove the tool works against the real bound Ecisco datasource and that it would have guided the existing sales dashboard.

**Files:**
- None (verification only)

**Step 1: Run the profiler via the tool handler against the real KB**

```bash
cd backend && docker exec zhanlu-backend python -c "
import asyncio
from app.database import SessionLocal
from app.services.tool_handlers.db_tools import _profile_data

db = SessionLocal()
async def main():
    r = await _profile_data(
        {'data_source_id': 'b1b9145d-5b6b-4c0e-ba82-919dde4620d7',
         'table': 'erp_v_sale_orderentry', 'columns': ['PLANDATE', 'FALLAMOUNT', 'org_name', 'material_name']},
        db, '89c8dfc2-152c-423d-bc6d-44cb9e9619fd', {'conversation_id': 'x'},
    )
    import json; print(json.dumps(r, ensure_ascii=False, default=str, indent=1)[:1800])
asyncio.run(main())
"
```

Expected: `success: true`, `row_count: 14416`, PLANDATE shape `time_series` with min/max spanning 2018→2026, org_name/materials `category` with top_values, all status `ok`.

**Step 2: Verify a bad table returns error**

```bash
docker exec zhanlu-backend python -c "
import asyncio
from app.database import SessionLocal
from app.services.tool_handlers.db_tools import _profile_data
db = SessionLocal()
async def main():
    r = await _profile_data({'data_source_id': 'b1b9145d-5b6b-4c0e-ba82-919dde4620d7', 'table': 'no_such_table'}, db, 'u', {})
    print(r.get('success'), r.get('tables', [{}])[0].get('status'), r.get('tables', [{}])[0].get('error_message', '')[:80])
asyncio.run(main())
"
```

Expected: `True error <message>`

**Step 3: Regenerate the sales dashboard with the prompt rule in force**

Ask the agent to rebuild `sales-performance-dashboard-v2` (or create a scratch one) and confirm the build flow now calls `profile_data` before `create_fullstack_dashboard`. Verify the org_split chart is a comparison bar (2 orgs) and header shows freshness when a date column is present.

**Step 4: Full test suite**

```bash
cd backend && python -m pytest tests/services/test_dashboard_profiler_helpers.py tests/services/test_dashboard_profiler_exec.py tests/services/test_profile_data_tool.py -v
```

Expected: all pass.

**Step 5: Commit any fixes**

```bash
git add -A && git commit -m "fix(profiler): e2e verification adjustments"
```

---

## Verification checklist

- [ ] `profile_data` registered and callable via the tool handler against a real KB
- [ ] Shape inference correct on real data (PLANDATE → time_series, org → category)
- [ ] Bad table/column returns `status: error` with message, never raises
- [ ] 0-row table returns `status: empty`
- [ ] No demo identifiers anywhere in `dashboard_profiler.py` or prompt rules
- [ ] Existing prompt/dashboard tests still pass
- [ ] Agent build flow calls `profile_data` before `create_fullstack_dashboard`; it
      never designs on an empty/error table (no empty frontend at design time)

## Notes

- The plan deliberately implements validation as a byproduct (per-table status
  fields) instead of a separate post-build pass — evidence: 0/53 widgets broken
  across all dashboards (2026-08-28 audit). Promote to a full pass only if a
  real failure appears in production.
- Profiler never blocks a build: any failure degrades to `status: error` the
  agent can act on.
- Freshness stamp in the header is deferred (YAGNI) — the profile min/max is
  available to the agent, so it can state freshness in its response instead.
- No frontend Empty-card work needed (user decision, 2026-08-28): the template's
  Empty component already renders `{error || 'No data yet — waiting for the
  first refresh'}` (widgets/index.jsx:237) and the widget wrapper already passes
  `data.error` through (widgets/index.jsx:913). More importantly, the profiling
  gate PREVENTS empty widgets at design time — the agent analyzes data first,
  decides the design, and only builds on tables that return `ok`. Empty cards
  remain only as a runtime fallback for unexpected failures (DB down, table
  dropped after build), which the existing code already handles.
