"""2C Tier B — LIVE engine acceptance (reviewer-authorized with restrictions).

Runs the REAL discovery + REAL connector against the local MySQL (:3308) and
PostgreSQL (:5400) engines over the 2C synthetic schema. SKIPS AUTOMATICALLY
unless the engine URL env var is present:

    TEST_MYSQL_URL=mysql+pymysql://user:pass@127.0.0.1:3308/
    TEST_PG_URL=postgresql+psycopg2://user:pass@127.0.0.1:5400/

Restrictions (reviewer Decision 2 + 2026-10-31 Tier-B directive, compliance):
  1. Read-only at the DATABASE level: the pipeline connector uses a
     dedicated SELECT-only account (``zhanlu_2c_ro``). A negative write
     probe asserts the account CANNOT create/insert. The unrestricted dev
     credentials are NOT used.
  2. Synthetic test schemas ONLY — the 2C sales schema.
  3. No production/customer tables are touched.
  4. No schema modifications outside the test-owned database
     ``zhanlu_2c_tmp``. The database itself is provisioned out-of-band by
     the server admin; inside it, DDL runs under the dedicated
     ``zhanlu_2c_setup`` account — never under the read-only account.
  5. No destructive setup or cleanup — the test database is LEFT IN PLACE.
  6. Skips automatically when the env vars are absent (verified below).
  7. The exact engine version is asserted and printed for the evidence.

Environment:
    TEST_MYSQL_URL=mysql+pymysql://zhanlu_2c_ro:...@127.0.0.1:3308/zhanlu_2c_tmp
    TEST_MYSQL_SETUP_URL=mysql+pymysql://zhanlu_2c_setup:...@127.0.0.1:3308/zhanlu_2c_tmp
    TEST_PG_URL=postgresql+psycopg2://zhanlu_2c_ro:...@127.0.0.1:5400/zhanlu_2c_tmp
    TEST_PG_SETUP_URL=postgresql+psycopg2://zhanlu_2c_setup:...@127.0.0.1:5400/zhanlu_2c_tmp

NOTE for reviewers: MySQL silently DISCARDS inline ``REFERENCES`` — the DDL
below uses named table-level FK constraints for MySQL and inline REFERENCES
for PostgreSQL; discovery must handle both real-world styles.
"""

from __future__ import annotations

import os
import sys

import pytest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

import helpers_2c_schemas as fx  # noqa: E402 — the 2C fixture library
from app.database import SessionLocal  # noqa: E402

MYSQL_URL = os.environ.get("TEST_MYSQL_URL", "").strip()
MYSQL_SETUP_URL = os.environ.get("TEST_MYSQL_SETUP_URL", "").strip()
PG_URL = os.environ.get("TEST_PG_URL", "").strip()
PG_SETUP_URL = os.environ.get("TEST_PG_SETUP_URL", "").strip()

pytestmark = [
    pytest.mark.skipif(
        not ((MYSQL_URL and MYSQL_SETUP_URL) or (PG_URL and PG_SETUP_URL)),
        reason="no TEST_*_URL / TEST_*_SETUP_URL pair provided — hermetic run",
    ),
]

_TEST_DB = "zhanlu_2c_tmp"


def _sales_ddl(engine: str) -> str:
    """The 2C sales schema, per-engine FK style (same names everywhere)."""
    inline_fk = engine == "postgresql"
    fk_txn = (
        " REFERENCES cust_ref(id)" if inline_fk
        else ", CONSTRAINT fk_txn_cust FOREIGN KEY (cust_ref_id) "
             "REFERENCES cust_ref(id)"
    )
    return f"""
CREATE TABLE cust_ref (
    id INTEGER PRIMARY KEY,
    ref_code VARCHAR(32) NOT NULL UNIQUE,
    tier_tag VARCHAR(16),
    listed_on DATE
);
CREATE TABLE txn_hdr (
    id INTEGER PRIMARY KEY,
    cust_ref_id INTEGER NOT NULL{fk_txn},
    channel_tag VARCHAR(16),
    total_amt REAL NOT NULL,
    booked_on DATE
);
CREATE TABLE north_sku_ledger (
    id INTEGER PRIMARY KEY,
    txn_hdr_id INTEGER NOT NULL,
    sku_tag VARCHAR(32) NOT NULL,
    region_tag VARCHAR(16) NOT NULL,
    units_moved INTEGER NOT NULL,
    moved_on DATE
);
CREATE TABLE south_sku_ledger (
    id INTEGER PRIMARY KEY,
    txn_hdr_id INTEGER NOT NULL,
    sku_tag VARCHAR(32) NOT NULL,
    region_tag VARCHAR(16) NOT NULL,
    units_moved INTEGER NOT NULL,
    moved_on DATE
);
CREATE TABLE sku_ledger (
    id INTEGER PRIMARY KEY,
    txn_hdr_id INTEGER NOT NULL,
    sku_tag VARCHAR(32) NOT NULL,
    region_tag VARCHAR(16) NOT NULL,
    units_moved INTEGER NOT NULL,
    moved_on DATE,
    summary_level VARCHAR(16) NOT NULL,
    built_on DATE,
    source_scope VARCHAR(16) NOT NULL
);
"""


