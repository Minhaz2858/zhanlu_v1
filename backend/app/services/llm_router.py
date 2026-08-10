"""Hierarchical LLM endpoint resolution.

Resolution precedence (highest wins):
  1. Project.llm_model_id  →  lookup in llm_models
  2. AgentApp.llm_model_id →  lookup in llm_models  (skipped if project resolved)
  3. llm_models.is_default = True →  catalog default  (skipped if above resolved)
  4. Global fallback: settings.LLM_BASE_URL + settings.LLM_API_KEY

Note: when BOTH project and agent are bound, **project always wins** —
the agent's own binding is ignored. This keeps the chat experience
consistent across all agents inside a single project.

Admin-lock: when bound at project/agent level (scope=company & current user
is NOT an admin), the resolved model is *locked* — the chat header renders
a read-only badge, and client-sent ``body.model`` is ignored by the server.

Gated by ``settings.HIERARCHICAL_LLM_ENABLED``.  When False, returns
``None`` for ``LLMEndpoint`` and the caller falls back to the legacy path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.models.llm_model import LlmModel
from app.services.crypto_utils import decrypt_value

logger = logging.getLogger(__name__)


@dataclass
class EffectiveLLM:
    """Resolved LLM configuration returned by ``resolve_effective_llm``."""

    endpoint: Optional["LLMEndpoint"] = None
    """The concrete (base_url, api_key, model_id) to use.  None = use legacy globals."""

    model_name: str = ""
    """Human-readable label for the chat header badge."""

    model_id: str = ""
    """The ``model`` field to send in the OpenAI payload."""

    locked: bool = False
    """If True, the frontend must render a read-only badge (no dropdown swap)."""

    locked_reason: str = ""
    """Human-readable reason for the lock, e.g. '🔒 Set by admin for this project'."""


@dataclass
class LLMEndpoint:
    """Self-contained credentials for one LLM provider entry."""

    base_url: str
    api_key: str
    model_id: str
    is_private: bool = False


def resolve_effective_llm(
    db: Session,
    *,
    project_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    agent_name: Optional[str] = None,
    user_model: Optional[str] = None,
    user_is_admin: bool = False,
    org_id: str = "default-org",
    app_id: str = "default-app",
) -> EffectiveLLM:
    """Resolve which LLM model + endpoint to use for this request.

    Precedence: Project > Agent > user model picker > catalog default > legacy global.

    Admin-lock: when ``user_is_admin`` is False and the resolved model comes
    from a project or agent that has ``resource_type='company'``, the result
    is locked.
    """
    if not settings.HIERARCHICAL_LLM_ENABLED:
        return EffectiveLLM()  # endpoint=None → legacy path

    def _lookup(llm_model_id: str | None) -> LlmModel | None:
        if not llm_model_id:
            return None
        return (
            db.query(LlmModel)
            .filter(
                LlmModel.id == llm_model_id,
                LlmModel.enabled.is_(True),
                LlmModel.is_deleted.is_(False),
            )
            .first()
        )

    def _lookup_by_model_id(model_id: str) -> LlmModel | None:
        return (
            db.query(LlmModel)
            .filter(
                LlmModel.model_id == model_id,
                LlmModel.enabled.is_(True),
                LlmModel.is_deleted.is_(False),
                LlmModel.org_id == org_id,
                LlmModel.app_id == app_id,
            )
            .first()
        )

    resolved: LlmModel | None = None
    locked = False
    locked_reason = ""
    source = ""

    # 1) Project binding
    if project_id:
        from app.models.project import Project
        proj = db.query(Project).filter(Project.id == project_id, Project.is_deleted.is_(False)).first()
        if proj and proj.llm_model_id:
            resolved = _lookup(proj.llm_model_id)
            if resolved:
                source = "project"
                if not user_is_admin and proj.resource_type == "company":
                    locked = True
                    locked_reason = "🔒 Set by admin for this project"

    # 2) Agent binding (only if project didn't resolve)
    if (agent_id or agent_name) and resolved is None:
        from app.models.agent_app import AgentApp
        q = db.query(AgentApp).filter(AgentApp.is_deleted.is_(False))
        if agent_id:
            q = q.filter(AgentApp.id == agent_id)
        else:
            q = q.filter(AgentApp.name == agent_name)
        agent = q.first()
        if agent and agent.llm_model_id:
            resolved = _lookup(agent.llm_model_id)
            if resolved:
                source = "agent"
                if not user_is_admin and agent.resource_type == "company":
                    locked = True
                    locked_reason = "🔒 Set by admin for this agent"

    # 3) Catalog default (only reached if neither Project nor Agent had a binding)
    if resolved is None:
        resolved = (
            db.query(LlmModel)
            .filter(
                LlmModel.is_default.is_(True),
                LlmModel.enabled.is_(True),
                LlmModel.is_deleted.is_(False),
                LlmModel.org_id == org_id,
                LlmModel.app_id == app_id,
            )
            .first()
        )
        if resolved:
            source = "default"

    # 5) Legacy fallback — caller handles endpoint=None
    if resolved is None:
        logger.debug("No catalog match → falling back to legacy LLM_BASE_URL / LLM_API_KEY")
        return EffectiveLLM()

    api_key = decrypt_value(resolved.api_key) or settings.LLM_API_KEY
    endpoint = LLMEndpoint(
        base_url=resolved.base_url,
        api_key=api_key,
        model_id=resolved.model_id,
        is_private=resolved.is_private,
    )

    logger.debug(
        "Resolved LLM: model=%s source=%s locked=%s",
        resolved.name, source, locked,
    )
    return EffectiveLLM(
        endpoint=endpoint,
        model_name=resolved.name,
        model_id=resolved.model_id,
        locked=locked,
        locked_reason=locked_reason,
    )
