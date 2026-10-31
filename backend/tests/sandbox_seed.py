"""Shared synthetic sandbox for the P0-P7 integrated acceptance program.

ONE set of seed definitions, two isolated database instances:
- the automated test DB (per-test ephemeral sqlite), and
- the live walkthrough DB (separate persistent sqlite file).

We share the seed *definitions*, never mutable database state (D1). The data
source is a separate sqlite file the KnowledgeBase points at, so the governed
metric path executes against REAL tables through the real QueryService +
execution gate.

This is a clearly-identified SYNTHETIC sandbox — no production data, no
fabricated formulas (the gross-margin formula is declared in MetricDefinition,
never computed by an LLM).
"""

from __future__ import annotations

import sqlite3

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.knowledge_base import KnowledgeBase
from app.models.metric_definition import MetricDefinition
from app.models.ontology import (
    AgentDomainBinding,
    OntologyAction,
    OntologyDomain,
    OntologyEvolutionState,
    OntologyLink,
    OntologyObject,
)
from app.models.ontology_revision import OntologyRevision
from app.models.knowledge_catalog import KBColumnMeta, KBTableMeta
from app.models.identity import FileTableRow, FileTableSchema, IdentityMatch
from app.models.resource_access_policy import ResourceAccessPolicy  # noqa: F401


# Explicit table list — mirrors the proven test pattern. Creating the full
# metadata via create_all would drag in a JSONB column (dashboard_apps) that
# SQLite cannot render, corrupting the engine.
def _tables():
    return [
        KnowledgeBase.__table__, MetricDefinition.__table__,
        OntologyDomain.__table__, OntologyObject.__table__,
        OntologyLink.__table__, OntologyAction.__table__,
        OntologyRevision.__table__, OntologyEvolutionState.__table__,
        AgentDomainBinding.__table__,
        KBTableMeta.__table__, KBColumnMeta.__table__,
        FileTableSchema.__table__, FileTableRow.__table__,
        IdentityMatch.__table__, ResourceAccessPolicy.__table__,
    ]


ORG = "org-sandbox"
OTHER_ORG = "org-other"
OWNER_ID = "user-owner"
RESTRICTED_ID = "user-restricted"

KB_SALES = "kb-sales"
KB_FILE = "kb-file"
KB_DOC = "kb-doc"

METRIC_REVENUE = "met-revenue"
METRIC_COST_DIRECT = "met-cost-direct"
METRIC_COST_INDIRECT = "met-cost-indirect"
METRIC_TOTAL_COST = "met-total-cost"
METRIC_GROSS_MARGIN = "met-gross-margin"

OBJ_VEHICLE = "obj-vehicle"
OBJ_SALES = "obj-sales"
OBJ_FINANCIALS = "obj-financials"
OBJ_GROSS_MARGIN = "obj-gross-margin"


def make_engine():
    """Ephemeral in-memory-shared sqlite engine with the needed tables only."""
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng, tables=_tables())
    return eng


