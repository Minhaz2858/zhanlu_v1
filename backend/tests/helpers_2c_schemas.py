"""2C shared fixtures — four DELIBERATELY UNFAMILIAR synthetic schemas.

The customer promise under test: connect a database Zhanlu has never seen
and receive a correctly grounded answer without developers teaching Zhanlu
that database first. Every table/column name below avoids the pipeline's
generic token lists where the test's purpose requires it (reviewer's
schema-generalization requirement), and each schema carries at least one
adversarial property:

  sales          unfamiliar names (txn_hdr, sku_ledger, cust_ref), a
                 DECLARED-FK path, a MISSING-FK path (sku_ledger.txn_hdr_id),
                 a name-partition family with CONFIRMED partition key
                 (north_/south_sku_ledger + region_tag), a superset rollup
                 (sku_ledger_all), a misleading-grain name (sku_ledger holds
                 per-transaction rows, not a ledger), and a system-shaped
                 credential table (meta_sync_state).
  healthcare     ambiguous relationship (ward_code is a joinable column in
                 TWO tables but is a key in NEITHER), a missing FK
                 (dispense_log.item_id), unfamiliar drug classes.
  manufacturing  a two-level declared-FK chain whose first hop is 1↔N
                 (batch_trace ≫ component_lot) — the join-fanout surface.
  finance        a relationship on a NON-ID unique key with NO declared FK
                 (journal_line.acct_code → account_map.acct_code), two
                 CONFLICTING measures on one table (amount_dr vs amount_cr,
                 different bases), and a second, ambiguous join path
                 (settlement_ref.acct_code).

Row values are DETERMINISTIC so acceptance tests can assert exact totals
(the `FACTS` registry) — actual query results must agree with streamed and
persisted answers (reviewer P0 #5).

Temporal columns are declared with the DATE type (not name tokens) — the
coverage probe must find them by TYPE, not by customer vocabulary.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import uuid


# ── Deterministic data facts (asserted in acceptance + e2e agreement) ──────

def _d(days: int, base="2026-01-01") -> str:
    """base + days, ISO date string (no imports beyond stdlib datetime)."""
    import datetime as _dt

    d0 = _dt.date.fromisoformat(base)
    return (d0 + _dt.timedelta(days=days)).isoformat()


def _build_sales_rows() -> dict[str, list[tuple]]:
    cust_ref = [
        (i + 1, f"R-{1001 + i}", "core" if i % 2 == 0 else "plus", _d(-60 + i))
        for i in range(6)
    ]
    txn_hdr = [
        (i + 1, (i % 6) + 1, "web" if i % 2 == 0 else "store",
         100.0 + i * 10.0, _d(i * 3))
        for i in range(40)
    ]
    # Name-partition family: same fingerprint, per-table constant region_tag,
    # txn_hdr_id WITHOUT a declared FK (the missing-FK discovery path).
    north = [
        (i + 1, (i % 40) + 1, f"S-{chr(65 + i % 10)}", "north",
         1 + (i % 5), _d(28 + i))
        for i in range(40)
    ]
    south = [
        (i + 1, (i % 40) + 1, f"S-{chr(74 + i % 10)}", "south",
         1 + (i % 5), _d(28 + i))
        for i in range(40)
    ]
    # Global rollup: name decomposes the members (base == "sku_ledger"),
    # fingerprint is a SUPERSET that falls BELOW the family clustering
    # threshold (6 shared cols + 3 rollup-only cols → Jaccard 6/9 < 0.7),
    # rows = north + south (P4 parity evidence).
    all_rows = []
    for i, (txn_id, sku, region, units, moved) in enumerate(
        [(r[1], r[2], r[3], r[4], r[5]) for r in north]
        + [(r[1], r[2], r[3], r[4], r[5]) for r in south]
    ):
        all_rows.append(
            (i + 1, txn_id, sku, region, units, moved, "auto", _d(75), "full")
        )
    sync = [(901, "tok_a", "hash_a", _d(-1)), (902, "tok_b", "hash_b", _d(-2))]
    return {
        "cust_ref": cust_ref,
        "txn_hdr": txn_hdr,
        "north_sku_ledger": north,
        "south_sku_ledger": south,
        "sku_ledger": all_rows,
        "meta_sync_state": sync,
        "__facts__": {
            "txn_hdr_total_amt_sum": round(sum(r[3] for r in txn_hdr), 2),
            "sku_ledger_units_sum": sum(r[4] for r in all_rows),
            "north_units_sum": sum(r[4] for r in north),
            "south_units_sum": sum(r[4] for r in south),
        },
    }


def _build_healthcare_rows() -> dict[str, list[tuple]]:
    formulary = [
        (i + 1, f"F-{201 + i}", ("analogue_a", "analogue_b", "nutrient_c")[i % 3],
         i % 4 == 0)
        for i in range(12)
    ]
    ward_stock = [
        (i + 1, f"w{(i % 4) + 1}", (i % 12) + 1, i % 7, _d(i * 3))
        for i in range(60)
    ]
    dispense = [
        (i + 1, (i % 12) + 1, f"w{(i % 4) + 1}", 1 + (i % 6), _d(i * 2))
        for i in range(150)
    ]
    return {
        "formulary_item": formulary,
        "ward_stock": ward_stock,
        "dispense_log": dispense,
        "__facts__": {
            "dispense_units_sum": sum(r[3] for r in dispense),
            "ward_stock_packs_sum": sum(r[3] for r in ward_stock),
        },
    }


def _build_manufacturing_rows() -> dict[str, list[tuple]]:
    lots = [
        (i + 1, f"L-{i + 1:02d}", "vend_p" if i % 2 == 0 else "vend_q", _d(-30 + i))
        for i in range(10)
    ]
    batches = [
        (i + 1, (i % 10) + 1, f"st_{(i % 3) + 1}", 40.0 + i, _d(i))
        for i in range(80)
    ]
    events = [
        (i + 1, (i % 80) + 1, ("start", "stop", "fault")[i % 3], _d(i // 2))
        for i in range(200)
    ]
    return {
        "component_lot": lots,
        "batch_trace": batches,
        "workcell_event": events,
        "__facts__": {
            "cycle_seconds_sum": round(sum(r[3] for r in batches), 2),
            "batch_count": len(batches),
        },
    }


def _build_finance_rows() -> dict[str, list[tuple]]:
    accounts = [
        (i + 1, f"A-{i + 1:02d}", "operating" if i % 2 == 0 else "clearing",
         _d(-90 + i))
        for i in range(8)
    ]
    journal = []
    for i in range(200):
        dr = 10.0 + i
        cr = round(dr * 0.25, 2)
        journal.append((i + 1, f"A-{(i % 8) + 1:02d}", dr, cr, _d(i)))
    settle = [
        (i + 1, (i % 200) + 1, f"ST-{i + 1:03d}", f"A-{(i % 8) + 1:02d}", _d(i))
        for i in range(50)
    ]
    return {
        "account_map": accounts,
        "journal_line": journal,
        "settlement_ref": settle,
        "__facts__": {
            "amount_dr_total": round(sum(r[2] for r in journal), 2),
            "amount_cr_total": round(sum(r[3] for r in journal), 2),
        },
    }


# ── DDL (dialect-neutral: valid on sqlite/mysql/postgres as written) ──────

_SALES_DDL = """
CREATE TABLE cust_ref (
    id INTEGER PRIMARY KEY,
    ref_code VARCHAR(32) NOT NULL UNIQUE,
    tier_tag VARCHAR(16),
    listed_on DATE
);
CREATE TABLE txn_hdr (
    id INTEGER PRIMARY KEY,
    cust_ref_id INTEGER NOT NULL REFERENCES cust_ref(id),
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
CREATE TABLE meta_sync_state (
    id INTEGER PRIMARY KEY,
    session_token VARCHAR(64),
    password_hash VARCHAR(64),
    synced_at DATE
);
"""

_HEALTHCARE_DDL = """
CREATE TABLE formulary_item (
    id INTEGER PRIMARY KEY,
    item_code VARCHAR(32) NOT NULL UNIQUE,
    drug_class VARCHAR(32),
    is_controlled INTEGER
);
CREATE TABLE ward_stock (
    id INTEGER PRIMARY KEY,
    ward_code VARCHAR(16) NOT NULL,
    item_id INTEGER NOT NULL REFERENCES formulary_item(id),
    packs_on_hand INTEGER NOT NULL,
    counted_on DATE
);
CREATE TABLE dispense_log (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL,
    ward_code VARCHAR(16) NOT NULL,
    units_dispensed INTEGER NOT NULL,
    dispensed_at DATE
);
"""

_MANUFACTURING_DDL = """
CREATE TABLE component_lot (
    id INTEGER PRIMARY KEY,
    lot_code VARCHAR(32) NOT NULL UNIQUE,
    vendor_tag VARCHAR(16),
    received_on DATE
);
CREATE TABLE batch_trace (
    id INTEGER PRIMARY KEY,
    lot_id INTEGER NOT NULL REFERENCES component_lot(id),
    station_tag VARCHAR(16),
    cycle_seconds REAL NOT NULL,
    ran_on DATE
);
CREATE TABLE workcell_event (
    id INTEGER PRIMARY KEY,
    batch_id INTEGER NOT NULL REFERENCES batch_trace(id),
    event_kind VARCHAR(16),
    logged_at DATE
);
"""

_FINANCE_DDL = """
CREATE TABLE account_map (
    id INTEGER PRIMARY KEY,
    acct_code VARCHAR(32) NOT NULL UNIQUE,
    acct_kind VARCHAR(16),
    opened_on DATE
);
CREATE TABLE journal_line (
    id INTEGER PRIMARY KEY,
    acct_code VARCHAR(32) NOT NULL,
    amount_dr REAL NOT NULL,
    amount_cr REAL NOT NULL,
    posted_on DATE
);
CREATE TABLE settlement_ref (
    id INTEGER PRIMARY KEY,
    journal_line_id INTEGER NOT NULL REFERENCES journal_line(id),
    settle_code VARCHAR(32) NOT NULL UNIQUE,
    acct_code VARCHAR(32),
    settled_on DATE
);
"""

#: schema key → (DDL, row builder). Order matters for deterministic ids.
SCHEMAS: dict[str, tuple[str, object]] = {
    "sales": (_SALES_DDL, _build_sales_rows),
    "healthcare": (_HEALTHCARE_DDL, _build_healthcare_rows),
    "manufacturing": (_MANUFACTURING_DDL, _build_manufacturing_rows),
    "finance": (_FINANCE_DDL, _build_finance_rows),
}


def build_sqlite_db(schema: str) -> tuple[str, dict]:
    """Build a synthetic SQLite file. Returns (path, facts)."""
    ddl, builder = SCHEMAS[schema]
    rows = builder()
    facts = rows.pop("__facts__")
    fd, path = tempfile.mkstemp(suffix=f"-2c-{schema}.db")
    os.close(fd)
    con = sqlite3.connect(path)
    try:
        con.executescript(ddl)
        for table, data in rows.items():
            if not data:
                continue
            cols = ", ".join(["?"] * len(data[0]))
            con.executemany(
                f"INSERT INTO {table} VALUES ({cols})", data,
            )
        con.commit()
    finally:
        con.close()
    return path, facts


def seed_kb(db, *, path: str, schema: str, org_id: str, app_id: str):
    """Insert a database-kind KnowledgeBase row shaped for the connector."""
    from app.models.knowledge_base import KnowledgeBase

    kb = KnowledgeBase(
        id=f"kb-2c-{schema}-{uuid.uuid4().hex[:8]}",
        name=f"2c_{schema}_synthetic",
        org_id=org_id,
        app_id=app_id,
        type="database",
        source_kind="database",
        db_type="sqlite",
        file_url=path,
        database_name=path,
        catalog_status="ready",
        nl2sql_state="nl2sql_ready",
    )
    db.add(kb)
    db.flush()
    return kb


def apply_discovery_flags(monkeypatch) -> None:
    """Turn the discovery machinery ON and the expensive/LLM side-paths OFF.

    The description LLM is stubbed to FAIL — description fields must then
    degrade honestly (blank / source-comment only, never invented). That is
    itself part of the acceptance (missing metadata ≠ invented metadata).
    """
    from app.config import settings

    monkeypatch.setattr(
        settings, "TABLE_ROLE_AUTO_CLASSIFY_ENABLED", True,
    )
    monkeypatch.setattr(settings, "CATALOG_JOIN_EDGES_ENABLED", True)
    monkeypatch.setattr(settings, "KG_COVERAGE_PROBE_ENABLED", True)
    monkeypatch.setattr(
        settings, "KG_DOMAIN_DISCOVERY_ENABLED", False,
    )
    monkeypatch.setattr(
        settings, "KG_METRIC_BOOTSTRAP_ENABLED", False,
    )
    monkeypatch.setattr(
        settings, "KG_RESOURCE_REGISTRY_ENABLED", False,
    )

    import app.services.knowledge_graph.catalog_indexer as indexer

    # Embeddings need a vector store — out of scope for the discovery
    # acceptance; stub the module-level hook the indexer calls.
    monkeypatch.setattr(indexer, "_embed_catalog", lambda *a, **k: None)

    # Description LLM: fail honestly (degrade to source comment / blank).
    async def _llm_unavailable(*a, **k):
        raise RuntimeError("llm unavailable in 2C discovery tests")

    monkeypatch.setattr(
        "app.services.llm_service.call_llm", _llm_unavailable,
    )


def discover(db, kb, monkeypatch) -> dict:
    """Run the REAL discovery pipeline: index_kb_catalog + classification
    backfill. Returns the per-table verdict summary the tests assert on."""
    import asyncio

    from app.models.knowledge_catalog import KBColumnMeta, KBTableMeta
    from app.services.knowledge_graph.catalog_indexer import index_kb_catalog
    from scripts.backfill_schema_classification import backfill

    apply_discovery_flags(monkeypatch)
    asyncio.run(index_kb_catalog(kb, db))
    backfill(kb_id=kb.id)
    db.expire_all()

    metas = (
        db.query(KBTableMeta)
        .filter(KBTableMeta.kb_id == kb.id)
        .all()
    )
    out: dict[str, dict] = {}
    for m in metas:
        cols = (
            db.query(KBColumnMeta)
            .filter(KBColumnMeta.table_meta_id == m.id)
            .order_by(KBColumnMeta.ordinal)
            .all()
        )
        out[m.table_name] = {
            "meta": m,
            "columns": {c.column_name: c for c in cols},
        }
    return out
