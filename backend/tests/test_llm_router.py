"""Tests for LLM model routing precedence rules.

Covers:
  - Rule 0: System-agent short-circuit (agent_builder, skill_agent, automation_agent)
  - Rule 1: Project binding wins
  - Rule 2: Agent binding (when no project binding)
  - Rule 3: Catalog default
  - Rule 4: Legacy fallback
  - Admin-lock logic
  - Source field tracking
"""
import os
import sys
import uuid
from unittest.mock import patch

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models.base import TimestampedBase
from app.models.llm_model import LlmModel
from app.models.project import Project
from app.models.agent_app import AgentApp
from app.services.llm_router import (
    EffectiveLLM,
    LLMEndpoint,
    resolve_effective_llm,
)


# ---------------------------------------------------------------------------
# In-memory SQLite fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="function")
def db():
    engine = create_engine("sqlite:///:memory:")
    TimestampedBase.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


def _make_llm_model(db, *, name="Test Model", model_id="test-model",
                     base_url="https://api.test.com/v1", api_key="sk-test",
                     is_default=False, is_private=False, enabled=True,
                     org_id="default-org", app_id="default-app"):
    """Insert an LlmModel row and return it."""
    m = LlmModel(
        id=str(uuid.uuid4()),
        name=name,
        model_id=model_id,
        provider="test",
        base_url=base_url,
        api_key=api_key,
        is_default=is_default,
        is_private=is_private,
        enabled=enabled,
        org_id=org_id,
        app_id=app_id,
    )
    db.add(m)
    db.flush()
    return m


def _make_project(db, *, name="Test Project", llm_model_id=None,
                  resource_type="personal", org_id="default-org", app_id="default-app"):
    """Insert a Project row and return it."""
    p = Project(
        id=str(uuid.uuid4()),
        name=name,
        llm_model_id=llm_model_id,
        resource_type=resource_type,
        org_id=org_id,
        app_id=app_id,
    )
    db.add(p)
    db.flush()
    return p


def _make_agent(db, *, name="general_assistant", llm_model_id=None,
                is_system=False, resource_type="personal",
                org_id="default-org", app_id="default-app"):
    """Insert an AgentApp row and return it."""
    a = AgentApp(
        id=str(uuid.uuid4()),
        name=name,
        llm_model_id=llm_model_id,
        is_system=is_system,
        resource_type=resource_type,
        org_id=org_id,
        app_id=app_id,
    )
    db.add(a)
    db.flush()
    return a


# ---------------------------------------------------------------------------
# Helper: resolve with HIERARCHICAL_LLM_ENABLED=True
# ---------------------------------------------------------------------------

def _resolve(db, **kwargs):
    with patch("app.services.llm_router.settings") as mock_settings:
        mock_settings.HIERARCHICAL_LLM_ENABLED = True
        mock_settings.OPENAI_API_KEY = "sk-legacy"
        return resolve_effective_llm(db, **kwargs)


# ===========================================================================
# Rule 0: System-agent short-circuit
# ===========================================================================

class TestSystemAgentShortCircuit:
    """System meta-agents (agent_builder, skill_agent, automation_agent)
    always resolve to the catalog default, ignoring project/agent bindings."""

    def test_agent_builder_ignores_project_binding(self, db):
        """agent_builder with a project binding → still uses catalog default."""
        default_model = _make_llm_model(db, name="Default", model_id="default-model",
                                         is_default=True)
        proj_model = _make_llm_model(db, name="Project Model", model_id="proj-model")
        proj = _make_project(db, llm_model_id=proj_model.id)

        result = _resolve(db, project_id=proj.id, agent_name="agent_builder")
        assert result.model_name == "Default"
        assert result.source == "system_default"

    def test_skill_agent_ignores_agent_binding(self, db):
        """skill_agent with its own agent binding → still uses catalog default."""
        default_model = _make_llm_model(db, name="Default", model_id="default-model",
                                         is_default=True)
        agent_model = _make_llm_model(db, name="Agent Model", model_id="agent-model")
        agent = _make_agent(db, name="skill_agent", llm_model_id=agent_model.id,
                            is_system=True)

        result = _resolve(db, agent_name="skill_agent")
        assert result.model_name == "Default"
        assert result.source == "system_default"

    def test_automation_agent_ignores_both_bindings(self, db):
        """automation_agent with both project and agent bindings → catalog default."""
        default_model = _make_llm_model(db, name="Default", model_id="default-model",
                                         is_default=True)
        proj_model = _make_llm_model(db, name="Project Model", model_id="proj-model")
        agent_model = _make_llm_model(db, name="Agent Model", model_id="agent-model")
        proj = _make_project(db, llm_model_id=proj_model.id)
        agent = _make_agent(db, name="automation_agent", llm_model_id=agent_model.id,
                            is_system=True)

        result = _resolve(db, project_id=proj.id, agent_name="automation_agent")
        assert result.model_name == "Default"
        assert result.source == "system_default"

    def test_general_assistant_respects_project_binding(self, db):
        """general_assistant is NOT a system meta-agent — it respects project binding."""
        proj_model = _make_llm_model(db, name="Project Model", model_id="proj-model")
        proj = _make_project(db, llm_model_id=proj_model.id)

        result = _resolve(db, project_id=proj.id, agent_name="general_assistant")
        assert result.model_name == "Project Model"
        assert result.source == "project"

    def test_power_user_respects_agent_binding(self, db):
        """power_user is NOT a system meta-agent — it respects agent binding."""
        agent_model = _make_llm_model(db, name="Agent Model", model_id="agent-model")
        agent = _make_agent(db, name="power_user", llm_model_id=agent_model.id)

        result = _resolve(db, agent_name="power_user")
        assert result.model_name == "Agent Model"
        assert result.source == "agent"

    def test_system_agent_no_default_falls_to_legacy(self, db):
        """System agent with no catalog default → legacy fallback."""
        proj_model = _make_llm_model(db, name="Project Model", model_id="proj-model")
        proj = _make_project(db, llm_model_id=proj_model.id)

        result = _resolve(db, project_id=proj.id, agent_name="agent_builder")
        assert result.endpoint is None  # legacy path


