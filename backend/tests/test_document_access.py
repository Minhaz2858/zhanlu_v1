"""P4 T8 — fail-closed document access (D6) + API + per-entry runtime
filtering (D7).

The gate inverts the platform default: ``resolve_user_policies`` FAILS
OPEN (no rows / missing principal / resolution error → full access), so
it CANNOT decide document disclosure directly. Five states:

- EXPLICITLY_AUTHORIZED (owner / policy grant / org-unrestricted member
  — basis recorded) → ALLOW
- EXPLICITLY_DENIED · NO_APPLICABLE_POLICY · MISSING_PRINCIPAL (API-key
  principals get no document-derived content) · RESOLUTION_FAILURE → DENY

admin ≠ document-reader: require_org_admin grants ROUTE access only —
the D6 gate applies to admins like everyone else at this boundary.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.knowledge_base import DocumentVersion, KnowledgeBase
from app.models.ontology import DocumentProposal, OntologyDomain, OntologyObject
import app.models.ontology_revision  # noqa: F401 — register in metadata
from app.models.resource_access_policy import ResourceAccessPolicy
from app.models.user import User

ORG = "org-a"
OTHER = "org-b"


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


def _user(db, *, org=ORG, role="user", uid=None) -> User:
    u = User(org_id=org, role=role, email=f"{uid or role}@x.io",
             full_name="Test User", password_hash="x")
    if uid:
        u.id = uid
    db.add(u)
    db.commit()
    return u


def _kb(db, *, org=ORG, project_id=None, created_by=None) -> KnowledgeBase:
    kb = KnowledgeBase(org_id=org, name=f"kb-{project_id or 'global'}",
                       source_kind="file", file_type="txt",
                       file_url="/api/uploads/x.txt",
                       project_id=project_id, created_by_id=created_by)
    db.add(kb)
    db.commit()
    return kb


def _policy(db, *, user_id, kb, mode="allow", project_id=None):
    db.add(ResourceAccessPolicy(
        resource_share_id="share-test",
        resource_type="project",
        resource_id=project_id or kb.project_id,
        user_id=user_id, kb_id=kb.id, mode=mode,
    ))
    db.commit()


# ── the D6 gate itself ───────────────────────────────────────────────────

def test_owner_is_authorized_with_recorded_basis(db):
    from app.services.ontology.doc_access import document_access_state

    owner = _user(db, uid="u-owner")
    kb = _kb(db, created_by=owner.id)
    out = document_access_state(db, owner, kb)
    assert out["allowed"] is True
    assert out["state"] == "EXPLICITLY_AUTHORIZED"
    assert out["basis"] == "owner"


def test_org_unrestricted_member_is_authorized(db):
    from app.services.ontology.doc_access import document_access_state

    member = _user(db, uid="u-member")
    kb = _kb(db, project_id=None)
    out = document_access_state(db, member, kb)
    assert out["allowed"] is True
    assert out["state"] == "EXPLICITLY_AUTHORIZED"
    assert out["basis"] == "org-unrestricted member"


def test_project_scoped_kb_without_policy_is_denied(db):
    """NO_APPLICABLE_POLICY → DENY: the fail-open default-allow of
    resolve_user_policies is NEVER read as document access."""
    from app.services.ontology.doc_access import document_access_state

    outsider = _user(db, uid="u-outsider")
    kb = _kb(db, project_id="proj-1")
    out = document_access_state(db, outsider, kb)
    assert out["allowed"] is False
    assert out["state"] == "NO_APPLICABLE_POLICY"


def test_project_scoped_kb_with_policy_grant_is_authorized(db):
    from app.services.ontology.doc_access import document_access_state

    member = _user(db, uid="u-shared")
    kb = _kb(db, project_id="proj-1")
    _policy(db, user_id=member.id, kb=kb, mode="allow")
    out = document_access_state(db, member, kb)
    assert out["allowed"] is True
    assert out["state"] == "EXPLICITLY_AUTHORIZED"
    assert out["basis"] == "policy grant"


def test_explicit_policy_deny_wins(db):
    from app.services.ontology.doc_access import document_access_state

    member = _user(db, uid="u-denied")
    kb = _kb(db, project_id="proj-1")
    _policy(db, user_id=member.id, kb=kb, mode="deny")
    out = document_access_state(db, member, kb)
    assert out["allowed"] is False
    assert out["state"] == "EXPLICITLY_DENIED"


def test_cross_org_is_denied_even_for_owners(db):
    from app.services.ontology.doc_access import document_access_state

    owner = _user(db, org=ORG, uid="u-owner-b")
    kb = _kb(db, org=OTHER, created_by=owner.id)
    out = document_access_state(db, owner, kb)
    assert out["allowed"] is False
    assert out["state"] == "EXPLICITLY_DENIED"


def test_missing_principal_is_denied(db):
    from app.services.ontology.doc_access import document_access_state

    kb = _kb(db)
    out = document_access_state(db, None, kb)
    assert out["allowed"] is False
    assert out["state"] == "MISSING_PRINCIPAL"


def test_resolution_failure_is_denied_not_fatal(db, monkeypatch):
    from app.services.ontology import doc_access

    member = _user(db, uid="u-boom")
    kb = _kb(db, project_id="proj-9")

    def _boom(*a, **k):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(doc_access, "resolve_user_policies", _boom)
    out = doc_access.document_access_state(db, member, kb)
    assert out["allowed"] is False
    assert out["state"] == "RESOLUTION_FAILURE"


def test_assert_raises_permission_error_on_deny(db):
    from app.services.ontology.doc_access import assert_document_kb_access

    outsider = _user(db, uid="u-no")
    kb = _kb(db, project_id="proj-1")
    with pytest.raises(PermissionError):
        assert_document_kb_access(db, outsider, kb)
    owner = _user(db, uid="u-yes")
    kb2 = _kb(db, created_by=owner.id)
    assert assert_document_kb_access(db, owner, kb2)["allowed"] is True


def test_explicit_deny_wins_on_org_unrestricted_kb(db):
    """T8 review F3: an explicit user deny row targets the KB through ANY
    share — it must win even when the KB itself is org-unrestricted."""
    from app.services.ontology.doc_access import document_access_state

    member = _user(db, uid="u-denied-global")
    kb = _kb(db, project_id=None)
    _policy(db, user_id=member.id, kb=kb, mode="deny",
            project_id="proj-other")
    out = document_access_state(db, member, kb)
    assert out["allowed"] is False
    assert out["state"] == "EXPLICITLY_DENIED"


# ── per-entry runtime filtering (D7 / scenarios 4 & 5) ───────────────────

def _entry(kb, ver_id, quote):
    return {
        "source_kb_id": kb.id, "doc_version_id": ver_id,
        "content_hash": "c" * 64, "location": {"page": 1},
        "quote": quote, "reviewer": "rev-1", "decision": "approved",
        "audience_snapshot": {"org_id": kb.org_id},
    }


def _object_with_entries(db, entries) -> OntologyObject:
    d = OntologyDomain(org_id=ORG, code="misc", name="Misc")
    db.add(d)
    db.flush()
    obj = OntologyObject(org_id=ORG, domain_id=d.id, name="X",
                         backing_kind="manual", evidence_status="verified",
                         provenance_json={"method": "discovery",
                                          "document_provenance": entries})
    db.add(obj)
    db.commit()
    return obj


def test_scenario_4_restricted_document_hidden_from_discovery(db):
    """A restricted document cannot leak through ontology serialization:
    default (no auth context) strips ALL document entries; a principal
    without access sees none; an authorized principal sees the entry."""
    from app.services.ontology.serialize import object_to_dict

    kb = _kb(db, project_id="proj-sec")
    ver = DocumentVersion(
        org_id=ORG, kb_id=kb.id, content_hash="d" * 64,
        first_indexed_at=datetime.now(timezone.utc))
    db.add(ver)
    db.commit()
    obj = _object_with_entries(db, [_entry(kb, ver.id, "Restricted span.")])

    # 1. No auth context (external API / unknown caller) → stripped.
    out = object_to_dict(obj)
    assert out["provenance"].get("document_provenance") in (None, [])
    assert out["provenance"]["method"] == "discovery"  # other keys survive

    # 2. Principal WITHOUT source access → hidden.
    outsider = _user(db, uid="u-out4")
    out2 = object_to_dict(obj, doc_ctx={"db": db, "user": outsider})
    assert out2["provenance"].get("document_provenance") in (None, [])

    # 3. Owner → visible.
    owner = _user(db, uid="u-owner4")
    kb.created_by_id = owner.id
    db.commit()
    out3 = object_to_dict(obj, doc_ctx={"db": db, "user": owner})
    kept = out3["provenance"]["document_provenance"]
    assert len(kept) == 1 and kept[0]["quote"] == "Restricted span."


def test_scenario_5_two_audiences_independently_protected(db):
    from app.services.ontology.serialize import object_to_dict

    kb_pub = _kb(db, project_id=None)         # org-unrestricted
    kb_fin = _kb(db, project_id="proj-fin")   # restricted (no policy rows)
    entries = [
        _entry(kb_pub, "v-pub", "Public handbook span."),
        _entry(kb_fin, "v-fin", "Finance-only span."),
    ]
    obj = _object_with_entries(db, entries)

    plain_member = _user(db, uid="u-plain")
    out = object_to_dict(obj, doc_ctx={"db": db, "user": plain_member})
    kept = out["provenance"]["document_provenance"]
    assert [e["quote"] for e in kept] == ["Public handbook span."]

    finance_member = _user(db, uid="u-fin")
    _policy(db, user_id=finance_member.id, kb=kb_fin, mode="allow")
    out2 = object_to_dict(obj, doc_ctx={"db": db, "user": finance_member})
    kept2 = out2["provenance"]["document_provenance"]
    assert sorted(e["quote"] for e in kept2) == sorted(
        ["Public handbook span.", "Finance-only span."])


def test_filter_drops_entries_on_gate_exception(db, monkeypatch):
    """Fail-closed: any gate failure hides the entry, never shows it."""
    from app.services.ontology import doc_access
    from app.services.ontology.serialize import object_to_dict

    kb = _kb(db, project_id="proj-x")
    obj = _object_with_entries(db, [_entry(kb, "v-x", "Secret.")])
    member = _user(db, uid="u-x")

    monkeypatch.setattr(doc_access, "document_access_state",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("gate down")))
    out = object_to_dict(obj, doc_ctx={"db": db, "user": member})
    assert out["provenance"].get("document_provenance") in (None, [])


# ── router endpoints ─────────────────────────────────────────────────────

@pytest.fixture()
def client(db):
    from app.deps import get_db, require_org_admin
    from app.routers.ontology import router

    admin = _user(db, role="admin", uid="u-admin")
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_org_admin] = lambda: admin
    return TestClient(app), admin


def _proposal(db, kb) -> DocumentProposal:
    ver = DocumentVersion(
        org_id=kb.org_id, kb_id=kb.id, content_hash="e" * 64,
        first_indexed_at=datetime.now(timezone.utc))
    db.add(ver)
    db.commit()
    p = DocumentProposal(
        org_id=kb.org_id, kb_id=kb.id, doc_version_id=ver.id,
        source_kb_id=kb.id, chunk_key=f"{kb.id}_{ver.id[:8]}_0",
        proposal_kind="definition",
        payload_json={"name": "Customer", "description": "d"},
        quote="Customer means any entity that buys goods.",
        location_json={"page": 1}, method="llm_extraction",
        model_version="deepseek-chat",
        audience_json={"org_id": kb.org_id, "project_ids": []},
        status="proposed",
    )
    db.add(p)
    db.commit()
    return p


def test_extract_requires_flag(db, client):
    c, admin = client
    kb = _kb(db, created_by=admin.id)
    r = c.post("/api/ontology/document-evidence/extract",
               json={"kb_id": kb.id})
    assert r.status_code == 400  # flag disabled


def test_admin_without_document_access_cannot_approve_or_see(db, client):
    """D6: admin ≠ document-reader — approval requires BOTH permissions;
    a restricted proposal's entries/history are hidden from the admin."""
    import app.config as config

    c, admin = client
    monkey = pytest.MonkeyPatch()
    monkey.setattr(config.settings, "ONTOLOGY_DOC_GROUNDING_ENABLED", True,
                   raising=False)
    try:
        kb = _kb(db, project_id="proj-no")  # project-scoped, no policy rows
        p = _proposal(db, kb)

        # approve → 403 (gate, not the service)
        r = c.post(f"/api/ontology/document-evidence/{p.id}/approve",
                   json={"domain_id": None})
        assert r.status_code == 403
        db.expire_all()
        assert p.status == "proposed"

        # history → 403 (cannot see the restricted entries either)
        r2 = c.get(f"/api/ontology/document-evidence/{p.id}/history")
        assert r2.status_code == 403
    finally:
        monkey.undo()


