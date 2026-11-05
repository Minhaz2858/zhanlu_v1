"""KB↔project multi-project sharing (2026-10-31).

One connector (KnowledgeBase row) must be usable by MULTIPLE projects at the
same time. Previously the single ``knowledge_bases.project_id`` FK enforced
1-to-1: attaching a KB to a second project overwrote the first binding.

Model: home + extra links. ``KnowledgeBase.project_id`` stays the home
project; ``project_knowledge_bases`` carries additional memberships. Every
read path (project listing, knowledge map, chat-time NL2SQL runtime, schema
linker, agent tools, access policies) resolves membership as
**home ∪ legacy-name ∪ explicit links** via the shared helper.

Runs under the T1 contract: module-fresh truncated DB, sequential sessions.
"""

from __future__ import annotations

import os
import sys
import uuid
from types import SimpleNamespace

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

import pytest  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models.knowledge_base import KnowledgeBase  # noqa: E402
from app.models.project import Project  # noqa: E402
from app.models.project_knowledge_base import ProjectKnowledgeBase  # noqa: E402
from app.services.data_source_runtime.data_source_runtime import (  # noqa: E402
    _extend_with_project_kbs,
)
from app.services.project_kb_membership import (  # noqa: E402
    attach_kb_to_project,
    detach_kb_from_project,
    project_kb_membership_filter,
)

_ORG = f"org-kbshare-{uuid.uuid4().hex[:8]}"
_APP = "app-kbshare"


def _mk_project(db, name: str) -> Project:
    p = Project(
        id=f"proj-{uuid.uuid4().hex[:12]}",
        name=name,
        org_id=_ORG,
        app_id=_APP,
        created_by_id="user-1",
        status="active",
    )
    db.add(p)
    db.flush()
    return p


def _mk_kb(db, name: str, project: Project | None) -> KnowledgeBase:
    kb = KnowledgeBase(
        id=f"kb-{uuid.uuid4().hex[:12]}",
        name=name,
        org_id=_ORG,
        app_id=_APP,
        project=project.name if project else "global",
        project_id=project.id if project else None,
        created_by_id="user-1",
        source_kind="database",
        db_type="mysql",
        status="active",
    )
    db.add(kb)
    db.flush()
    return kb


def _bound_kb_ids(db, project: Project) -> set[str]:
    rows = (
        db.query(KnowledgeBase)
        .filter(
            project_kb_membership_filter(project),
            KnowledgeBase.is_deleted == False,  # noqa: E712
        )
        .all()
    )
    return {kb.id for kb in rows}


def _commit(fn):
    """Run fn(db) with a fresh session, commit, return its result."""
    db = SessionLocal()
    try:
        result = fn(db)
        db.commit()
        return result
    finally:
        db.close()


# ── attach ────────────────────────────────────────────────────────────────

def test_attach_is_additive_never_steals():
    """Attaching a home-bound KB to a second project must NOT move it."""
    def scenario(db):
        p1 = _mk_project(db, "Sales")
        p2 = _mk_project(db, "Finance")
        kb = _mk_kb(db, "warehouse_prod", p1)
        attach_kb_to_project(db, kb, p2, created_by_id="user-1")
        return p1.id, p2.id, kb.id

    p1_id, p2_id, kb_id = _commit(scenario)

    def check(db):
        kb = db.get(KnowledgeBase, kb_id)
        # original binding intact
        assert kb.project_id == p1_id
        assert _bound_kb_ids(db, db.get(Project, p1_id)) == {kb_id}
        assert _bound_kb_ids(db, db.get(Project, p2_id)) == {kb_id}

    _commit(check)


def test_attach_is_idempotent():
    def scenario(db):
        p = _mk_project(db, "Ops")
        kb = _mk_kb(db, "orders_json", p)
        attach_kb_to_project(db, kb, p)
        attach_kb_to_project(db, kb, p)
        count = (
            db.query(ProjectKnowledgeBase)
            .filter(
                ProjectKnowledgeBase.project_id == p.id,
                ProjectKnowledgeBase.knowledge_base_id == kb.id,
            )
            .count()
        )
        return count

    assert _commit(scenario) == 1


def test_attach_homeless_kb_adopts_project_as_home():
    def scenario(db):
        p = _mk_project(db, "Starter")
        kb = _mk_kb(db, "orphan_db", None)
        assert kb.project_id is None
        link = attach_kb_to_project(db, kb, p)
        # capture plain values while attached — detached ORM refresh fails
        return kb.id, link.project_id, link.role

    kb_id, link_project_id, link_role = _commit(scenario)

    def check(kb_id, link_project_id, link_role):
        db = SessionLocal()
        try:
            fresh_kb = db.get(KnowledgeBase, kb_id)
            assert fresh_kb.project_id == link_project_id
            assert link_role == "home"
        finally:
            db.close()

    _commit(lambda db: check(kb_id, link_project_id, link_role))


# ── membership resolution ─────────────────────────────────────────────────

def test_membership_home_union_legacy_union_links():
    """All three membership sources resolve: home FK, legacy name, link."""
    def scenario(db):
        p = _mk_project(db, "Union")
        other = _mk_project(db, "Elsewhere")
        home_kb = _mk_kb(db, "home_db", p)          # home FK
        legacy_kb = KnowledgeBase(                   # legacy name only
            id=f"kb-{uuid.uuid4().hex[:12]}",
            name="legacy_db",
            org_id=_ORG,
            app_id=_APP,
            project=p.name,                          # name matches, project_id NULL
            project_id=None,
            created_by_id="user-1",
            source_kind="database",
        )
        db.add(legacy_kb)
        db.flush()
        linked_kb = _mk_kb(db, "linked_db", other)   # bound elsewhere…
        attach_kb_to_project(db, linked_kb, p)       # …and linked here
        noise_kb = _mk_kb(db, "noise_db", other)     # not a member
        return p.id, home_kb.id, legacy_kb.id, linked_kb.id, noise_kb.id

    p_id, home_id, legacy_id, linked_id, noise_id = _commit(scenario)

    def check(p_id, home_id, legacy_id, linked_id, noise_id):
        db = SessionLocal()
        try:
            bound = _bound_kb_ids(db, db.get(Project, p_id))
            assert bound == {home_id, legacy_id, linked_id}
            assert noise_id not in bound
        finally:
            db.close()

    _commit(lambda db: check(p_id, home_id, legacy_id, linked_id, noise_id))


