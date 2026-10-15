"""Discovery v2 — SaaS dynamic schema matching (2026-10-15).

The database is TOTALLY dynamic per tenant (ERP/PLM/CRM, any vendor).
Layers: connector hints → org glossary → LLM-assisted matching with a
confidence gate. Everything here mocks ONLY the LLM call — schema
inventory is a real fake SchemaService."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.services.ontology.discovery import (
    _column_for_property,
    _object_hints_for,
    discover_bindings,
)
from app.services.ontology.queries import ensure_domains, get_object_by_name


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    session = Session()
    yield session
    session.close()
    eng.dispose()


ORG = "org-saas"


class FakeSchemaService:
    """Two tenant sources: a Salesforce org (known connector vocabulary)
    and a plain Postgres warehouse with naming NO hint list knows."""

    TABLES = {
        "sf1": ["Account", "Contact", "Opportunity", "User",
                "Product2", "PricebookEntry"],
        "pg1": ["crm_opportunity", "hr_people", "cust_master"],
    }
    COLUMNS = {
        "Account": ["Id", "AccountName", "AccountNumber", "Industry",
                    "AnnualRevenue", "CreatedDate"],
        "Contact": ["Id", "FirstName", "LastName", "Email"],
        "Opportunity": ["Id", "OpportunityName", "Amount", "StageName",
                        "CloseDate", "AccountId"],
        "User": ["Id", "FullName", "Email", "Title"],
        "Product2": ["Id", "ProductName", "ProductCode", "Description"],
        "PricebookEntry": ["Id", "UnitPrice", "ProductId"],
        "crm_opportunity": ["opp_id", "opp_name", "opp_value", "stage",
                            "close_dt"],
        "hr_people": ["person_id", "person_full_name", "person_email",
                      "dept_name"],
        "cust_master": ["cust_id", "cust_name", "cust_tier", "region_nm"],
    }

    def __init__(self, db):
        pass

    def list_tables(self, kb_id):
        return {"tables": [{"name": t} for t in self.TABLES.get(kb_id, [])]}

    def describe_table(self, kb_id, table):
        return {"columns": [
            {"name": c} for c in self.COLUMNS.get(table, [])
        ]}


@pytest.fixture()
def kbs(db):
    from app.models.knowledge_base import KnowledgeBase

    db.add(KnowledgeBase(
        id="sf1", org_id=ORG, name="tenant-crm", source_kind="database",
        db_type="salesforce", status="ready"))
    db.add(KnowledgeBase(
        id="pg1", org_id=ORG, name="tenant-warehouse", source_kind="database",
        db_type="postgres", status="ready"))
    db.commit()
    return db


@pytest.fixture()
def patched_schema(monkeypatch):
    import app.services.db.schema_service as schema_module

    monkeypatch.setattr(schema_module, "SchemaService", FakeSchemaService)


@pytest.fixture()
def patched_pg_only(monkeypatch):
    """Only the alien Postgres source — no connector hints can win."""
    import app.services.db.schema_service as schema_module

    class PgOnlySchemaService(FakeSchemaService):
        TABLES = {"pg1": FakeSchemaService.TABLES["pg1"]}
        COLUMNS = {k: v for k, v in FakeSchemaService.COLUMNS.items()
                   if k in FakeSchemaService.TABLES["pg1"]}

    monkeypatch.setattr(schema_module, "SchemaService", PgOnlySchemaService)


def _patch_llm(monkeypatch, verdict_by_table):
    import re as _re

    import app.services.llm_service as llm_module

    def fake(prompt, schema=None, temperature=0.0, **kw):
        for table, verdict in verdict_by_table.items():
            if table in prompt:
                out = dict(verdict)
                m = _re.search(r"\[(\d+)\] table=" + _re.escape(table), prompt)
                out["table_index"] = int(m.group(1)) if m else 0
                return out
        return {}

    monkeypatch.setattr(llm_module, "chat_completion_json_sync", fake)


def _prop(obj, name):
    return next(p for p in (obj.properties_json or [])
                if isinstance(p, dict) and p.get("name") == name)


# ── Layer 1: connector hints ─────────────────────────────────────────────

def test_connector_hints_merge_per_db_type():
    assert "account" in _object_hints_for("Customer", "salesforce")
    assert "kna1" in _object_hints_for("Customer", "sap")
    # unknown connector → global hints only
    assert _object_hints_for("Customer", "windchill") == \
        _object_hints_for("Customer", None)
    # property hints are connector-aware too
    assert _column_for_property("code", ["AccountNumber"], "salesforce") \
        == "AccountNumber"
    assert _column_for_property("code", ["KUNNR"], "sap") == "KUNNR"


def test_salesforce_connector_binds_account_to_customer(
    db, kbs, patched_schema, monkeypatch,
):
    ensure_domains(db, ORG)
    monkeypatch.setattr(
        "app.config.settings.ONTOLOGY_LLM_DISCOVERY_ENABLED", False,
        raising=False)
    out = discover_bindings(db, ORG, apply=True)
    by_obj = {r["object"]: r for r in out["report"]}
    cust = by_obj.get("Customer")
    assert cust and cust["status"] == "bound" and cust["method"] == "hint"
    obj = get_object_by_name(db, ORG, "Customer")
    assert obj.backing_ref == "kb:sf1:Account"
    assert _prop(obj, "name")["column"] == "AccountName"


# ── Layer 3: LLM-assisted matching, confidence-gated ─────────────────────

def test_llm_autobinds_high_confidence(db, kbs, patched_pg_only, monkeypatch):
    ensure_domains(db, ORG)
    monkeypatch.setattr(
        "app.config.settings.ONTOLOGY_LLM_DISCOVERY_ENABLED", True,
        raising=False)
    _patch_llm(monkeypatch, {
        "crm_opportunity": {"match": True, "confidence": 0.9,
                            "properties": {"name": "opp_name",
                                           "amount": "opp_value",
                                           "date": "close_dt"}},
    })
    out = discover_bindings(db, ORG, apply=True)
    by_obj = {r["object"]: r for r in out["report"]}
    order = by_obj.get("Order")
    assert order and order["status"] == "bound" and order["method"] == "llm"
    assert order["properties_mapped"]["name"] == "opp_name"
    obj = get_object_by_name(db, ORG, "Order")
    assert obj.backing_ref == "kb:pg1:crm_opportunity"


def test_llm_low_confidence_is_proposed_never_bound(
    db, kbs, patched_pg_only, monkeypatch,
):
    ensure_domains(db, ORG)
    monkeypatch.setattr(
        "app.config.settings.ONTOLOGY_LLM_DISCOVERY_ENABLED", True,
        raising=False)
    _patch_llm(monkeypatch, {
        "hr_people": {"match": True, "confidence": 0.4,
                      "properties": {"name": "person_full_name"}},
    })
    out = discover_bindings(db, ORG, apply=True)
    by_obj = {r["object"]: r for r in out["report"]}
    emp = by_obj.get("Employee")
    assert emp["status"] == "proposed"
    obj = get_object_by_name(db, ORG, "Employee")
    assert obj.backing_kind == "manual"  # NOT bound


def test_llm_invented_columns_are_dropped(db, kbs, patched_pg_only, monkeypatch):
    ensure_domains(db, ORG)
    monkeypatch.setattr(
        "app.config.settings.ONTOLOGY_LLM_DISCOVERY_ENABLED", True,
        raising=False)
    _patch_llm(monkeypatch, {
        "hr_people": {"match": True, "confidence": 0.95,
                      "properties": {"name": "person_full_name",
                                     "department": "NOT_A_REAL_COLUMN"}},
    })
    out = discover_bindings(db, ORG, apply=True)
    by_obj = {r["object"]: r for r in out["report"]}
    emp = by_obj.get("Employee")
    assert emp["status"] == "bound"
    assert emp["properties_mapped"] == {"name": "person_full_name"}
    assert "department" in emp["properties_unmapped"]
    # the invented column never reaches the persisted object
    obj = get_object_by_name(db, ORG, "Employee")
    assert _prop(obj, "name")["column"] == "person_full_name"
    assert "column" not in _prop(obj, "department")


def test_llm_unavailable_binds_nothing(db, kbs, patched_pg_only, monkeypatch):
    ensure_domains(db, ORG)
    monkeypatch.setattr(
        "app.config.settings.ONTOLOGY_LLM_DISCOVERY_ENABLED", True,
        raising=False)
    _patch_llm(monkeypatch, {})  # LLM returns {} everywhere
    out = discover_bindings(db, ORG, apply=True)
    by_obj = {r["object"]: r for r in out["report"]}
    assert by_obj.get("Employee", {}).get("status") == "no-table-found"
    assert get_object_by_name(db, ORG, "Employee").backing_kind == "manual"


def test_never_overwrites_human_binding(db, kbs, patched_pg_only, monkeypatch):
    ensure_domains(db, ORG)
    obj = get_object_by_name(db, ORG, "Customer")
    obj.backing_kind = "table"
    obj.backing_ref = "kb:sf1:Contact"  # a human chose Contact
    db.commit()
    _patch_llm(monkeypatch, {})
    discover_bindings(db, ORG, apply=True)
    assert get_object_by_name(db, ORG, "Customer").backing_ref == "kb:sf1:Contact"