def create_data_db(path: str) -> None:
    """Create the source database (a separate sqlite file) with seed rows.

    gross_margin is stored as a derived column so the governed metric reads
    SUM(margin) — the decomposition (revenue, total_cost) is declared via
    depends_on_metrics, not computed here.
    """
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE IF EXISTS vehicle_models")
    conn.execute("DROP TABLE IF EXISTS sales_transactions")
    conn.execute("DROP TABLE IF EXISTS financials")
    conn.execute(
        "CREATE TABLE vehicle_models ("
        "id TEXT PRIMARY KEY, model_name TEXT, segment TEXT)")
    conn.execute(
        "CREATE TABLE sales_transactions ("
        "id TEXT PRIMARY KEY, model_id TEXT, sale_amount REAL, cost_amount REAL,"
        " margin REAL, sale_date TEXT, sale_month TEXT, cost_type TEXT)")
    conn.execute(
        "CREATE TABLE financials ("
        "id TEXT PRIMARY KEY, model_id TEXT, period TEXT, overhead_amount REAL)")

    models = [
        ("v-001", "Model X", "SUV"),
        ("v-002", "Model Y", "Sedan"),
        ("v-003", "Model Z", "Truck"),
    ]
    conn.executemany("INSERT INTO vehicle_models VALUES (?, ?, ?)", models)

    def sale(sid, mid, amt, cost, month, ctype):
        margin = round(amt - cost, 2)
        return (sid, mid, amt, cost, margin, f"{month}-15", month, ctype)

    rows = [
        # 2026-10 (current)
        sale("s-01", "v-001", 1000.0, 600.0, "2026-10", "direct"),
        sale("s-02", "v-001", 1000.0, 650.0, "2026-10", "indirect"),
        sale("s-03", "v-002", 800.0, 500.0, "2026-10", "direct"),
        sale("s-04", "v-002", 800.0, 520.0, "2026-10", "indirect"),
        sale("s-05", "v-003", 1200.0, 900.0, "2026-10", "direct"),
        sale("s-06", "v-003", 1200.0, 950.0, "2026-10", "indirect"),
        # 2026-09 (prior) — Model X margin declined vs prior month
        sale("s-07", "v-001", 1000.0, 550.0, "2026-09", "direct"),
        sale("s-08", "v-001", 1000.0, 560.0, "2026-09", "indirect"),
        sale("s-09", "v-002", 800.0, 520.0, "2026-09", "direct"),
        sale("s-10", "v-002", 800.0, 530.0, "2026-09", "indirect"),
        sale("s-11", "v-003", 1200.0, 920.0, "2026-09", "direct"),
        sale("s-12", "v-003", 1200.0, 960.0, "2026-09", "indirect"),
    ]
    conn.executemany(
        "INSERT INTO sales_transactions VALUES (?,?,?,?,?,?,?,?)", rows)

    fin = [
        ("f-001", "v-001", "2026-10", 40.0),
        ("f-002", "v-002", "2026-10", 30.0),
        ("f-003", "v-003", "2026-10", 50.0),
    ]
    conn.executemany("INSERT INTO financials VALUES (?,?,?,?)", fin)
    conn.commit()
    conn.close()


def _metric(mid, name, table, column, agg, filters, depends_on, base_sql=None,
            time_column="sale_month"):
    # NOTE: the time window (sale_month) is intentionally NOT baked into
    # filters_json — it is supplied at compute time so the same governed metric
    # can answer "this month" vs "prior month" (declining-profitability check).
    # time_column documents the metric's period dimension so the governed
    # compare/trend endpoints can vary the period without bypassing the
    # MetricDefinition.
    return MetricDefinition(
        id=mid, org_id=ORG, datasource_id=KB_SALES,
        name=name, display_name=name,
        description=f"governed {name}",
        base_table=table, base_column=column, aggregation=agg,
        unit="CNY", synonyms=[], is_enabled=True,
        lifecycle="published", reviewed_by="admin-1",
        version=1, evidence_status="verified",
        time_column=time_column,
        filters_json=[],
        depends_on_metrics=depends_on,
        base_sql=base_sql or f"SELECT {agg}({column}) FROM {table}",
        source_object_refs=[OBJ_GROSS_MARGIN] if mid == METRIC_GROSS_MARGIN else None,
    )


