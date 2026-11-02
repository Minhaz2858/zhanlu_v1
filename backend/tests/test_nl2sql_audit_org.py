"""Phase 7C T13 — nl2sql audit rows are stamped with the real tenant org.

Before 7C ``_write_audit`` set no org_id, so every nl2sql audit row fell
to the ``default-org`` server default regardless of the requesting
tenant — a cross-tenant mis-attribution in the audit trail.
"""

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.agent_data_binding import AgentDataBinding
from app.models.audit_event import AuditEvent
from app.services.nl2sql import _write_audit

ORG = "org-acme"


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    Base.metadata.create_all(eng, tables=[
        AgentDataBinding.__table__, AuditEvent.__table__])
    s = sessionmaker(bind=eng)()
    s.add(AgentDataBinding(id="bd-1", org_id=ORG, agent_app_id="app-1",
                           datasource_id="kb-1"))
    s.commit()
    yield s
    s.close()
    eng.dispose()


def test_audit_row_carries_binding_org(db):
    _write_audit(db, "bd-1", "hash-1", 5, None, decision="allowed")
    row = db.query(AuditEvent).filter_by(trace_id="bd-1").one()
    assert row.org_id == ORG


def test_audit_row_without_binding_still_writes(db):
    _write_audit(db, "bd-missing", "hash-2", 0, "no binding",
                 decision="denied")
    row = db.query(AuditEvent).filter_by(trace_id="bd-missing").one()
    assert row.event_type == "nl2sql_query"  # fail-open write survives
