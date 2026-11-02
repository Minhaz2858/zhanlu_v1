"""Phase 7C T8/T9 — drift watchdog stops spinning on deleted resources.

Before 7C: candidates came from EnterpriseOnboardingRun triples with no
existence check. A cleaned-up KB produced an empty-table digest != last
digest → a "resource not found" failed run row was committed EVERY cycle,
forever (normal return → no backoff). Deleted resources also never left
the candidate set (mcp/automation skip honestly but stay candidates).
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.enterprise_onboarding import EnterpriseOnboardingRun
from app.models.enterprise_process import EnterpriseResource
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_catalog import (
    KBColumnMeta, KBTableMeta, KBTableRelation)
from app.services.enterprise import drift_watchdog

ORG = "org-acme"


@pytest.fixture()
def db(monkeypatch):
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    Base.metadata.create_all(eng, tables=[
        KnowledgeBase.__table__, KBTableMeta.__table__,
        KBColumnMeta.__table__, KBTableRelation.__table__,
        EnterpriseOnboardingRun.__table__, EnterpriseResource.__table__,
    ])
    s = sessionmaker(bind=eng)()
    # the watchdog opens its own per-candidate session via the module-level
    # SessionLocal — point it at this test's engine (documented gotcha)
    monkeypatch.setattr(drift_watchdog, "SessionLocal",
                        sessionmaker(bind=eng))
    yield s
    s.close()
    eng.dispose()


def _seed_kb(db, kb_id, deleted=False):
    db.add(KnowledgeBase(id=kb_id, name="DB", org_id=ORG,
                         source_kind="database", is_deleted=deleted))
    db.add(KBTableMeta(id=f"tm-{kb_id}", kb_id=kb_id, org_id=ORG,
                       table_name="orders"))
    db.add(EnterpriseOnboardingRun(
        id=f"run-{kb_id}", org_id=ORG, resource_type="knowledge_base",
        resource_id=kb_id, status="completed", inventory_digest="dig-old",
        steps_json={}, attempt=1,
        created_date=datetime.now(timezone.utc),
        updated_date=datetime.now(timezone.utc)))
    db.commit()


def test_deleted_resource_is_skipped_and_tombstoned(db):
    _seed_kb(db, "kb-gone", deleted=True)  # cleaned-up KB (soft-deleted)
    drift_watchdog.clear_failure_state_for_tests()
    summary = drift_watchdog.scan_once(db)
    assert summary["skipped_missing"] == 1
    # no failed run row was created for the deleted resource
    rows = db.query(EnterpriseOnboardingRun).filter_by(
        resource_id="kb-gone").all()
    assert all(r.is_deleted for r in rows)  # tombstoned → candidate gone
    assert all((r.status or "") != "failed" for r in rows)


def test_next_cycle_has_no_candidates_after_tombstone(db):
    _seed_kb(db, "kb-gone", deleted=True)
    drift_watchdog.clear_failure_state_for_tests()
    drift_watchdog.scan_once(db)
    summary2 = drift_watchdog.scan_once(db)
    assert summary2["scanned"] == 0
    assert summary2["skipped_missing"] == 0  # candidate set is empty now


def test_live_resource_still_processed(db):
    _seed_kb(db, "kb-live")
    drift_watchdog.clear_failure_state_for_tests()
    summary = drift_watchdog.scan_once(db)
    assert summary["scanned"] == 1  # live resources are unaffected


def test_digest_is_org_scoped(db):
    # T9: the inventory digest must filter KBTableMeta by org too
    from app.services.enterprise.onboarding import _resource_digest
    other = "org-other"
    db.add(KnowledgeBase(id="kb-x", name="X", org_id=other,
                         source_kind="database"))
    db.add(KBTableMeta(id="tm-other", kb_id="kb-x", org_id=other,
                       table_name="secret"))
    db.commit()
    digest = _resource_digest(db, ORG, "knowledge_base", "kb-x")
    # kb-x belongs to another org — the digest for ORG must not see its
    # catalog (and onboarding existence checks refuse it anyway)
    assert digest != _resource_digest(db, other, "knowledge_base", "kb-x")