def test_admin_with_document_access_can_approve(db, client):
    import app.config as config

    c, admin = client
    monkey = pytest.MonkeyPatch()
    monkey.setattr(config.settings, "ONTOLOGY_DOC_GROUNDING_ENABLED", True,
                   raising=False)
    try:
        d = OntologyDomain(org_id=ORG, code="cust", name="Customer")
        db.add(d)
        db.commit()
        kb = _kb(db, created_by=admin.id)  # org-unrestricted + owner
        p = _proposal(db, kb)

        r = c.post(f"/api/ontology/document-evidence/{p.id}/approve",
                   json={"domain_id": d.id})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "approved"
        assert body["object_id"]
    finally:
        monkey.undo()


def test_list_hides_restricted_proposals(db, client):
    import app.config as config

    c, admin = client
    monkey = pytest.MonkeyPatch()
    monkey.setattr(config.settings, "ONTOLOGY_DOC_GROUNDING_ENABLED", True,
                   raising=False)
    try:
        kb_ok = _kb(db, created_by=admin.id)
        kb_no = _kb(db, project_id="proj-hide")
        p_ok = _proposal(db, kb_ok)
        p_no = _proposal(db, kb_no)

        r = c.get("/api/ontology/document-evidence")
        assert r.status_code == 200
        body = r.json()
        ids = [x["id"] for x in body["proposals"]]
        assert p_ok.id in ids and p_no.id not in ids
        assert body["hidden"] == 1
    finally:
        monkey.undo()


