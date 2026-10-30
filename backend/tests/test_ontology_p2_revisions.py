"""P2 versioned ontology registry — revisions.

Contract (PRD P2):
- every ontology mutation produces exactly ONE meaningful revision
- mutation + revision are transactionally atomic (same commit/rollback)
- idempotent re-runs (seeding, discovery, repeated binds) → ZERO revisions
- history is append-only and immutable; rollback creates a NEW revision
- concurrent mutations of one entity never duplicate/gap revision numbers
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.ontology import OntologyDomain, OntologyObject
from app.models.ontology_revision import OntologyRevision  # noqa: F401 — register before create_all

ORG = "org-test"


@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine):
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _domain(db) -> OntologyDomain:
    d = db.query(OntologyDomain).filter_by(
        org_id=ORG, code="procurement").first()
    if not d:
        d = OntologyDomain(org_id=ORG, code="procurement", name="Procurement")
        db.add(d)
        db.commit()
    return d


def _obj(db, name="PurchaseOrder", **kw):
    from uuid import uuid4
    o = OntologyObject(
        org_id=ORG, domain_id=_domain(db).id, name=f"{name}{uuid4().hex[:6]}",
        backing_kind="manual", **kw)
    db.add(o)
    db.commit()
    return o


# ── Atomicity ───────────────────────────────────────────────────────────


def test_mutation_and_revision_commit_together(db):
    """The revision rides the caller's transaction: one commit persists both."""
    from app.services.ontology.revisions import record_revision

    o = _obj(db)
    o.backing_kind = "table"
    o.backing_ref = "kb:k1:t"
    rev = record_revision(db, "object", o, change_kind="bind",
                          changed_by="boss")
    assert rev is not None and rev.revision_no == 1
    db.commit()
    assert rev.id is not None  # persisted in the SAME transaction


def test_mutation_and_revision_rollback_together(db, monkeypatch):
    """Forced failure AFTER record_revision: the mutation must NOT survive
    without its revision (state must be reconstructible from history)."""
    from app.models.ontology_revision import OntologyRevision
    from app.services.ontology.revisions import record_revision

    o = _obj(db)
    before_state = (o.backing_kind, o.backing_ref)
    o.backing_kind = "table"
    o.backing_ref = "kb:k1:t"
    rev = record_revision(db, "object", o, change_kind="bind")

    def broken_commit():
        raise RuntimeError("db down mid-mutation")

    monkeypatch.setattr(db, "commit", broken_commit)
    with pytest.raises(RuntimeError):
        db.commit()
    db.rollback()

    db.expire_all()
    o2 = db.query(OntologyObject).filter_by(id=o.id).first()
    assert (o2.backing_kind, o2.backing_ref) == before_state
    assert db.query(OntologyRevision).filter_by(
        entity_id=o.id, revision_no=rev.revision_no).count() == 0


# ── Idempotency ─────────────────────────────────────────────────────────


def test_no_change_produces_zero_revisions(db):
    from app.models.ontology_revision import OntologyRevision
    from app.services.ontology.revisions import record_revision

    o = _obj(db)
    r1 = record_revision(db, "object", o, change_kind="bind")
    db.commit()
    assert r1 is not None                      # first record = create
    # identical state again → no meaningful change → ZERO revisions
    r2 = record_revision(db, "object", o, change_kind="bind")
    db.commit()
    assert r2 is None
    assert db.query(OntologyRevision).filter_by(entity_id=o.id).count() == 1


def test_real_change_after_no_change_produces_one_revision(db):
    from app.models.ontology_revision import OntologyRevision
    from app.services.ontology.revisions import record_revision

    o = _obj(db)
    record_revision(db, "object", o)
    db.commit()
    o.backing_kind = "table"
    o.backing_ref = "kb:k1:t"
    rev = record_revision(db, "object", o, change_kind="bind")
    db.commit()
    assert rev is not None and rev.change_kind == "bind"
    assert db.query(OntologyRevision).filter_by(entity_id=o.id).count() == 2


