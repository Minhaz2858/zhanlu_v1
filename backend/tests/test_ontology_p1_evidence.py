"""P1 unified evidence taxonomy — central module, model fields, stamping.

Canonical statuses (services/ontology/evidence.py):
verified | computed | inferred | proposed | unavailable
Never auto-classify uncertain legacy rows as verified; confidence is
never fabricated.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.ontology import OntologyAction, OntologyObject

ORG = "org-test"


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


def _obj(db, backing_kind="manual", backing_ref=None):
    from app.models.ontology import OntologyDomain
    dom = (
        db.query(OntologyDomain)
        .filter(OntologyDomain.org_id == ORG, OntologyDomain.code == "procurement")
        .first()
    )
    if not dom:
        dom = OntologyDomain(org_id=ORG, code="procurement", name="Procurement")
        db.add(dom)
        db.flush()
    from uuid import uuid4
    o = OntologyObject(
        org_id=ORG, domain_id=dom.id,
        name=f"Thing{backing_kind}{uuid4().hex[:6]}",
        backing_kind=backing_kind, backing_ref=backing_ref,
    )
    db.add(o)
    db.commit()
    return o


# ── Canonical statuses + pure backfill rules ────────────────────────────


def test_canonical_statuses_are_exactly_five():
    from app.services.ontology.evidence import EVIDENCE_STATUSES
    assert EVIDENCE_STATUSES == (
        "verified", "computed", "inferred", "proposed", "unavailable",
    )


def test_is_valid_and_normalize():
    from app.services.ontology.evidence import is_valid_status, normalize_status
    assert is_valid_status("verified") and is_valid_status("inferred")
    assert not is_valid_status("true") and not is_valid_status("")
    assert normalize_status(" Verified ") == "verified"
    with pytest.raises(ValueError):
        normalize_status("nonsense")


def test_legacy_backfill_rules_never_verify():
    """Never auto-classify uncertain legacy rows as verified."""
    from app.services.ontology.evidence import (
        legacy_backfill_status,
        legacy_backfill_status_action,
    )
    # bound objects → inferred (evidence exists, human authorization not recorded)
    assert legacy_backfill_status("table", "kb:x:erp_t_bd_supplier") == "inferred"
    # manual / unbound → proposed
    assert legacy_backfill_status("manual", None) == "proposed"
    assert legacy_backfill_status("manual", "") == "proposed"
    assert legacy_backfill_status("table", None) == "proposed"  # ref required
    # actions: bound tool → inferred, else proposed
    assert legacy_backfill_status_action("po_tool") == "inferred"
    assert legacy_backfill_status_action(None) == "proposed"
    assert legacy_backfill_status_action("") == "proposed"


def test_new_objects_default_to_proposed(db):
    o = _obj(db)
    assert o.evidence_status == "proposed"
    assert o.confidence is None            # never fabricated
    assert o.provenance_json is None


# ── Stamping helpers ────────────────────────────────────────────────────


def test_stamp_human_bind_sets_verified_with_provenance(db):
    from app.services.ontology.evidence import stamp_human_bind

    o = _obj(db, "table", "kb:kb1:erp_t_bd_customer")
    stamp_human_bind(o, bound_by="admin@zhanlu.dev")
    db.commit()
    assert o.evidence_status == "verified"
    assert o.confidence is None            # human confirmation isn't a score
    p = o.provenance_json
    assert p["verified_by"] == "admin@zhanlu.dev"
    assert p["backing_ref"] == "kb:kb1:erp_t_bd_customer"
    assert "verified_at" in p


def test_stamp_human_bind_reassigns_json(db):
    """The SQLAlchemy JSON-mutation gotcha: provenance must be REASSIGNED,
    never mutated in place (same bug class that shipped in discovery v1)."""
    from app.services.ontology.evidence import stamp_human_bind

    o = _obj(db, "table", "kb:kb1:t")
    o.provenance_json = {"old": True}
    stamp_human_bind(o, bound_by="boss")
    assert o.provenance_json["verified_by"] == "boss"
    assert o.provenance_json["old"] is True  # prior provenance preserved


def test_stamp_unbind_returns_object_to_proposed(db):
    from app.services.ontology.evidence import stamp_human_bind, stamp_unbind

    o = _obj(db, "table", "kb:kb1:t")
    stamp_human_bind(o, bound_by="boss")
    stamp_unbind(o)
    assert o.evidence_status == "proposed"


def test_stamp_discovery_sets_inferred_with_confidence(db):
    from app.services.ontology.evidence import stamp_discovery

    o = _obj(db, "manual")
    stamp_discovery(
        o, method="llm_discovery", confidence=0.9,
        kb_id="kb1", table="party_master",
    )
    db.commit()
    assert o.evidence_status == "inferred"   # NEVER verified from discovery
    assert o.confidence == 0.9
    p = o.provenance_json
    assert p["method"] == "llm_discovery"
    assert p["kb_id"] == "kb1" and p["table"] == "party_master"


def test_stamp_discovery_confidence_is_optional_for_hint_matches(db):
    from app.services.ontology.evidence import stamp_discovery

    o = _obj(db, "manual")
    stamp_discovery(o, method="hint_match", confidence=None,
                    kb_id="kb1", table="t")
    assert o.evidence_status == "inferred"
    assert o.confidence is None              # honesty: hints carry no score


def test_discovery_can_never_set_verified(db, monkeypatch):
    """The discovery stamp refuses evidence_status overrides to verified —
    only stamp_human_bind can classify a row as verified."""
    from app.services.ontology.evidence import stamp_discovery

    o = _obj(db, "manual")
    with pytest.raises(ValueError):
        stamp_discovery(o, method="llm_discovery", confidence=0.99,
                        kb_id="kb1", table="t", status="verified")


def test_apply_binding_stamps_inferred_evidence(db):
    """Integration: discovery's _apply_binding (the only autobind writer)
    lands objects at inferred with method + confidence provenance."""
    from app.services.ontology.discovery import _apply_binding
    from app.services.ontology.evidence import STATUS_INFERRED

    o = _obj(db, "manual")
    _apply_binding(o, "kb1", "party_master",
                   {"name": "party_full_nm"}, method="llm_discovery",
                   confidence=0.9)
    db.commit()
    assert o.backing_ref == "kb:kb1:party_master"
    assert o.evidence_status == STATUS_INFERRED
    assert o.confidence == 0.9
    assert o.provenance_json["method"] == "llm_discovery"

    # Hint path: no fabricated score
    o2 = _obj(db, "manual")
    _apply_binding(o2, "kb1", "t", {})
    assert o2.evidence_status == STATUS_INFERRED
    assert o2.confidence is None


def test_unbind_clears_stale_confidence_and_supersedes_provenance(db):
    """Review fix: an unbound proposal must not display a score, and a
    re-discovery cycle must not inherit stale verified_by/method keys."""
    from app.services.ontology.discovery import _apply_binding
    from app.services.ontology.evidence import (
        stamp_human_bind, stamp_unbind,
    )

    o = _obj(db, "manual")
    _apply_binding(o, "kb1", "t", {"name": "n"}, method="llm_discovery",
                   confidence=0.9)
    stamp_human_bind(o, bound_by="boss")
    stamp_unbind(o)
    db.commit()
    assert o.evidence_status == "proposed"
    assert o.confidence is None
    p = o.provenance_json
    assert p["verified_by"] is None and p["method"] is None
    assert "unbound_at" in p


# ── Serialization carries the canonical fields ──────────────────────────


def test_serialize_emits_evidence_fields(db):
    from app.services.ontology.evidence import stamp_discovery
    from app.services.ontology.serialize import object_to_dict

    o = _obj(db, "manual")
    stamp_discovery(o, method="llm_discovery", confidence=0.85,
                    kb_id="kb1", table="t")
    d = object_to_dict(o)
    assert d["evidence_status"] == "inferred"
    assert d["confidence"] == 0.85
    assert d["provenance"]["method"] == "llm_discovery"


def test_action_and_link_serializers_emit_evidence(db):
    from app.models.ontology import OntologyLink
    from app.services.ontology.serialize import action_to_dict, link_to_dict

    a = OntologyAction(org_id=ORG, object_id="x", name="A",
                       risk_level="read", requires_confirmation=False)
    l = OntologyLink(org_id=ORG, from_object_id="x", to_object_id="y",
                     link_type="relates")
    db.add(a)
    db.add(l)
    db.commit()  # python/server defaults apply on flush
    d = action_to_dict(a)
    assert d["evidence_status"] == "proposed"
    d = link_to_dict(l)
    assert d["evidence_status"] == "proposed"
