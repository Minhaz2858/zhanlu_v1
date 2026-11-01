"""Phase 5C — scheduled ontology drift monitoring (lifespan watchdog).

Turns Phase 4's manual drift/re-onboarding into a continuous process —
WITHOUT a new scheduling platform: the watchdog reuses the repository's
existing lifespan-watchdog pattern (see catalog_watchdog) and the Phase 4
onboarding pipeline (NO second discovery engine).

Honesty/robustness rules verified here:
- disabled watchdog performs NO scans;
- unchanged resources are never reprocessed (digest short-circuit);
- drifted resources are re-onboarded via ``on_resource_changed``;
- removed catalog tables become stale references; verified evidence
  survives; nothing is ever deleted because a resource changed;
- one failing resource never stops the others; failures back off;
- in-flight resources are never double-onboarded;
- per-org isolation is preserved (the run's org drives every call).
"""

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.services.ontology.enterprise_discovery as ed
import app.services.enterprise.drift_watchdog as dw
from app.database import Base
from app.models.audit_event import AuditEvent
from app.models.enterprise_onboarding import EnterpriseOnboardingRun
from app.models.enterprise_process import (
    BusinessProcess, EnterpriseResource, ProcessEvidence)
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_catalog import KBTableMeta
from app.models.ontology import OntologyAction, OntologyLink, OntologyObject
from app.services.enterprise.onboarding import (
    get_onboarding_status, onboard_resource)

ORG = "org-acme"
ORG2 = "org-other"

INV = {
    "datasources": [{"id": "kb-1", "name": "Sales DW", "kb_type": "database"},
                    {"id": "kb-2", "name": "Fin DW", "kb_type": "database"}],
    "documents": [], "ontology": {"actions": [], "objects": []},
    "automations": [],
}


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    Base.metadata.create_all(eng, tables=[
        EnterpriseOnboardingRun.__table__, EnterpriseResource.__table__,
        KBTableMeta.__table__, BusinessProcess.__table__,
        ProcessEvidence.__table__, OntologyObject.__table__,
        OntologyLink.__table__, OntologyAction.__table__,
        AuditEvent.__table__, KnowledgeBase.__table__])
    s = sessionmaker(bind=eng)()
    yield s
    s.close()
    eng.dispose()


@pytest.fixture(autouse=True)
def _reset_state(db, monkeypatch):
    # point the watchdog's SessionLocal at the test DB (scan_once opens
    # its own per-candidate sessions)
    monkeypatch.setattr("app.services.enterprise.drift_watchdog.SessionLocal",
                        sessionmaker(bind=db.get_bind()))
    dw._IN_FLIGHT.clear()
    dw._FAILURES.clear()
    dw._LAST_CYCLE.clear()
    yield
    dw._IN_FLIGHT.clear()
    dw._FAILURES.clear()
    dw._LAST_CYCLE.clear()


@pytest.fixture()
def discovery(monkeypatch):
    def _inv(db, org_id):
        return INV if org_id == ORG else {
            "datasources": [], "documents": [],
            "ontology": {"actions": [], "objects": []}, "automations": []}

    def _propose(db, org_id, llm_fn=None):
        return {"org_id": org_id, "llm_ok": True, "proposals": []}

    monkeypatch.setattr(ed, "inventory_resources", _inv)
    monkeypatch.setattr(ed, "propose_operating_model", _propose)


def _tables(db, kb_id, names, org=ORG):
    if db.query(KnowledgeBase).filter_by(id=kb_id).first() is None:
        db.add(KnowledgeBase(id=kb_id, org_id=org, name=f"KB {kb_id}"))
    for t in names:
        db.add(KBTableMeta(kb_id=kb_id, table_name=t))
    db.commit()


def _onboard(db, kb_id="kb-1", org=ORG):
    return onboard_resource(db, org, "datasource", kb_id)


# 1. enablement -----------------------------------------------------------------------