def build_sandbox(session, data_db_path: str) -> None:
    """Seed the metadata DB (ontology + metrics + identity + docs)."""
    data_kb = KnowledgeBase(
        id=KB_SALES, org_id=ORG, name="sales-db",
        source_kind="database", db_type="sqlite", api_url=data_db_path)
    file_kb = KnowledgeBase(
        id=KB_FILE, org_id=ORG, name="vehicle-file",
        source_kind="file", db_type="file")
    # project-scoped document KB — owner may access, an org member WITHOUT a
    # policy grant may NOT (fail-closed document gate).
    doc_kb = KnowledgeBase(
        id=KB_DOC, org_id=ORG, name="gm-policy-doc",
        source_kind="document", project_id="proj-gm",
        created_by_id=OWNER_ID)
    session.add_all([data_kb, file_kb, doc_kb])

    # Catalog metadata for heterogeneous source discovery.
    tmeta = KBTableMeta(
        id="tm-sales", kb_id=KB_SALES, schema_name="",
        table_name="sales_transactions", table_type="TABLE",
        table_role="fact", grain="one row = one sale line")
    session.add(tmeta)
    session.flush()  # FK order: Postgres enforces; the UOW does not sort raw FKs
    session.add(KBColumnMeta(
        table_meta_id="tm-sales", column_name="sale_amount",
        ordinal=2, data_type="REAL", is_primary_key=False))
    session.add(KBColumnMeta(
        table_meta_id="tm-sales", column_name="cost_amount",
        ordinal=3, data_type="REAL", is_primary_key=False))
    session.add(KBColumnMeta(
        table_meta_id="tm-sales", column_name="model_id",
        ordinal=1, data_type="TEXT", is_primary_key=False))

    # File KB structured staging (second, heterogeneous source).
    session.add(FileTableSchema(
        kb_id=KB_FILE, sheet_name="Vehicles",
        columns_json=[{"name": "model_name", "type": "text"}],
        row_count=3))
    session.add(FileTableRow(
        kb_id=KB_FILE, sheet_name="Vehicles", row_idx=0,
        values_json={"model_name": "Model X"}))

    domain = OntologyDomain(org_id=ORG, code="auto", name="Automotive")
    session.add(domain)
    session.flush()

    objects = [
        OntologyObject(
            org_id=ORG, id=OBJ_VEHICLE, domain_id=domain.id, name="VehicleModel",
            display_name="Vehicle Model", backing_kind="entity",
            backing_ref=f"kb:{KB_SALES}:vehicle_models",
            evidence_status="verified", confidence=0.95,
            properties_json=[{"name": "model_name", "type": "string"}],
            is_active=True),
        OntologyObject(
            org_id=ORG, id=OBJ_SALES, domain_id=domain.id, name="SalesTransaction",
            display_name="Sales Transaction", backing_kind="entity",
            backing_ref=f"kb:{KB_SALES}:sales_transactions",
            evidence_status="verified", confidence=0.95,
            properties_json=[{"name": "sale_amount", "type": "number"}],
            is_active=True),
        OntologyObject(
            org_id=ORG, id=OBJ_FINANCIALS, domain_id=domain.id, name="Financials",
            display_name="Financials", backing_kind="entity",
            backing_ref=f"kb:{KB_SALES}:financials",
            evidence_status="inferred", confidence=0.8,
            properties_json=[{"name": "overhead_amount", "type": "number"}],
            is_active=True),
        OntologyObject(
            org_id=ORG, id=OBJ_GROSS_MARGIN, domain_id=domain.id, name="GrossMargin",
            display_name="Gross Margin", backing_kind="metric",
            backing_ref=f"metric:{METRIC_GROSS_MARGIN}",
            evidence_status="verified", confidence=0.9,
            properties_json=[{"name": "margin", "type": "number"}],
            # P4 document grounding + authorization: owner may see it,
            # a non-granted org member may not (fail-closed).
            provenance_json={
                "document_provenance": [{
                    "source_kb_id": KB_DOC,
                    "evidence_id": "ev-gm",
                    "text": "Gross margin = revenue minus total cost, per "
                            "the finance policy document.",
                }]
            },
            is_active=True),
    ]
    session.add_all(objects)

    links = [
        OntologyLink(org_id=ORG, from_object_id=OBJ_SALES, to_object_id=OBJ_VEHICLE,
                     link_type="records", cardinality="many_to_one",
                     description="a sale line records one vehicle model",
                     evidence_status="verified", confidence=0.95),
        OntologyLink(org_id=ORG, from_object_id=OBJ_FINANCIALS, to_object_id=OBJ_VEHICLE,
                     link_type="attributes", cardinality="many_to_one",
                     description="financials attribute a vehicle model",
                     evidence_status="inferred", confidence=0.8),
        OntologyLink(org_id=ORG, from_object_id=OBJ_GROSS_MARGIN, to_object_id=OBJ_SALES,
                     link_type="derived_from", cardinality="one_to_many",
                     description="gross margin is derived from sales",
                     evidence_status="verified", confidence=0.9),
    ]
    session.add_all(links)

    # P3 cross-source identity: the DB vehicle v-001 IS the file KB vehicle.
    session.add(IdentityMatch(
        org_id=ORG,
        object_id=OBJ_VEHICLE,
        source_a={"kb_id": KB_SALES, "table": "vehicle_models"},
        source_b={"kb_id": KB_FILE, "sheet": "Vehicles"},
        source_a_key="sales.vehicle_models", source_b_key="file.Vehicles",
        pk_a="v-001", pk_b="v-001",
        match_tier="exact", status="confirmed",
        confidence=1.0,
        evidence_json={"method": "exact_key_match"},
        reviewed_by=OWNER_ID))

    # Governed metrics with explicit dependencies (D3).
    revenue = _metric(METRIC_REVENUE, "Revenue", "sales_transactions",
                      "sale_amount", "sum",
                      [{"column": "sale_month", "op": "eq", "value": "2026-10"}],
                      None)
    cost_direct = _metric(METRIC_COST_DIRECT, "DirectCost", "sales_transactions",
                          "cost_amount", "sum",
                          [{"column": "cost_type", "op": "eq", "value": "direct"},
                           {"column": "sale_month", "op": "eq", "value": "2026-10"}],
                          None)
    cost_indirect = _metric(METRIC_COST_INDIRECT, "IndirectCost",
                            "sales_transactions", "cost_amount", "sum",
                            [{"column": "cost_type", "op": "eq", "value": "indirect"},
                             {"column": "sale_month", "op": "eq", "value": "2026-10"}],
                            None)
    total_cost = _metric(METRIC_TOTAL_COST, "TotalCost", "sales_transactions",
                         "cost_amount", "sum",
                         [{"column": "sale_month", "op": "eq", "value": "2026-10"}],
                         [METRIC_COST_DIRECT, METRIC_COST_INDIRECT])
    gross_margin = _metric(METRIC_GROSS_MARGIN, "GrossMargin",
                           "sales_transactions", "margin", "sum",
                           [{"column": "sale_month", "op": "eq", "value": "2026-10"}],
                           [METRIC_REVENUE, METRIC_TOTAL_COST])
    session.add_all([revenue, cost_direct, cost_indirect, total_cost, gross_margin])

    session.commit()


