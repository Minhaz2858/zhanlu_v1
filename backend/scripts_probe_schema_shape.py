"""Probe: how expensive is a SCHEMA-SHAPE fingerprint, and is the catalog stale?

Answers the one question G4 depends on, on the LIVE stack: can the platform notice
that a datasource's SHAPE changed (a table or column added/dropped/retyped) without
paying for a full re-index?

Measured per database KnowledgeBase:

  * ``conn.list_tables()``                 — 1 query: table NAMES only (baseline)
  * one-query information_schema scan      — the candidate cheap probe: every
                                             (table, column, type) in one round trip
  * ``Inspector.get_multi_columns()``      — SQLAlchemy 2.0 bulk API. NOT a bulk
                                             query on every dialect (see output)
  * naive ``describe_table()`` per table   — N round trips: the shape we refuse

and compares the live shape against the CATALOG's own shape (``kb_table_meta`` +
``kb_column_meta``). The comparison is on lowercased (table, column) NAMES, because
the catalog stores the raw ``data_type`` ('varchar') while a driver reports its own
rendering ('VARCHAR(50)') — hashing types makes every KB look drifted, which is a
false positive, not a finding.

Read-only: introspection plus catalog SELECTs. It never writes and never ALTERs.

Run (copy into the backend container first):

    docker cp backend/scripts_probe_schema_shape.py zhanlu-copy-backend:/app/
    docker exec zhanlu-copy-backend sh -lc 'cd /app && python scripts_probe_schema_shape.py'
"""

from __future__ import annotations

import hashlib
import time

from sqlalchemy import inspect, text

from app.database import SessionLocal
from app.models.knowledge_base import KnowledgeBase
from app.services.db.connector_factory import get_connector

# One round trip per dialect. Mirrors what the connectors already do for
# list_tables/describe_table — dialect SQL lives in the connector layer by design.
_SHAPE_SQL = {
    "mysql": (
        "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME, ORDINAL_POSITION"
    ),
    "mariadb": (
        "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME, ORDINAL_POSITION"
    ),
    "postgres": (
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = current_schema() ORDER BY table_name, ordinal_position"
    ),
    "postgresql": (
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = current_schema() ORDER BY table_name, ordinal_position"
    ),
    "sqlite": (
        "SELECT m.name, p.name, p.type FROM sqlite_master m "
        "JOIN pragma_table_info(m.name) p WHERE m.type IN ('table','view')"
    ),
}

CAP = 15


def _digest(pairs) -> str:
    body = "\n".join(sorted(f"{t.lower()}.{c.lower()}" for t, c in pairs))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _ms(fn):
    t0 = time.perf_counter()
    try:
        return fn(), (time.perf_counter() - t0) * 1000, None
    except Exception as e:  # pragma: no cover - probe
        return None, (time.perf_counter() - t0) * 1000, f"{type(e).__name__}: {e}"


def _catalog_pairs(db, kb_id):
    """Catalog shape as {(table, column)}. Key MUST be the pair, not the table:
    keying on the table alone silently collapses 2,627 warehouse columns into 142
    entries and invents a drift that does not exist."""
    from app.models.knowledge_catalog import KBColumnMeta, KBTableMeta

    rows = (
        db.query(KBTableMeta.table_name, KBColumnMeta.column_name)
        .join(KBColumnMeta, KBColumnMeta.table_meta_id == KBTableMeta.id)
        .filter(
            KBTableMeta.kb_id == kb_id,
            KBTableMeta.is_deleted.is_(False),
            KBColumnMeta.is_deleted.is_(False),
        )
        .all()
    )
    pairs = {((t or "").lower(), (c or "").lower()) for t, c in rows}
    return pairs, {(t or "").lower() for t, _ in pairs}


def _diff(live_pairs, live_tables, cat_pairs, cat_tables):
    # Casefold BOTH sides. MySQL reports column names in the case they were
    # created in while the catalog stores them as written — comparing raw makes
    # a pure casing difference look like 1,753 columns added and 1,737 dropped.
    live_cols = {f"{t.lower()}.{c.lower()}" for t, c in live_pairs}
    cat_col_set = {f"{t}.{c}" for t, c in cat_pairs}
    return {
        "tables_added": sorted(t.lower() for t in live_tables - cat_tables),
        "tables_dropped": sorted(cat_tables - live_tables),
        "columns_added": sorted(live_cols - cat_col_set),
        "columns_dropped": sorted(cat_col_set - live_cols),
    }