def test_disabled_watchdog_performs_no_scan(db, monkeypatch):
    _tables(db, "kb-1", ["sales_order"])
    _onboard(db)
    calls = {"n": 0}
    monkeypatch.setattr(dw, "scan_once",
                        lambda: calls.update(n=calls["n"] + 1))
    monkeypatch.setattr(dw.settings, "ONTOLOGY_DRIFT_WATCHDOG_ENABLED", False)

    async def _run():
        stop = asyncio.Event()
        task = asyncio.create_task(dw.run_drift_watchdog(stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(_run())
    assert calls["n"] == 0  # disabled → no scans, ever


def test_watchdog_starts_and_stops(db, monkeypatch):
    _tables(db, "kb-1", ["sales_order"])
    _onboard(db)
    calls = {"n": 0}

    def _scan():
        calls["n"] += 1

    monkeypatch.setattr(dw, "scan_once", _scan)
    monkeypatch.setattr(dw.settings, "ONTOLOGY_DRIFT_WATCHDOG_ENABLED", True)
    monkeypatch.setattr(dw.settings,
                        "ONTOLOGY_DRIFT_WATCHDOG_INTERVAL_SECONDS", 1)

    async def _run():
        stop = asyncio.Event()
        task = asyncio.create_task(dw.run_drift_watchdog(stop))
        await asyncio.sleep(0.1)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)
        return task

    task = asyncio.run(_run())
    assert task.done() and not task.cancelled()
    assert calls["n"] >= 1  # at least one scan happened, then clean exit


# 2. drift behavior --------------------------------------------------------------------

def test_unchanged_resources_not_reprocessed(db, discovery, monkeypatch):
    _tables(db, "kb-1", ["sales_order"])
    _onboard(db)
    before = db.query(EnterpriseOnboardingRun).count()
    monkeypatch.setattr(dw.settings, "ONTOLOGY_DRIFT_WATCHDOG_ENABLED", True)
    summary = dw.scan_once()
    after = db.query(EnterpriseOnboardingRun).count()
    assert summary["drifted"] == 0
    assert after == before  # no new onboarding runs for unchanged resource


def test_changed_resource_triggers_onboarding(db, discovery, monkeypatch):
    _tables(db, "kb-1", ["sales_order"])
    _onboard(db)
    _tables(db, "kb-1", ["region_dim"])  # catalog drift
    monkeypatch.setattr(dw.settings, "ONTOLOGY_DRIFT_WATCHDOG_ENABLED", True)
    summary = dw.scan_once()
    assert summary["drifted"] == 1
    runs = db.query(EnterpriseOnboardingRun).order_by(
        EnterpriseOnboardingRun.created_date).all()
    assert runs[-1].status == "completed"
    assert runs[-1].attempt == 2  # re-onboard after drift (retry=drifted)


def test_removed_catalog_tables_become_stale_references(
        db, discovery, monkeypatch):
    _tables(db, "kb-1", ["sales_order", "old_fact"])
    _onboard(db)
    db.query(KBTableMeta).filter_by(kb_id="kb-1", table_name="old_fact") \
        .update({"is_deleted": True})
    db.commit()
    monkeypatch.setattr(dw.settings, "ONTOLOGY_DRIFT_WATCHDOG_ENABLED", True)
    dw.scan_once()
    latest = db.query(EnterpriseOnboardingRun).order_by(
        EnterpriseOnboardingRun.created_date.desc()).first()
    steps = {s["step"]: s for s in latest.steps_json}
    assert "stale_references" in steps
    assert "old_fact" in steps["stale_references"]["detail"]["removed_tables"]


def test_verified_evidence_survives_drift(db, discovery, monkeypatch):
    _tables(db, "kb-1", ["sales_order"])
    _onboard(db)
    db.add(BusinessProcess(id="proc-1", org_id=ORG, name="P",
                           discovery_status="documented",
                           evidence_status="verified"))
    db.add(ProcessEvidence(id="ev-1", org_id=ORG, process_id="proc-1",
                           evidence_kind="table_catalog",
                           source_ref="table://sales_order",
                           evidence_status="verified"))
    db.commit()
    _tables(db, "kb-1", ["region_dim"])  # drift
    monkeypatch.setattr(dw.settings, "ONTOLOGY_DRIFT_WATCHDOG_ENABLED", True)
    dw.scan_once()
    ev = db.query(ProcessEvidence).filter_by(id="ev-1").one()
    assert ev.evidence_status == "verified"  # never touched by drift


def test_scan_bounded_by_max_resources(db, discovery, monkeypatch):
    _tables(db, "kb-1", ["sales_order"])
    _tables(db, "kb-2", ["finance_fact"])
    _onboard(db, "kb-1")
    _onboard(db, "kb-2")
    _tables(db, "kb-1", ["region_dim"])
    _tables(db, "kb-2", ["budget_dim"])
    monkeypatch.setattr(dw.settings, "ONTOLOGY_DRIFT_WATCHDOG_ENABLED", True)
    monkeypatch.setattr(dw.settings,
                        "ONTOLOGY_DRIFT_WATCHDOG_MAX_RESOURCES_PER_CYCLE", 1)
    summary = dw.scan_once()
    assert summary["scanned"] == 1  # bounded batch, remainder waits


# 3. failure isolation + backoff ---------------------------------------------------------

def test_one_resource_failure_does_not_stop_others(db, discovery, monkeypatch):
    _tables(db, "kb-1", ["sales_order"])
    _tables(db, "kb-2", ["finance_fact"])
    _onboard(db, "kb-1")
    _onboard(db, "kb-2")
    _tables(db, "kb-1", ["region_dim"])
    _tables(db, "kb-2", ["budget_dim"])
    monkeypatch.setattr(dw.settings, "ONTOLOGY_DRIFT_WATCHDOG_ENABLED", True)

    real = dw.on_resource_changed

    def _flaky(db, org_id, rtype, rid, **kw):
        if rid == "kb-1":
            raise RuntimeError("connector exploded")
        return real(db, org_id, rtype, rid, **kw)

    monkeypatch.setattr(dw, "on_resource_changed", _flaky)
    summary = dw.scan_once()
    assert summary["failed"] >= 1
    assert summary["drifted"] == 1  # kb-2 still re-onboarded


def test_failed_resource_backs_off(db, discovery, monkeypatch):
    _tables(db, "kb-1", ["sales_order"])
    _onboard(db)
    _tables(db, "kb-1", ["region_dim"])
    monkeypatch.setattr(dw.settings, "ONTOLOGY_DRIFT_WATCHDOG_ENABLED", True)
    monkeypatch.setattr(dw.settings,
                        "ONTOLOGY_DRIFT_WATCHDOG_RETRY_BACKOFF_SECONDS", 3600)

    def _flaky(db, org_id, rtype, rid, **kw):
        raise RuntimeError("still broken")

    monkeypatch.setattr(dw, "on_resource_changed", _flaky)
    dw.scan_once()
    first = dw.get_drift_status()
    assert first["failed"] >= 1
    # backoff not yet due → the next cycle skips the resource entirely
    monkeypatch.setattr(dw, "on_resource_changed",
                        lambda *a, **k: pytest.fail("must not be called"))
    second = dw.scan_once()
    assert second["scanned"] == 0  # in backoff — not even scanned


# 4. concurrency + isolation ---------------------------------------------------------------

def test_in_flight_resource_not_double_onboarded(db, discovery, monkeypatch):
    _tables(db, "kb-1", ["sales_order"])
    _onboard(db)
    _tables(db, "kb-1", ["region_dim"])
    monkeypatch.setattr(dw.settings, "ONTOLOGY_DRIFT_WATCHDOG_ENABLED", True)
    assert dw.try_mark_in_flight((ORG, "datasource", "kb-1")) is True
    summary = dw.scan_once()
    assert summary["skipped_in_flight"] == 1


def test_cross_org_isolation(db, discovery, monkeypatch):
    _tables(db, "kb-1", ["sales_order"])
    _onboard(db, org=ORG)
    _tables(db, "kb-1", ["region_dim"], org=ORG)
    monkeypatch.setattr(dw.settings, "ONTOLOGY_DRIFT_WATCHDOG_ENABLED", True)
    summary = dw.scan_once()
    # only ORG's resource is a candidate; ORG2 has no onboarding runs
    assert summary["scanned"] == 1
    orgs = {r.org_id for r in db.query(EnterpriseOnboardingRun).all()}
    assert orgs == {ORG}


def test_manual_onboarding_remains_functional(db, discovery):
    _tables(db, "kb-1", ["sales_order"])
    res = onboard_resource(db, ORG, "datasource", "kb-1")
    assert res["status"] == "completed"
    st = get_onboarding_status(db, ORG, "datasource", "kb-1")
    assert st["latest"]["status"] == "completed"


# 5. status surface --------------------------------------------------------------------------

def test_drift_status_reports_cycle_info(db, discovery, monkeypatch):
    _tables(db, "kb-1", ["sales_order"])
    _onboard(db)
    monkeypatch.setattr(dw.settings, "ONTOLOGY_DRIFT_WATCHDOG_ENABLED", True)
    dw.scan_once()
    st = dw.get_drift_status()
    assert st["last_run"] is not None
    assert st["next_run"] is not None
    assert st["scanned"] == 1
