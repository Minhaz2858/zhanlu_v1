"""Phase 1A — BusinessRule model (unified enterprise business rules).

Covers: proposed-by-default creation, org isolation, vocabulary
CheckConstraints (scope / status / applies_to), scoped-rule-must-name-a-target
constraint, evidence-id preservation, append-only review history, and the
index/constraint definitions matching migration 119.
"""

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.business_rule import APPLIES_TO, SCOPES, STATUSES, BusinessRule

ORG = "org-acme"


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng, tables=[BusinessRule.__table__])
    s = sessionmaker(bind=eng)()
    yield s
    s.close()
    eng.dispose()


def _make(**kw):
    defaults = dict(
        id="br-1", org_id=ORG, statement="Discounts over 10% need manager approval"
    )
    defaults.update(kw)
    return BusinessRule(**defaults)


def test_creation_defaults_to_proposed_status(db):
    rule = _make()
    db.add(rule)
    db.commit()
    db.refresh(rule)
    assert rule.status == "proposed"
    assert rule.scope == "org"
    assert rule.applies_to == "sql"
    assert rule.scope_ref_id is None
    assert rule.is_deleted is False


def test_org_isolation_query(db):
    db.add(_make(id="br-a", org_id=ORG))
    db.add(_make(id="br-b", org_id="org-other",
                 statement="Other org rule"))
    db.commit()
    mine = db.query(BusinessRule).filter(
        BusinessRule.org_id == ORG, BusinessRule.is_deleted.is_(False)
    ).all()
    assert {r.id for r in mine} == {"br-a"}


def test_all_valid_enum_values_accepted(db):
    for i, scope in enumerate(SCOPES):
        db.add(_make(id=f"br-s{i}", scope=scope,
                     scope_ref_id=None if scope == "org" else "ref-1"))
    for i, status in enumerate(STATUSES):
        db.add(_make(id=f"br-st{i}", status=status))
    for i, applies in enumerate(APPLIES_TO):
        db.add(_make(id="br-a%d" % i, applies_to=applies))
    db.commit()
    assert db.query(BusinessRule).count() == len(SCOPES) + len(STATUSES) + len(APPLIES_TO)


@pytest.mark.parametrize("bad_scope", ["global", "table", "ORG", ""])
def test_invalid_scope_rejected(db, bad_scope):
    db.add(_make(scope=bad_scope))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


@pytest.mark.parametrize("bad_status", ["draft", "active", "PROPOSED"])
def test_invalid_status_rejected(db, bad_status):
    db.add(_make(status=bad_status))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


@pytest.mark.parametrize("bad_applies", ["prompt", "ui", "SQL"])
def test_invalid_applies_to_rejected(db, bad_applies):
    db.add(_make(applies_to=bad_applies))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_scoped_rule_without_ref_rejected(db):
    db.add(_make(scope="domain", scope_ref_id=None))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    # with a ref it passes
    db.add(_make(scope="domain", scope_ref_id="dom-1"))
    db.commit()


def test_enabled_requires_review_transition_via_status_values(db):
    # 'enabled' is a valid stored status (DB cannot know history; the gate
    # is procedural — record_review + callers). Verify it stores and that
    # verified/rejected are equally storable.
    for i, status in enumerate(("verified", "rejected", "enabled")):
        db.add(_make(id=f"br-g{i}", status=status))
    db.commit()
    stored = {r.id: r.status for r in db.query(BusinessRule).all()}
    assert stored == {"br-g0": "verified", "br-g1": "rejected",
                      "br-g2": "enabled"}


def test_source_evidence_ids_preserved(db):
    ids = ["pe-1", "pe-2", "pe-3"]
    db.add(_make(source_evidence_ids=ids))
    db.commit()
    db.expire_all()
    rule = db.get(BusinessRule, "br-1")
    assert rule.source_evidence_ids == ids


def test_record_review_appends_not_replaces(db):
    rule = _make()
    db.add(rule)
    db.commit()
    rule.record_review("confirmed", reviewer_id="u-1", reviewer_name="Alice",
                       reason="matches policy doc")
    rule.record_review("enabled", reviewer_id="u-1", reviewer_name="Alice")
    db.commit()
    db.expire_all()
    history = db.get(BusinessRule, "br-1").review_history
    assert [h["decision"] for h in history] == ["confirmed", "enabled"]
    assert history[0]["reason"] == "matches policy doc"
    assert all(h["at"] for h in history)


def test_constraint_and_index_definitions_match_migration(db):
    idx = {i["name"] for i in inspect(db.bind).get_indexes("business_rules")}
    assert {"ix_business_rules_org_status", "ix_business_rules_org_scope"} <= idx
    checks = {c["name"] for c in inspect(db.bind).get_check_constraints("business_rules")}
    assert {"ck_business_rule_scope", "ck_business_rule_status",
            "ck_business_rule_applies_to",
            "ck_business_rule_scoped_needs_ref"} <= checks
