"""P3-6 escalation tests — create with context + audit, list filter,
resolve closes the loop, agent tool handler packages the handoff."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.escalation import Escalation

ORG = "org-esc"


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


def test_create_packages_context_and_audits(db):
    from app.models.audit_event import AuditEvent
    from app.services.ontology.escalation import create_escalation

    out = create_escalation(
        db, ORG, "Customer needs a refund our tools cannot process",
        context={"tried": ["read_object_data Ticket", "check_policy"],
                 "ticket_id": "T-99"},
        agent_name="support-agent", sentiment="frustrated",
        conversation_id="conv-1", created_by="u1",
    )
    esc = db.query(Escalation).filter(Escalation.id == out["id"]).first()
    assert esc and esc.status == "open"
    assert esc.context_json and "tried" in esc.context_json
    audits = db.query(AuditEvent).filter(
        AuditEvent.event_type == "escalation_created").all()
    assert len(audits) == 1
    assert audits[0].detail_json["sentiment"] == "frustrated"


def test_list_status_filter(db):
    from app.services.ontology.escalation import create_escalation, list_escalations

    create_escalation(db, ORG, "first")
    create_escalation(db, ORG, "second")
    create_escalation(db, "org-other", "other org")  # tenant-isolated
    assert len(list_escalations(db, ORG)) == 2
    first_id = list_escalations(db, ORG)[0]["id"]
    from app.services.ontology.escalation import resolve_escalation
    resolve_escalation(db, ORG, first_id, resolved_by="u1", note="done")
    open_items = list_escalations(db, ORG, status="open")
    resolved = list_escalations(db, ORG, status="resolved")
    assert len(open_items) == 1 and len(resolved) == 1
    assert resolved[0]["resolution_note"] == "done"


def test_resolve_unknown_404(db):
    from app.services.ontology.escalation import resolve_escalation

    with pytest.raises(LookupError):
        resolve_escalation(db, ORG, "nope", resolved_by="u1", note=None)


def test_agent_tool_handler(db, monkeypatch):
    import app.services.tool_handlers.ontology_tools as ot

    # no fan-out object → no external calls needed
    out = asyncio.run(ot._escalate_to_human(
        {"summary": "user asked for a human",
         "context": {"attempts": 2}, "sentiment": "asks_human"},
        db, "u2", {"org_id": ORG, "conversation_id": "conv-9",
                   "agent_name": "general_assistant"},
    ))
    assert out["status"] == "open"
    esc = db.query(Escalation).filter(Escalation.id == out["id"]).first()
    assert esc.agent_name == "general_assistant"
    assert esc.conversation_id == "conv-9"
    assert esc.sentiment == "asks_human"


def test_tool_requires_summary():
    import app.services.tool_handlers.ontology_tools as ot

    out = asyncio.run(ot._escalate_to_human({}, None, None, {"org_id": ORG}))
    assert not out["success"]


def test_escalate_tool_registered():
    import app.services.tool_handlers  # noqa: F401 — trigger registration
    from app.services.tool_registry import registry

    assert registry.get_entry("escalate_to_human") is not None