def main() -> None:
    db = SessionLocal()
    try:
        kbs = (
            db.query(KnowledgeBase)
            .filter(
                KnowledgeBase.is_deleted.is_(False),
                KnowledgeBase.source_kind == "database",
            )
            .all()
        )
        print(f"database KBs: {len(kbs)}")
        for kb in kbs:
            print("\n" + "=" * 78)
            print(f"KB {kb.name!r} ({kb.db_type} {kb.host}:{kb.port}/{kb.database_name}) "
                  f"catalog_status={kb.catalog_status}")
            cat_pairs, cat_tables = _catalog_pairs(db, kb.id)
            print(f"  catalog        : {len(cat_tables)} tables / {len(cat_pairs)} columns "
                  f"→ {_digest(cat_pairs) if cat_pairs else 'n/a'}")

            try:
                ctx = get_connector(kb)
            except Exception as e:
                print(f"  connector      : UNAVAILABLE ({e})")
                continue

            with ctx as conn:
                names, t_names, err = _ms(conn.list_tables)
                if err:
                    print(f"  list_tables()  : ERROR {err}")
                    continue
                print(f"  list_tables()  : {len(names)} tables in {t_names:.0f} ms")

                engine = getattr(conn, "_engine", None)
                dtype = (kb.db_type or "").lower()
                sql = _SHAPE_SQL.get(dtype)
                live_pairs = None
                if sql and engine is not None:
                    def one_query():
                        with engine.connect() as c:
                            return [(str(r[0]), str(r[1])) for r in c.execute(text(sql))]

                    rows, t_sql, err = _ms(one_query)
                    if err:
                        print(f"  info_schema ×1 : ERROR {err}")
                    else:
                        live_pairs = rows
                        print(f"  info_schema ×1 : {len(rows)} columns in {t_sql:.0f} ms "
                              f"→ live shape {_digest(rows)}")

                if engine is not None:
                    def bulk():
                        md = inspect(engine).get_multi_columns()
                        return [(str(t), str(c_.get("name", "")))
                                for t, cols in md.items() for c_ in cols]

                    rows2, t_bulk, err = _ms(bulk)
                    if err:
                        print(f"  get_multi_cols : ERROR {err}")
                    else:
                        print(f"  get_multi_cols : {len(rows2)} columns in {t_bulk:.0f} ms"
                              f" → {_digest(rows2)}"
                              f" {'(SAME as info_schema)' if live_pairs and _digest(rows2) == _digest(live_pairs) else '(DIFFERS from info_schema)'}")

                if live_pairs:
                    live_tables = {t.lower() for t, _ in live_pairs}
                    diff = _diff(live_pairs, live_tables, cat_pairs, cat_tables)
                    print(f"  DRIFT          : "
                          f"tables +{len(diff['tables_added'])}/-{len(diff['tables_dropped'])}, "
                          f"columns +{len(diff['columns_added'])}/-{len(diff['columns_dropped'])}")
                    for key in ("tables_added", "tables_dropped",
                                "columns_added", "columns_dropped"):
                        vals = diff[key]
                        if vals:
                            shown = ", ".join(vals[:CAP])
                            more = f" … +{len(vals) - CAP} more" if len(vals) > CAP else ""
                            print(f"    {key}: {shown}{more}")

                sample = names[:10]

                def naive():
                    n = 0
                    for t in sample:
                        n += len(conn.describe_table(t))
                    return n

                _, t_naive, err = _ms(naive)
                if not err and sample:
                    print(f"  describe_table : {len(sample)} of {len(names)} tables in "
                          f"{t_naive:.0f} ms → ~{t_naive / len(sample) * len(names):.0f} ms "
                          f"for the whole KB")
    finally:
        db.close()


if __name__ == "__main__":
    main()
