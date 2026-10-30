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
    """A concurrent transaction claiming the next revision_no forces an
    IntegrityError inside the savepoint; the recorder rolls the savepoint
    back, re-reads max, and retries — the caller's mutation survives."""
    from app.models.ontology_revision import OntologyRevision
    from app.services.ontology.revisions import record_revision

    o = _obj(db)
    calls = {"n": 0}
    real_flush = db.flush

    def racing_flush(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            # The concurrent winner commits revision_no 1 FIRST.
            s2 = sessionmaker(bind=db.get_bind())()
            s2.add(OntologyRevision(
                org_id=ORG, entity_type="object", entity_id=o.id,
                revision_no=1, change_kind="create",
                before_json={}, after_json={},
            ))
            s2.commit()
            s2.close()
            # Force OUR insert (computed as max+1 = 1 before the race was
            # visible) to collide with s2's row → real IntegrityError.
            orig_add = db.add

            def poisoned_add(instance):
                if isinstance(instance, OntologyRevision) and \
                        instance.revision_no == 1:
                    instance.revision_no = 1  # guarantee the collision
                orig_add(instance)

            db.add = poisoned_add
        return real_flush(*a, **kw)

    monkeypatch.setattr(db, "flush", racing_flush)
    rev = record_revision(db, "object", o, change_kind="update")
    db.commit()
    assert rev is not None and rev.revision_no == 2  # retry re-read max=1
    rows = db.query(OntologyRevision).filter_by(entity_id=o.id) \
        .order_by(OntologyRevision.revision_no).all()
    numbers = [r.revision_no for r in rows]
    assert len(numbers) == len(set(numbers))        # no duplicates
    assert numbers == sorted(numbers)               # ordered, no gaps
    assert numbers == [1, 2]


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


def test_template_import_revisions_and_idempotency(db):
    """Import records create-revisions for EVERY imported element
    (domains AND objects AND links); re-import changes nothing → zero
    new revisions."""
    from app.models.ontology_revision import OntologyRevision
    from app.services.ontology.template_export import import_org_template

    bundle = {"domains": [{"code": "finance", "name": "Finance",
              "objects": [{"name": "Invoice",
                           "properties": [{"name": "total", "type": "number"}]}],
              "links": []}]}
    r1 = import_org_template(db, ORG, bundle)
    assert r1["objects_created"] == 1
    n1 = db.query(OntologyRevision).filter_by(org_id=ORG).count()
    kinds = {r.change_kind for r in
             db.query(OntologyRevision).filter_by(org_id=ORG).all()}
    assert "create" in kinds
    r2 = import_org_template(db, ORG, bundle)
    n2 = db.query(OntologyRevision).filter_by(org_id=ORG).count()
    assert r2["objects_created"] == 0 and r2["domains_created"] == 0
    assert n2 == n1, "idempotent re-import must produce ZERO revisions"


def test_rollback_name_conflict_is_honest_valueerror(db):
    """Restoring an old name that another element now owns → honest
    ValueError (endpoint maps to 422), not a raw 500."""
    from uuid import uuid4

    from app.services.ontology.revisions import (
        record_revision, rollback_to_revision,
    )

    o = _obj(db, name="Alpha")
    r1 = record_revision(db, "object", o)
    db.commit()
    old_name = o.name                       # what rollback will restore
    o.name = f"Beta{uuid4().hex[:6]}"
    record_revision(db, "object", o, change_kind="update")
    db.commit()

    other = OntologyObject(
        org_id=ORG, domain_id=o.domain_id, name=old_name,  # occupies it now
        backing_kind="manual")
    db.add(other)
    db.commit()

    with pytest.raises(ValueError):
        rollback_to_revision(db, "object", o, r1.revision_no)