def test_reject_and_revise_gated_on_restricted_source(db, client):
    import app.config as config

    c, admin = client
    monkey = pytest.MonkeyPatch()
    monkey.setattr(config.settings, "ONTOLOGY_DOC_GROUNDING_ENABLED", True,
                   raising=False)
    try:
        kb = _kb(db, project_id="proj-no2")
        p = _proposal(db, kb)
        r1 = c.post(f"/api/ontology/document-evidence/{p.id}/reject")
        r2 = c.post(f"/api/ontology/document-evidence/{p.id}/revise",
                    json={"payload": {"name": "X"}})
        assert r1.status_code == 403 and r2.status_code == 403
        db.expire_all()
        assert p.status == "proposed"
    finally:
        monkey.undo()


def test_stamp_has_no_object_level_reviewer_keys(db):
    """T8 review F4: reviewer identity lives ONLY inside the gated entries —
    hiding an entry hides ALL traces of that document's approval."""
    from app.services.ontology.evidence import stamp_human_document_approval

    d = OntologyDomain(org_id=ORG, code="f4", name="F4")
    db.add(d)
    db.flush()
    obj = OntologyObject(org_id=ORG, domain_id=d.id, name="F4obj",
                         backing_kind="manual", evidence_status="proposed")
    kb = _kb(db)
    p = _proposal(db, kb)
    db.add(obj)
    db.commit()

    stamp_human_document_approval(obj, reviewer_id="rev-f4", proposal=p)
    assert "document_reviewer" not in obj.provenance_json
    assert "document_approved_at" not in obj.provenance_json
    assert obj.provenance_json["document_provenance"][0]["reviewer"] == "rev-f4"