def test_chat_runtime_resolves_shared_kb_for_both_projects():
    """The chat-time NL2SQL runtime union (_extend_with_project_kbs) sees a
    linked KB from BOTH projects — this is what makes shared data answerable
    in conversations of each project."""
    def scenario(db):
        p1 = _mk_project(db, "RuntimeA")
        p2 = _mk_project(db, "RuntimeB")
        kb = _mk_kb(db, "shared_warehouse", p1)
        attach_kb_to_project(db, kb, p2)
        return kb.id, p1.id, p1.name, p2.id, p2.name

    kb_id, p1_id, p1_name, p2_id, p2_name = _commit(scenario)

    # resolve in a FRESH session: _extend_with_project_kbs owns its read
    # transaction and rolls it back, which would discard uncommitted data.
    db = SessionLocal()
    try:
        agent = SimpleNamespace(id="agent-1", org_id=_ORG, app_id=_APP)
        ids1 = _extend_with_project_kbs(db, agent, [], p1_id, project_name=p1_name)
        ids2 = _extend_with_project_kbs(db, agent, [], p2_id, project_name=p2_name)
    finally:
        db.close()
    assert kb_id in ids1
    assert kb_id in ids2


# ── detach ────────────────────────────────────────────────────────────────

def test_detach_member_leaves_home_intact():
    def scenario(db):
        home = _mk_project(db, "DetHome")
        member = _mk_project(db, "DetMember")
        kb = _mk_kb(db, "keep_home_db", home)
        attach_kb_to_project(db, kb, member)
        detach_kb_from_project(db, kb, member)
        return home.id, member.id, kb.id

    home_id, member_id, kb_id = _commit(scenario)

    def check(home_id, member_id, kb_id):
        db = SessionLocal()
        try:
            kb = db.get(KnowledgeBase, kb_id)
            assert kb.project_id == home_id                      # home kept
            assert _bound_kb_ids(db, db.get(Project, home_id)) == {kb_id}
            assert _bound_kb_ids(db, db.get(Project, member_id)) == set()
        finally:
            db.close()

    _commit(lambda db: check(home_id, member_id, kb_id))


def test_detach_home_reassigns_to_first_remaining_link():
    def scenario(db):
        p1 = _mk_project(db, "HomeFirst")
        p2 = _mk_project(db, "LinkSecond")
        kb = _mk_kb(db, "rehome_db", p1)
        attach_kb_to_project(db, kb, p2)
        detach_kb_from_project(db, kb, p1)
        return p2.id, kb.id

    p2_id, kb_id = _commit(scenario)

    def check(p2_id, kb_id):
        db = SessionLocal()
        try:
            kb = db.get(KnowledgeBase, kb_id)
            assert kb.project_id == p2_id   # home pointer reassigned
            assert _bound_kb_ids(db, db.get(Project, p2_id)) == {kb_id}
        finally:
            db.close()

    _commit(lambda db: check(p2_id, kb_id))


def test_detach_last_project_leaves_kb_projectless_but_present():
    def scenario(db):
        p = _mk_project(db, "Solo")
        kb = _mk_kb(db, "last_db", p)
        detach_kb_from_project(db, kb, p)
        return kb.id

    kb_id = _commit(scenario)

    def check(kb_id):
        db = SessionLocal()
        try:
            kb = db.get(KnowledgeBase, kb_id)
            assert kb.project_id is None
            assert kb.is_deleted is False
            # still resolvable and attachable again
            p2 = _mk_project(db, "Reborn")
            attach_kb_to_project(db, kb, p2)
            db.flush()
            assert kb.project_id == p2.id
        finally:
            db.close()

    _commit(lambda db: check(kb_id))


# ── tenant isolation ──────────────────────────────────────────────────────

def test_org_scoped_membership_filter_excludes_foreign_org_rows():
    """A link row from another org must never leak into this org's project
    resolution (consistent with the FSM tenant-isolation contract)."""
    def scenario(db):
        p = _mk_project(db, "Scoped")
        kb = _mk_kb(db, "scoped_db", p)
        foreign_kb = KnowledgeBase(
            id=f"kb-{uuid.uuid4().hex[:12]}",
            name="foreign_db",
            org_id=f"org-other-{uuid.uuid4().hex[:8]}",
            app_id=_APP,
            project="global",
            project_id=None,
            created_by_id="user-2",
            source_kind="database",
        )
        db.add(foreign_kb)
        db.flush()
        db.add(ProjectKnowledgeBase(
            project_id=p.id,
            knowledge_base_id=foreign_kb.id,
            role="member",
            org_id=foreign_kb.org_id,
            app_id=_APP,
        ))
        db.flush()
        return p.id, kb.id, foreign_kb.id

    p_id, kb_id, foreign_id = _commit(scenario)

    def check(p_id, kb_id, foreign_id):
        db = SessionLocal()
        try:
            bound = _bound_kb_ids(db, db.get(Project, p_id))
            assert kb_id in bound
            assert foreign_id not in bound
        finally:
            db.close()

    _commit(lambda db: check(p_id, kb_id, foreign_id))