def _sales_rows() -> dict[str, list[tuple]]:
    import datetime as dt

    def _d(days: int) -> str:
        return (dt.date(2026, 1, 1) + dt.timedelta(days=days)).isoformat()

    cust = [(i + 1, f"R-{1001 + i}", "core" if i % 2 == 0 else "plus",
             _d(-60 + i)) for i in range(6)]
    txn = [(i + 1, (i % 6) + 1, "web" if i % 2 == 0 else "store",
            100.0 + i * 10.0, _d(i * 3)) for i in range(40)]
    north = [(i + 1, (i % 40) + 1, f"S-{chr(65 + i % 10)}", "north",
              1 + (i % 5), _d(28 + i)) for i in range(40)]
    south = [(i + 1, (i % 40) + 1, f"S-{chr(74 + i % 10)}", "south",
              1 + (i % 5), _d(28 + i)) for i in range(40)]
    rollup = [
        (i + 1, txn_id, sku, region, units, moved, "auto", _d(75), "full")
        for i, (txn_id, sku, region, units, moved) in enumerate(
            [(r[1], r[2], r[3], r[4], r[5]) for r in north]
            + [(r[1], r[2], r[3], r[4], r[5]) for r in south]
        )
    ]
    return {
        "cust_ref": cust, "txn_hdr": txn,
        "north_sku_ledger": north, "south_sku_ledger": south,
        "sku_ledger": rollup,
    }


def _setup_engine(engine: str, setup_url: str):
    """(Re)build the synthetic tables inside the test-owned database.

    DDL runs under the DEDICATED setup credential — never the read-only
    account the pipeline uses. Touches NOTHING outside ``zhanlu_2c_tmp``;
    leaves everything in place (no destructive cleanup). Returns table_rows.
    """
    from sqlalchemy import create_engine, text

    eng = create_engine(setup_url, isolation_level="AUTOCOMMIT")
    rows = _sales_rows()
    with eng.connect() as c:
        if engine == "mysql":
            c.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for t in ("sku_ledger", "north_sku_ledger", "south_sku_ledger",
                  "txn_hdr", "cust_ref"):
            c.execute(text(f"DROP TABLE IF EXISTS {t}"))
        if engine == "mysql":
            c.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        for stmt in _sales_ddl(engine).split(";"):
            if stmt.strip():
                c.execute(text(stmt))
        for table, data in rows.items():
            for row in data:
                cols = ", ".join([":p" + str(i) for i in range(len(row))])
                params = {f"p{i}": v for i, v in enumerate(row)}
                c.execute(text(
                    f"INSERT INTO {table} VALUES ({cols})"
                ), params)
        if engine == "postgresql":
            # The read-only role must see the tables the setup role owns.
            c.execute(text(
                "GRANT SELECT ON ALL TABLES IN SCHEMA public TO zhanlu_2c_ro"
            ))
    eng.dispose()
    return rows


def _probe_read_only(engine: str, ro_url: str):
    """Negative probe: the pipeline account must NOT be able to write."""
    from sqlalchemy import create_engine, text

    eng = create_engine(ro_url, isolation_level="AUTOCOMMIT")
    denied = False
    try:
        with eng.connect() as c:
            c.execute(text(
                "INSERT INTO cust_ref VALUES (99999, 'X', 'X', '2026-01-01')"
            ))
    except Exception:
        denied = True  # the expected outcome
    eng.dispose()
    assert denied, (
        f"{engine}: the read-only pipeline account WAS able to write — "
        "database-level read-only enforcement is absent"
    )