def test_revision_feed_scrubs_document_provenance(db, client):
    """T8 review F1 (BLOCKER): the revision history must not become a
    provenance side-channel — snapshots are per-entry gated exactly like
    the live object serialization."""
    from app.deps import get_current_user_required, get_db
    from app.services.ontology.evidence import stamp_human_document_approval
    from app.services.ontology.revisions import record_revision

    c, admin = client
    d = OntologyDomain(org_id=ORG, code="rev", name="Rev")
    db.add(d)
    db.commit()
    kb = _kb(db, project_id="proj-rev")  # restricted source
    p = _proposal(db, kb)
    obj = OntologyObject(org_id=ORG, domain_id=d.id, name="RevObj",
                         backing_kind="manual", evidence_status="proposed")
    db.add(obj)
    db.commit()

    stamp_human_document_approval(obj, reviewer_id="rev-x", proposal=p,
                                  content_hash="e" * 64)
    record_revision(db, "object", obj, change_kind="update",
                    changed_by="rev-x")
    db.commit()

    # Owner of the source document → snapshot provenance visible.
    owner = _user(db, uid="u-owner-rev")
    kb.created_by_id = owner.id
    db.commit()
    app2 = FastAPI()
    from app.routers.ontology import router as ont_router

    app2.include_router(ont_router, prefix="/api")
    app2.dependency_overrides[get_db] = lambda: db
    app2.dependency_overrides[get_current_user_required] = lambda: owner
    c2 = TestClient(app2)
    r = c2.get(f"/api/ontology/object/{obj.id}/revisions")
    assert r.status_code == 200
    snap = r.json()["revisions"][0]["after"]
    kept = (snap["provenance_json"] or {}).get("document_provenance")
    assert kept and kept[0]["quote"] == p.quote

    # A user WITHOUT source access → entries scrubbed out of the snapshot.
    outsider = _user(db, uid="u-out-rev")
    app2.dependency_overrides[get_current_user_required] = lambda: outsider
    r2 = c2.get(f"/api/ontology/object/{obj.id}/revisions")
    snap2 = r2.json()["revisions"][0]["after"]
    assert (snap2["provenance_json"] or {}).get("document_provenance") in (
        None, [])