def make_session(engine):
    Session = sessionmaker(bind=engine)
    return Session()


def run_business_question(session, data_db_path: str) -> dict:
    """The integrated 'ultimate test' chain (workstream 5).

    Business question: gross margin by vehicle model this month, and which
    models have declining profitability. Uses ONLY the governed compute_metric
    path — no LLM-computed numbers, deterministic, provenance-carrying.
    """
    from app.services.ontology.metrics import compute_metric

    # 1. Discover authorized sources + resolve concepts (the sandbox seeded them)
    vehicles = [("v-001", "Model X"), ("v-002", "Model Y"), ("v-003", "Model Z")]

    by_model = {}
    for vid, vname in vehicles:
        # metric filters_json is empty; pin BOTH model_id and sale_month here
        res = compute_metric(
            session, ORG, METRIC_GROSS_MARGIN,
            filters={"model_id": vid, "sale_month": "2026-10"})
        by_model[vname] = res

    # 2. Declining profitability: compare current month vs prior month.
    declining = []
    for vid, vname in vehicles:
        cur = compute_metric(
            session, ORG, METRIC_GROSS_MARGIN,
            filters={"model_id": vid, "sale_month": "2026-10"})
        prev = compute_metric(
            session, ORG, METRIC_GROSS_MARGIN,
            filters={"model_id": vid, "sale_month": "2026-09"})
        cur_v = (cur.get("value") if cur.get("success") else None)
        prev_v = (prev.get("value") if prev.get("success") else None)
        if cur_v is not None and prev_v is not None and cur_v < prev_v:
            declining.append({
                "model": vname,
                "current_margin": cur_v,
                "prior_margin": prev_v,
            })

    return {
        "question": "gross margin by vehicle model this month + declining models",
        "gross_margin_by_model": by_model,
        "declining_models": declining,
        # 3. Provenance of the governed computation (traceable)
        "trace": by_model.get("Model X", {}).get("execution_trace"),
        "provenance": by_model.get("Model X", {}).get("provenance"),
    }