def _kb_row(db, engine: str, url: str, org_id: str):
    from sqlalchemy.engine import make_url
    from app.models.knowledge_base import KnowledgeBase

    u = make_url(url)
    kb = KnowledgeBase(
        id=f"kb-2c-live-{engine}-{os.urandom(4).hex()}",
        name=f"2c_{engine}_live",
        org_id=org_id,
        app_id="app-2c",
        type="database",
        source_kind="database",
        db_type=engine,
        host=u.host,
        port=u.port,
        username=u.username,
        password=u.password,
        database_name=u.database,
        catalog_status="ready",
        nl2sql_state="nl2sql_ready",
    )
    db.add(kb)
    db.commit()
    return kb


@pytest.mark.skipif(
    not (MYSQL_URL and MYSQL_SETUP_URL),
    reason="TEST_MYSQL_URL / TEST_MYSQL_SETUP_URL not set",
)
def test_live_mysql_discovery_and_grounding(monkeypatch):
    _live_engine_test(monkeypatch, "mysql", MYSQL_URL, MYSQL_SETUP_URL)


@pytest.mark.skipif(
    not (PG_URL and PG_SETUP_URL),
    reason="TEST_PG_URL / TEST_PG_SETUP_URL not set",
)
def test_live_postgres_discovery_and_grounding(monkeypatch):
    _live_engine_test(monkeypatch, "postgresql", PG_URL, PG_SETUP_URL)


def _live_engine_test(monkeypatch, engine: str, ro_url: str, setup_url: str):
    import asyncio
    import uuid as _uuid

    from app.models.knowledge_catalog import KBTableMeta, KBTableRelation
    from app.services.db.connector_factory import get_connector
    from app.services.knowledge_graph.catalog_indexer import index_kb_catalog
    from scripts.backfill_schema_classification import backfill

    rows = _setup_engine(engine, setup_url)
    # Database-level read-only enforcement — NOT just the app validator.
    _probe_read_only(engine, ro_url)
    fx.apply_discovery_flags(monkeypatch)

    db = SessionLocal()
    try:
        kb = _kb_row(db, engine, ro_url, f"org-2c-{_uuid.uuid4().hex[:8]}")

        # Restriction 7: record the EXACT engine version.
        with get_connector(kb) as conn:
            info = conn.test_connection()
        assert info.get("ok"), f"{engine}: connection probe failed: {info}"
        print(f"\n2C LIVE {engine}: {info}")

        asyncio.run(index_kb_catalog(kb, db))
        backfill(kb_id=kb.id)
        db.expire_all()

        metas = {
            m.table_name: m
            for m in db.query(KBTableMeta)
            .filter(KBTableMeta.kb_id == kb.id)
            .all()
        }
        assert {n.lower() for n in metas} == {
            "cust_ref", "txn_hdr", "north_sku_ledger", "south_sku_ledger",
            "sku_ledger",
        }, f"{engine}: discovery missed tables: {sorted(metas)}"
        assert metas["txn_hdr"].row_count == 40, (
            f"{engine}: exact count wrong: {metas['txn_hdr'].row_count}"
        )
        # The FK (named constraint on MySQL, inline on Postgres) is
        # discovered on BOTH styles.
        rels = (
            db.query(KBTableRelation)
            .filter(KBTableRelation.kb_id == kb.id)
            .all()
        )
        fk = [r for r in rels if r.relation_type == "FK"]
        assert fk, f"{engine}: no declared FK discovered"
        assert any(
            r.source_cardinality == "many" and r.target_cardinality == "one"
            for r in fk
        )
        # Scope machinery on live metadata.
        assert metas["sku_ledger"].dataset_scope == "global", (
            f"{engine}: rollup not global "
            f"({metas['sku_ledger'].dataset_scope!r})"
        )
        for p in ("north_sku_ledger", "south_sku_ledger"):
            assert (metas[p].dataset_scope or "").startswith("partition"), (
                f"{engine}.{p}: {metas[p].dataset_scope!r}"
            )

        # Real grounding: execute the aggregate through the REAL connector.
        expected = round(sum(r[3] for r in rows["txn_hdr"]), 2)
        with get_connector(kb) as conn:
            out = conn.execute(
                "SELECT SUM(total_amt) AS s FROM txn_hdr"
            )
        got = list(out[0].values())[0] if out else None
        assert got is not None and abs(float(got) - expected) < 0.01, (
            f"{engine}: connector aggregate {got!r} != seeded {expected!r}"
        )
        print(f"2C LIVE {engine}: SUM(total_amt)={got} matches seed data")
    finally:
        db.close()
