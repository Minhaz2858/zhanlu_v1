"""Shared KB↔project membership helpers (multi-project sharing).

2026-10-31: previously a connector belonged to exactly one project via the
single ``KnowledgeBase.project_id`` FK, and ~7 call sites each hand-rolled the
"dual-column" filter (``project_id == X OR project == legacy_name``). With the
``project_knowledge_bases`` link table (mirrors ``ProjectAgent``), membership
is: **home project OR legacy name parity OR an explicit link row**.

All read paths MUST use ``project_kb_membership_filter`` so a shared KB is
visible everywhere consistently (project listing, knowledge map, chat-time
NL2SQL runtime, schema linking, agent tools, access policies).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.knowledge_base import KnowledgeBase
from app.models.project import Project
from app.models.project_knowledge_base import ProjectKnowledgeBase

if TYPE_CHECKING:  # pragma: no cover
    pass

logger = logging.getLogger(__name__)


def project_kb_membership_filter(project: Project):
    """SQLA filter: "does KB belong to this project?" — home ∪ legacy ∪ linked.

    Keeps the legacy dual-column parity (``project_id`` / ``project`` name)
    that every existing site honored, and extends it with link rows.
    """
    link_kb_ids = (
        select(ProjectKnowledgeBase.knowledge_base_id)
        .where(
            ProjectKnowledgeBase.project_id == project.id,
            # Org/app-scoped like every other tenant filter: a link row from
            # another org must never widen this project's membership.
            ProjectKnowledgeBase.org_id == project.org_id,
            ProjectKnowledgeBase.app_id == project.app_id,
        )
    )
    return or_(
        KnowledgeBase.project_id == project.id,
        KnowledgeBase.project == project.name,
        KnowledgeBase.id.in_(link_kb_ids),
    )


def project_kb_link_subquery(project_id: str, org_id: str | None = None,
                             app_id: str | None = None):
    """Subquery of KB ids linked (non-home membership) to a project id.

    For call sites that hold only a project id (chat runtime, schema
    linker) and cannot load a Project row. Pass ``org_id``/``app_id`` when
    available so a foreign-org link row can never widen membership.
    """
    clauses = [ProjectKnowledgeBase.project_id == project_id]
    if org_id:
        clauses.append(ProjectKnowledgeBase.org_id == org_id)
    if app_id:
        clauses.append(ProjectKnowledgeBase.app_id == app_id)
    return (
        select(ProjectKnowledgeBase.knowledge_base_id)
        .where(*clauses)
    )


def attach_kb_to_project(
    db: Session,
    kb: KnowledgeBase,
    project: Project,
    created_by_id: str | None = None,
    role: str = "member",
) -> ProjectKnowledgeBase:
    """Idempotently link a KB to a project. NEVER overwrites another
    project's binding (that was the old 1-to-1 steal bug).

    Special case: if the KB has no home project yet, this link becomes the
    home (keeps ``project_id`` meaningful for legacy readers).
    """
    if kb.project_id == project.id:
        role = "home"
    elif kb.project_id is None and role == "member":
        # Adopt as home so legacy single-project readers keep seeing it.
        kb.project_id = project.id
        role = "home"

    existing = (
        db.query(ProjectKnowledgeBase)
        .filter(
            ProjectKnowledgeBase.project_id == project.id,
            ProjectKnowledgeBase.knowledge_base_id == kb.id,
        )
        .first()
    )
    if existing:
        if role == "home" and existing.role != "home":
            existing.role = "home"
            db.flush()
        return existing

    link = ProjectKnowledgeBase(
        project_id=project.id,
        knowledge_base_id=kb.id,
        role=role,
        created_by_id=created_by_id,
        org_id=kb.org_id,
        app_id=kb.app_id,
    )
    db.add(link)
    db.flush()
    return link


def detach_kb_from_project(db: Session, kb: KnowledgeBase, project: Project) -> bool:
    """Remove a KB's membership in a project. Returns True if a link was removed.

    Detaching the HOME project reassigns the home pointer to the first
    remaining linked project; with none left the KB becomes project-less
    (still visible in Connectors, attachable again).
    """
    link = (
        db.query(ProjectKnowledgeBase)
        .filter(
            ProjectKnowledgeBase.project_id == project.id,
            ProjectKnowledgeBase.knowledge_base_id == kb.id,
        )
        .first()
    )
    was_home = kb.project_id == project.id
    if link:
        db.delete(link)
        db.flush()

    if was_home:
        next_link = (
            db.query(ProjectKnowledgeBase)
            .filter(ProjectKnowledgeBase.knowledge_base_id == kb.id)
            .order_by(ProjectKnowledgeBase.created_date.asc())
            .first()
        )
        kb.project_id = next_link.project_id if next_link else None
        if next_link:
            next_link.role = "home"
    db.flush()
    return bool(link) or was_home
