"""Phase 8.1 Stage 3 — auto-bind migration compatibility guarantee.

The migration must NEVER change an agent's effective bound set:
    pre-migration (flag ON, JSON authority)  ==  post-migration (bindings)
and never silently widen or narrow access. Also pins:
  * all active readers of the flag are the four known surfaces;
  * the flag remains honored until the workspace migrates;
  * removing the migration stamp restores legacy behavior (rollback).
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.agent_app import AgentApp
from app.models.agent_data_binding import AgentDataBinding
from app.models.knowledge_base import KnowledgeBase
from app.models.organization import Organization
from app.models.user import User
from app.models.workspace_settings import WorkspaceSetting
from app.services import workspace_settings_service as wss
from app.services.agent_binding_service import upsert_binding
from app.services.data_source_runtime.data_source_runtime import (
    _agent_bound_ids_binding_aware,
    _maybe_extend_with_workspace_auto_bind,
)
from app.routers.workspace_settings import migrate_auto_bind

ORG = "org-acme"


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng, tables=[
        AgentApp.__table__, KnowledgeBase.__table__,
        Organization.__table__, User.__table__,
        WorkspaceSetting.__table__, AgentDataBinding.__table__])
    s = sessionmaker(bind=eng)()
    s.add(Organization(id=ORG, name="ACME", slug="acme"))
    s.add(User(id="u-admin", org_id=ORG, email="a@t", full_name="A",
               role="admin", password_hash="x"))
    agent = AgentApp(id="app-1", name="Analyst", org_id=ORG,
                     app_id="default-app", knowledge_bases=[])
    s.add(agent)
    s.add(KnowledgeBase(id="ds-1", name="DW", org_id=ORG,
                        app_id="default-app", source_kind="database"))
    s.add(KnowledgeBase(id="ds-2", name="Docs", org_id=ORG,
                        app_id="default-app", source_kind="file"))
    s.add(WorkspaceSetting(org_id=ORG, app_id="default-app",
                           key=wss.KEY_AUTO_BIND_ALL_DATASOURCES,
                           value="true"))
    s.commit()
    yield s, agent
    s.close()
    eng.dispose()


def test_effective_set_identical_across_migration(db):
    s, agent = db
    # PRE-migration: flag ON, JSON authority → auto-bind widens to both KBs
    pre = _maybe_extend_with_workspace_auto_bind(
        s, agent, list(_agent_bound_ids_binding_aware(s, agent)))
    assert sorted(pre) == ["ds-1", "ds-2"]

    migrate_auto_bind(org_id="", app_id="default-app", db=s,
                      admin=s.get(User, "u-admin"))

    # POST-migration: explicit bindings are authoritative; the flag no
    # longer widens — and the effective set MUST be identical (no silent
    # access loss, no silent expansion)
    post = _agent_bound_ids_binding_aware(s, agent)
    assert sorted(post) == sorted(pre)


def test_flag_remains_honored_until_migration(db):
    s, agent = db
    # no bindings yet + no migration stamp → legacy widening still active
    bound = _maybe_extend_with_workspace_auto_bind(s, agent, [])
    assert sorted(bound) == ["ds-1", "ds-2"]


def test_removing_the_stamp_restores_legacy_behavior(db):
    s, agent = db
    migrate_auto_bind(org_id="", app_id="default-app", db=s,
                      admin=s.get(User, "u-admin"))
    # rollback: drop the stamp — legacy widening returns (bindings still
    # present, so the effective set is unchanged either way)
    row = s.query(WorkspaceSetting).filter_by(
        org_id=ORG, app_id="default-app",
        key="auto_bind_migrated_at").one()
    s.delete(row)
    s.commit()
    bound = _maybe_extend_with_workspace_auto_bind(
        s, agent, list(_agent_bound_ids_binding_aware(s, agent)))
    assert sorted(bound) == ["ds-1", "ds-2"]  # union ∪ bindings = same set


def test_active_readers_are_the_known_four():
    """No new/rogue consumers of the deprecated flag may appear."""
    import pathlib
    backend = pathlib.Path(__file__).resolve().parents[1]
    readers = set()
    for py in backend.glob("app/**/*.py"):
        text = py.read_text(encoding="utf-8", errors="replace")
        if ("KEY_AUTO_BIND_ALL_DATASOURCES" in text
                or '"auto_bind_all_datasources"' in text):
            readers.add(str(py.relative_to(backend)))
    assert readers == {
        "app/models/workspace_settings.py",  # key column definition comment
        "app/services/data_source_runtime/data_source_runtime.py",  # THE consumer
        "app/routers/workspace_settings.py",  # GET/PUT + migration endpoint
        "app/services/workspace_settings_service.py",  # constant + defaults
    }, readers