# ===========================================================================
# Rule 1: Project binding
# ===========================================================================

class TestProjectBinding:
    """When a project has an LLM binding, that model is used."""

    def test_project_binding_resolves(self, db):
        model = _make_llm_model(db, name="Project LLM", model_id="proj-llm")
        proj = _make_project(db, llm_model_id=model.id)

        result = _resolve(db, project_id=proj.id)
        assert result.model_name == "Project LLM"
        assert result.source == "project"
        assert result.endpoint is not None
        assert result.endpoint.model_id == "proj-llm"

    def test_project_wins_over_agent_binding(self, db):
        """When both project and agent have bindings, project wins."""
        proj_model = _make_llm_model(db, name="Project LLM", model_id="proj-llm")
        agent_model = _make_llm_model(db, name="Agent LLM", model_id="agent-llm")
        proj = _make_project(db, llm_model_id=proj_model.id)
        agent = _make_agent(db, name="my_agent", llm_model_id=agent_model.id)

        result = _resolve(db, project_id=proj.id, agent_name="my_agent")
        assert result.model_name == "Project LLM"
        assert result.source == "project"

    def test_project_with_disabled_model_falls_through(self, db):
        """If the project's bound model is disabled, fall through to agent/default."""
        disabled_model = _make_llm_model(db, name="Disabled", model_id="disabled",
                                          enabled=False)
        default_model = _make_llm_model(db, name="Default", model_id="default-model",
                                         is_default=True)
        proj = _make_project(db, llm_model_id=disabled_model.id)

        result = _resolve(db, project_id=proj.id)
        assert result.model_name == "Default"
        assert result.source == "default"


# ===========================================================================
# Rule 2: Agent binding
# ===========================================================================

class TestAgentBinding:
    """When no project binding exists, agent binding is used."""

    def test_agent_binding_resolves(self, db):
        model = _make_llm_model(db, name="Agent LLM", model_id="agent-llm")
        agent = _make_agent(db, name="my_agent", llm_model_id=model.id)

        result = _resolve(db, agent_name="my_agent")
        assert result.model_name == "Agent LLM"
        assert result.source == "agent"

    def test_agent_binding_by_id(self, db):
        model = _make_llm_model(db, name="Agent LLM", model_id="agent-llm")
        agent = _make_agent(db, name="my_agent", llm_model_id=model.id)

        result = _resolve(db, agent_id=agent.id)
        assert result.model_name == "Agent LLM"
        assert result.source == "agent"

    def test_agent_with_disabled_model_falls_through(self, db):
        """If the agent's bound model is disabled, fall through to default."""
        disabled_model = _make_llm_model(db, name="Disabled", model_id="disabled",
                                          enabled=False)
        default_model = _make_llm_model(db, name="Default", model_id="default-model",
                                         is_default=True)
        agent = _make_agent(db, name="my_agent", llm_model_id=disabled_model.id)

        result = _resolve(db, agent_name="my_agent")
        assert result.model_name == "Default"
        assert result.source == "default"


# ===========================================================================
# Rule 3: Catalog default
# ===========================================================================