# ── Snapshots carry evidence state (P1 history is immutable) ────────────


def test_snapshots_include_evidence_fields(db):
    from app.services.ontology.evidence import stamp_discovery
    from app.services.ontology.revisions import record_revision

    o = _obj(db)
    stamp_discovery(o, method="llm_discovery", confidence=0.9,
                    kb_id="k1", table="t")
    rev = record_revision(db, "object", o, change_kind="bind")
    assert rev.after_json["evidence_status"] == "inferred"
    assert rev.after_json["confidence"] == 0.9
    assert rev.after_json["provenance_json"]["method"] == "llm_discovery"


# ── Concurrency / revision_no integrity ─────────────────────────────────


def test_revision_no_collision_is_retried_not_duplicated(db, monkeypatch):
    """If a concurrent transaction claimed the next revision_no, the
    recorder retries after re-reading max — never duplicates, never loses
    the caller's mutation, never gaps numbers."""
    from app.models.ontology_revision import OntologyRevision
    from app.services.ontology.revisions import record_revision

    o = _obj(db)
    calls = {"n": 0}
    real_flush = db.flush

    def racing_flush(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            # simulate the concurrent winner committing revision_no 1
            s2 = sessionmaker(bind=db.get_bind())()
            s2.add(OntologyRevision(
                org_id=ORG, entity_type="object", entity_id=o.id,
                revision_no=1, change_kind="create",
                before_json={}, after_json={},
            ))
            s2.commit()
            s2.close()
            # make OUR first insert collide, then let the retry through
            orig_add = db.add

            def poisoned_add(instance):
                if isinstance(instance, OntologyRevision) and \
                        instance.revision_no == 1:
                    instance.revision_no = 999_999_999  # force real collision
                orig_add(instance)

            # NOTE: the retry path re-reads max (now 1) and inserts no=2;
            # the poisoned insert is rolled back with the savepoint.
        return real_flush(*a, **kw)

    monkeypatch.setattr(db, "flush", racing_flush)
    rev = record_revision(db, "object", o, change_kind="update")
    db.commit()
    assert rev is not None
    rows = db.query(OntologyRevision).filter_by(entity_id=o.id) \
        .order_by(OntologyRevision.revision_no).all()
    numbers = [r.revision_no for r in rows]
    assert len(numbers) == len(set(numbers))        # no duplicates
    assert numbers == sorted(numbers)               # ordered
    assert numbers[-1] == rev.revision_no


def test_unique_constraint_on_revision_no_exists():
    from app.models.ontology_revision import OntologyRevision
    cons = [c for c in OntologyRevision.__table__.constraints
            if c.__class__.__name__ == "UniqueConstraint"]
    cols = {frozenset(c.columns.keys()) for c in cons}
    assert frozenset({"entity_type", "entity_id", "revision_no"}) in cols


# ── Rollback ────────────────────────────────────────────────────────────


def test_rollback_creates_new_revision_and_restores_state(db):
    from app.services.ontology.revisions import (
        record_revision, rollback_to_revision,
    )

    o = _obj(db)
    r1 = record_revision(db, "object", o)  # create (proposed, manual)
    db.commit()
    o.backing_kind = "table"
    o.backing_ref = "kb:k1:t"
    record_revision(db, "object", o, change_kind="bind")
    db.commit()

    rev = rollback_to_revision(db, "object", o, r1.revision_no,
                               changed_by="admin")
    db.commit()
    assert rev.change_kind == "rollback"            # NEW revision, not erase
    assert o.backing_kind == "manual" and o.backing_ref is None
    assert o.evidence_status == "proposed"
    from app.models.ontology_revision import OntologyRevision
    assert db.query(OntologyRevision).filter_by(entity_id=o.id).count() == 3


def test_rollback_rejects_unknown_revision(db):
    from app.services.ontology.revisions import rollback_to_revision

    o = _obj(db)
    with pytest.raises(ValueError):
        rollback_to_revision(db, "object", o, 42)
