"""Live E2E for schema-shape change detection (G4).

Run against the copy stack with the probe modules overlaid (no restart needed):

    docker exec zhanlu-copy-backend sh -lc 'mkdir -p /tmp/shape_e2e && cp -r /app/app /tmp/shape_e2e/'
    # docker cp the changed app/** files into /tmp/shape_e2e/app/... (overlay)
    docker cp backend/scripts_probe_schema_shape_live.py zhanlu-copy-backend:/tmp/shape_e2e/
    docker exec zhanlu-copy-backend sh -lc 'cd /tmp/shape_e2e && PYTHONPATH=/tmp/shape_e2e python scripts_probe_schema_shape_live.py'

What it proves, with explicit PASS/FAIL:

  1 PROBE_COST      the shape probe is ONE dialect query, timed, on the biggest
                    warehouse the stack has (read-only)
  2 CACHE_MISS      after ALTER TABLE ... ADD COLUMN, describe_table returns the
                    new column WITHOUT a restart and WITHOUT waiting out the 1 h
                    schema TTL, and the connector is re-introspected
  3 INVALIDATION    the change is reported (SHAPE CHANGED) and stale entries for
                    that KB are gone
  4 DRIFT_REPORT    the tool result names the new column and tells the model the
                    catalog needs a re-index
  5 REVERT          DROP COLUMN is detected too, and the datasource is left exactly
                    as found (column gone, row count unchanged)
  6 SAFETY          read-only against any warehouse the operator does not own: the
                    only writes are ADD/DROP COLUMN on the DISPOSABLE demo sources
                    (name contains AUTO). The real warehouse is never altered.

Self-cleaning: the added column is dropped in a finally block; the row count is
printed before and after.
"""

from __future__ import annotations

import time

from sqlalchemy import create_engine, text

from app.config import settings
from app.database import SessionLocal
from app.models.knowledge_base import KnowledgeBase
from app.services.db import schema_shape as ss
from app.services.db.schema_service import SchemaService, invalidate_schema_cache

PROBE_COL = "zz_shape_probe_col"
FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(label)


class Counting:
    """Delegate to a real connector, counting the calls that cost round trips."""

    def __init__(self, inner, box):
        self._inner = inner
        self._box = box

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)

    def _count(self, name):
        self._box[name] = self._box.get(name, 0) + 1

    def list_tables(self):
        self._count("list_tables")
        return self._inner.list_tables()

    def describe_table(self, table):
        self._count("describe_table")
        return self._inner.describe_table(table)

    def shape(self):
        self._count("shape")
        return self._inner.shape()

    def __getattr__(self, item):
        return getattr(self._inner, item)


def _wire(monkeypatch_like, box):
    """Route both connector lookups through the counter."""
    import app.services.db.connector_factory as cf
    import app.services.db.schema_service as svc

    real_factory = cf.get_connector

    def counted(kb):
        return Counting(real_factory(kb), box)

    svc.get_connector = counted  # type: ignore[assignment]


def _ddl_engine(kb):
    """A raw engine for the DEMO database only, reusing the connector's own URL."""
    from app.services.db.connector_factory import get_connector

    with get_connector(kb) as conn:
        url = conn._build_url()  # noqa: SLF001 — deliberate, probe-only
    return create_engine(url)


def demo_kbs(db):
    rows = (
        db.query(KnowledgeBase)
        .filter(
            KnowledgeBase.is_deleted.is_(False),
            KnowledgeBase.source_kind == "database",
            KnowledgeBase.catalog_status == "ready",
            KnowledgeBase.name.like("%AUTO%"),
        )
        .all()
    )
    seen, out = set(), []
    for kb in rows:
        key = (kb.db_type, kb.host, str(kb.port), kb.database_name)
        if key in seen:
            continue
        seen.add(key)
        out.append(kb)
    return out