class TestCatalogDefault:
    """When no project or agent binding exists, the catalog default is used."""

    def test_default_resolves(self, db):
        model = _make_llm_model(db, name="Default LLM", model_id="default-llm",
                                 is_default=True)

        result = _resolve(db)
        assert result.model_name == "Default LLM"
        assert result.source == "default"

    def test_no_default_no_bindings_returns_legacy(self, db):
        """No default, no bindings → legacy fallback."""
        _make_llm_model(db, name="Non-default", model_id="non-default",
                         is_default=False)

        result = _resolve(db)
        assert result.endpoint is None  # legacy path


# ===========================================================================
# Rule 4: Legacy fallback
# ===========================================================================

class TestLegacyFallback:
    """When HIERARCHICAL_LLM_ENABLED is False or nothing matches, return legacy."""

    def test_hierarchical_disabled_returns_empty(self, db):
        with patch("app.services.llm_router.settings") as mock_settings:
            mock_settings.HIERARCHICAL_LLM_ENABLED = False
            result = resolve_effective_llm(db)
        assert result.endpoint is None
        assert result.model_name == ""

    def test_nothing_configured_returns_legacy(self, db):
        result = _resolve(db)
        assert result.endpoint is None


# ===========================================================================
# Admin-lock logic
# ===========================================================================

class TestAdminLock:
    """When a project/agent binding is company-scoped and the user is not an admin,
    the resolved model is locked."""

    def test_project_company_resource_locked_for_non_admin(self, db):
        model = _make_llm_model(db, name="Project LLM", model_id="proj-llm")
        proj = _make_project(db, llm_model_id=model.id, resource_type="company")

        result = _resolve(db, project_id=proj.id, user_is_admin=False)
        assert result.locked is True
        assert "admin" in result.locked_reason.lower()

    def test_project_company_resource_unlocked_for_admin(self, db):
        model = _make_llm_model(db, name="Project LLM", model_id="proj-llm")
        proj = _make_project(db, llm_model_id=model.id, resource_type="company")

        result = _resolve(db, project_id=proj.id, user_is_admin=True)
        assert result.locked is False

    def test_project_personal_resource_never_locked(self, db):
        model = _make_llm_model(db, name="Project LLM", model_id="proj-llm")
        proj = _make_project(db, llm_model_id=model.id, resource_type="personal")

        result = _resolve(db, project_id=proj.id, user_is_admin=False)
        assert result.locked is False

    def test_agent_company_resource_locked_for_non_admin(self, db):
        model = _make_llm_model(db, name="Agent LLM", model_id="agent-llm")
        agent = _make_agent(db, name="my_agent", llm_model_id=model.id,
                            resource_type="company")

        result = _resolve(db, agent_name="my_agent", user_is_admin=False)
        assert result.locked is True
        assert "agent" in result.locked_reason.lower()

    def test_agent_company_resource_unlocked_for_admin(self, db):
        model = _make_llm_model(db, name="Agent LLM", model_id="agent-llm")
        agent = _make_agent(db, name="my_agent", llm_model_id=model.id,
                            resource_type="company")

        result = _resolve(db, agent_name="my_agent", user_is_admin=True)
        assert result.locked is False


# ===========================================================================
# Source field tracking
# ===========================================================================

class TestSourceField:
    """Verify the source field is correctly set for each resolution path."""

    def test_source_project(self, db):
        model = _make_llm_model(db, name="P", model_id="p")
        proj = _make_project(db, llm_model_id=model.id)
        result = _resolve(db, project_id=proj.id)
        assert result.source == "project"

    def test_source_agent(self, db):
        model = _make_llm_model(db, name="A", model_id="a")
        agent = _make_agent(db, name="my_agent", llm_model_id=model.id)
        result = _resolve(db, agent_name="my_agent")
        assert result.source == "agent"

    def test_source_default(self, db):
        _make_llm_model(db, name="D", model_id="d", is_default=True)
        result = _resolve(db)
        assert result.source == "default"

    def test_source_system_default(self, db):
        _make_llm_model(db, name="D", model_id="d", is_default=True)
        result = _resolve(db, agent_name="agent_builder")
        assert result.source == "system_default"

    def test_source_legacy(self, db):
        result = _resolve(db)
        assert result.source == "legacy"

    def test_source_system_default_with_project_binding(self, db):
        """System agent with project binding → source is system_default, not project."""
        proj_model = _make_llm_model(db, name="P", model_id="p")
        default_model = _make_llm_model(db, name="D", model_id="d", is_default=True)
        proj = _make_project(db, llm_model_id=proj_model.id)

        result = _resolve(db, project_id=proj.id, agent_name="skill_agent")
        assert result.source == "system_default"
