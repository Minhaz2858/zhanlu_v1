"""P6 live drift (workstream 3) — REAL schema-change → health transition.

These prove the full chain against a KB that HAS a catalog baseline: a real
ALTER/DROP on the source produces the correct binding_status transition and a
drift revision, and report-only reconciliation does NOT consume the pending
change.

Uses the shared sandbox (sandbox_seed) for the metadata + catalog baseline, and
a separate, mutable data sqlite file (sales_transactions) that we alter live.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.models.base import Base
from app.models.knowledge_base import KnowledgeBase
from app.models.ontology import OntologyObject
import app.models.ontology_revision  # noqa: F401
from app.services.ontology.evolution import reconcile
from tests.sandbox_seed import (
    ORG, KB_SALES, make_engine, make_session, create_data_db, build_sandbox,
)


@pytest.fixture()
def env(tmp_path):
    eng = make_engine()
    data_path = str(tmp_path / "sales.db")
    create_data_db(data_path)
    session = make_session(eng)
    build_sandbox(session, data_path)
    # A table-backed object bound to a column we will drop / a table we will drop.
    return eng, session, data_path


def _add_table_object(session, name, ref, columns, status="ok"):
    o = OntologyObject(
        id=f"obj-{name}", org_id=ORG, domain_id="auto",
        name=name, display_name=name, backing_kind="table",
        backing_ref=ref, binding_status=status,
        properties_json=[{"column": c} for c in columns],
        evidence_status="verified", confidence=0.9, is_active=True)
    session.add(o)
    session.commit()
    return o


def _mutate(path, sql):
    conn = sqlite3.connect(path)
    conn.execute(sql)
    conn.commit()
    conn.close()


def _drift_revisions(session):
    from app.models.ontology_revision import OntologyRevision
    return session.query(OntologyRevision).filter(
        OntologyRevision.change_kind == "drift").all()


# --- Three distinct real transitions ----------------------------------------

def test_removed_column_marks_binding_stale(env):
    eng, session, data_path = env
    _add_table_object(session, "SalesCost", f"kb:{KB_SALES}:sales_transactions",
                     ["cost_amount"])
    _mutate(data_path, "ALTER TABLE sales_transactions DROP COLUMN cost_amount")
    report = reconcile(session, ORG, apply=True)
    obj = session.query(OntologyObject).filter(
        OntologyObject.id == "obj-SalesCost").first()
    assert obj.binding_status == "stale"
    assert any(a.get("action") == "mark_stale" for a in report["actions"])
    assert len(_drift_revisions(session)) >= 1


def test_removed_table_marks_binding_broken(env):
    eng, session, data_path = env
    _add_table_object(session, "SalesAll", f"kb:{KB_SALES}:sales_transactions",
                     ["sale_amount"])
    _mutate(data_path, "DROP TABLE sales_transactions")
    report = reconcile(session, ORG, apply=True)
    obj = session.query(OntologyObject).filter(
        OntologyObject.id == "obj-SalesAll").first()
    assert obj.binding_status == "broken"
    assert any(a.get("action") == "mark_broken" for a in report["actions"])
    assert len(_drift_revisions(session)) >= 1


def test_unrelated_additive_column_keeps_binding_healthy(env):
    eng, session, data_path = env
    _add_table_object(session, "SalesAmt", f"kb:{KB_SALES}:sales_transactions",
                     ["sale_amount"], status="ok")
    _mutate(data_path,
            "ALTER TABLE sales_transactions ADD COLUMN new_flag TEXT")
    reconcile(session, ORG, apply=True)
    obj = session.query(OntologyObject).filter(
        OntologyObject.id == "obj-SalesAmt").first()
    # Additive change must NOT degrade the binding.
    assert obj.binding_status == "ok"


# --- Report-only must not consume the pending change ------------------------

def test_report_only_does_not_consume_pending_change(env):
    eng, session, data_path = env
    _add_table_object(session, "SalesCost2", f"kb:{KB_SALES}:sales_transactions",
                     ["cost_amount"], status="ok")
    _mutate(data_path, "ALTER TABLE sales_transactions DROP COLUMN cost_amount")

    # Report-only: change is detected in report["changes"] but nothing mutates
    # and the signature ledger is NOT advanced (so the follow-up apply still
    # sees the drift).
    report = reconcile(session, ORG, apply=False)
    obj = session.query(OntologyObject).filter(
        OntologyObject.id == "obj-SalesCost2").first()
    assert obj.binding_status == "ok"  # untouched
    drift_recorded = any(
        any(f[0] == "column_removed" for f in c["facts"])
        for c in report.get("changes", []))
    assert drift_recorded

    # The follow-up apply run still detects the drift (change was NOT consumed).
    reconcile(session, ORG, apply=True)
    obj = session.query(OntologyObject).filter(
        OntologyObject.id == "obj-SalesCost2").first()
    assert obj.binding_status == "stale"


def test_drift_revision_recorded(env):
    eng, session, data_path = env
    _add_table_object(session, "SalesCost3", f"kb:{KB_SALES}:sales_transactions",
                     ["cost_amount"])
    _mutate(data_path, "ALTER TABLE sales_transactions DROP COLUMN cost_amount")
    reconcile(session, ORG, apply=True)
    revs = _drift_revisions(session)
    assert len(revs) >= 1
    assert all(r.change_kind == "drift" for r in revs)