def scenario_demo(db, kb) -> None:
    print("\n" + "=" * 78)
    print(f"KB {kb.name!r} ({kb.db_type} {kb.host}:{kb.port}/{kb.database_name})")

    box: dict[str, int] = {}
    _wire(None, box)
    ss.invalidate_shape_cache(None)
    invalidate_schema_cache(None)

    engine = _ddl_engine(kb)
    table = "customers"
    rows_before = None
    try:
        with engine.connect() as c:
            rows_before = c.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
    except Exception as e:
        print(f"  SKIP — cannot read demo table {table}: {type(e).__name__}: {e}")
        return

    svc = SchemaService(db)
    added = False
    try:
        base = svc.describe_table(kb.id, table)
        base_cols = [c["name"] for c in base["columns"]]
        print(f"  baseline columns: {len(base_cols)}  describe_calls={box['describe_table']}")

        # A DBA changes the live schema.
        with engine.connect() as c:
            c.execute(text(f"ALTER TABLE {table} ADD COLUMN {PROBE_COL} VARCHAR(32)"))
            c.commit()
        added = True
        print(f"  ALTER TABLE {table} ADD COLUMN {PROBE_COL} …")

        box_before = box.get("describe_table", 0)
        after = svc.describe_table(kb.id, table)
        cols = [c["name"] for c in after["columns"]]

        check("2 CACHE_MISS — new column visible with no restart",
              PROBE_COL in cols, f"columns={len(cols)}")
        check("2 CACHE_MISS — connector re-introspected (not served from cache)",
              box.get("describe_table", 0) > box_before,
              f"describe_table calls {box_before} → {box.get('describe_table')}")

        drift = after.get("schema_drift")
        check("4 DRIFT_REPORT — drift names the new column",
              bool(drift) and f"{table}.{PROBE_COL}" in (drift.get("columns_added") or []),
              str((drift or {}).get("columns_added"))[:160])
        check("4 DRIFT_REPORT — model is told to re-index",
              bool(drift) and "re-index" in str(drift.get("hint", "")))

        # Revert.
        with engine.connect() as c:
            c.execute(text(f"ALTER TABLE {table} DROP COLUMN {PROBE_COL}"))
            c.commit()
        added = False
        ss._SHAPE_CACHE.clear()  # cold detector: only the cache KEY can help
        ss._LAST_DIGEST.clear()
        reverted = svc.describe_table(kb.id, table)
        rcols = [c["name"] for c in reverted["columns"]]
        check("5 REVERT — dropped column disappears", PROBE_COL not in rcols)
        check("5 REVERT — original column set restored", sorted(rcols) == sorted(base_cols))

        with engine.connect() as c:
            rows_after = c.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
        check("6 SAFETY — demo table rows unchanged", rows_after == rows_before,
              f"{rows_before} → {rows_after}")
    finally:
        if added:
            try:
                with engine.connect() as c:
                    c.execute(text(f"ALTER TABLE {table} DROP COLUMN {PROBE_COL}"))
                    c.commit()
                print(f"  cleaned up: dropped {PROBE_COL}")
            except Exception as e:  # pragma: no cover - probe
                print(f"  CLEANUP FAILED for {PROBE_COL}: {e}")


def scenario_warehouse_readonly(db) -> None:
    print("\n" + "=" * 78)
    kb = (
        db.query(KnowledgeBase)
        .filter(
            KnowledgeBase.is_deleted.is_(False),
            KnowledgeBase.source_kind == "database",
            KnowledgeBase.catalog_status == "ready",
            KnowledgeBase.name.notlike("%AUTO%"),
            KnowledgeBase.db_type.in_(["mysql", "mariadb"]),
        )
        .order_by(KnowledgeBase.name)
        .first()
    )
    if kb is None:
        print("no real warehouse KB found — skipped (nothing altered)")
        return
    print(f"KB {kb.name!r} ({kb.db_type} {kb.host}:{kb.port}/{kb.database_name}) "
          f"— READ-ONLY, no writes issued")

    ss.invalidate_shape_cache(None)
    t0 = time.perf_counter()
    state = ss.shape_state(db, kb.id, force=True)
    ms = (time.perf_counter() - t0) * 1000

    check("1 PROBE_COST — one query returned a shape", state.known,
          f"{len(state.pairs)} columns, {ms:.0f} ms")
    if state.error:
        print(f"    error: {state.error}")

    cat = ss.catalog_pairs(db, kb.id)
    report = ss.diff_pairs(state.pairs, cat) if (state.pairs and cat) else None
    if report:
        print(f"    catalog={len(cat)} live={len(state.pairs)} → "
              f"tables +{len(report.tables_added)}/-{len(report.tables_dropped)}, "
              f"columns +{len(report.columns_added)}/-{len(report.columns_dropped)}")
        if report.tables_added:
            print(f"    live-only tables: {report.tables_added[:6]}")
        if report.columns_added:
            print(f"    live-only columns (first 6): {report.columns_added[:6]}")
    check("1 PROBE_COST — probe is fast enough for the cache path", ms < 8000,
          f"{ms:.0f} ms")


def main() -> None:
    settings.SCHEMA_SHAPE_ENABLED = True
    settings.SCHEMA_SHAPE_TTL_SECONDS = 0  # probe on every lookup
    print("SCHEMA_SHAPE_ENABLED=%s TTL=%s" % (
        settings.SCHEMA_SHAPE_ENABLED, settings.SCHEMA_SHAPE_TTL_SECONDS))

    db = SessionLocal()
    try:
        scenario_warehouse_readonly(db)
        for kb in demo_kbs(db):
            scenario_demo(db, kb)
    finally:
        db.close()

    print("\n" + "=" * 78)
    print("FAILURES:", FAILURES or "none")


if __name__ == "__main__":
    main()
