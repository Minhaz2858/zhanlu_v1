"""Hierarchical LLM endpoint resolution.

Resolution precedence (highest wins):
  0. System-agent short-circuit → catalog default (ignores project/agent bindings)
  1. Project.llm_model_id  →  lookup in llm_models
  2. AgentApp.llm_model_id →  lookup in llm_models  (skipped if project resolved)
  3. llm_models.is_default = True →  catalog default  (skipped if above resolved)
  4. Global fallback: settings.OPENAI_BASE_URL + settings.OPENAI_API_KEY

Note: when BOTH project and agent are bound, **project always wins** —
the agent's own binding is ignored. This keeps the chat experience
consistent across all agents inside a single project.

Rule 0: System meta-agents (agent_builder, skill_agent, automation_agent)
ALWAYS use the catalog default, regardless of project/agent bindings.
This prevents infrastructure agents from being influenced by project-level
model changes.

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

# System meta-agents that always use the catalog default, ignoring
# project/agent-level LLM bindings.  These are infrastructure agents
# whose behaviour must be stable regardless of project context.
_SYSTEM_META_AGENTS = frozenset({
    "agent_builder",    # Agent creation wizard
    "skill_agent",      # Skill execution agent
    "automation_agent", # Automation run agent
})


@dataclass
class EffectiveLLM:
    """Resolved LLM configuration returned by ``resolve_effective_llm``."""

    endpoint: Optional["LLMEndpoint"] = None
    """The concrete (base_url, api_key, model_id) to use.  None = use legacy globals."""

    model_name: str = ""
    """Human-readable label for the chat header badge."""

    model_id: str = ""
    """The ``model`` field to send in the OpenAI payload."""

    source: str = ""
    """Origin of the resolved model: project | agent | default | system_default | legacy."""

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
    bypass_hallucination_guardrail: bool = False
    provider: str = ""
    context_window: Optional[int] = None
    """Real context window in tokens (e.g. 16384 for small vLLM). None = heuristic default."""
    max_output_tokens: Optional[int] = None
    """Per-model output token cap. None = use user setting / global hard cap."""
    supports_structured_tool_calls: bool = True
    """False for vLLM without --enable-auto-tool-choice (tool calls arrive as XML in content)."""


def resolve_message_project_id(
    db: Session,
    *,
    conv_project_id: Optional[str],
    body_project_id: Optional[str],
    body_project_name: Optional[str] = None,
) -> Optional[str]:
    """Effective project scope for a chat message.

    The frontend sends the live-URL ``project_id`` on every message. A
    conversation may have been created without one (legacy rows, chats
    first opened from the main page), so ``conv.project_id`` alone can
    miss the currently-selected project — the agent would then think
    with the catalog-default model while reading the project's data
    sources.

    Precedence mirrors the data-source runtime in ``agents.py``:
    ``body_project_id`` (validated against the live projects table)
    wins, then ``body_project_name`` (resolved case-insensitively),
    then ``conv_project_id``. A stale or soft-deleted body id/name is
    dropped and the conv value is returned instead.
    """
    from app.models.project import Project

    if body_project_id and body_project_id != conv_project_id:
        row = (
            db.query(Project)
            .filter(Project.id == body_project_id, Project.is_deleted.is_(False))
            .first()
        )
        if row is not None:
            return row.id
        logger.debug(
            "resolve_message_project_id: dropping stale body project_id %s "
            "(project not found or soft-deleted)",
            body_project_id,
        )
    if body_project_name:
        from sqlalchemy import func

        row = (
            db.query(Project)
            .filter(
                func.lower(Project.name) == body_project_name.lower(),
                Project.is_deleted.is_(False),
            )
            .first()
        )
        if row is not None and row.id != conv_project_id:
            return row.id
    return conv_project_id


def _apply_context_window_probe(endpoint: "LLMEndpoint") -> "LLMEndpoint":
    """Best-effort: fill ``endpoint.context_window`` when the admin did not set it.

    Probes the endpoint's real window (vLLM ``max_model_len``, Ollama
    ``context_length``) so ANY model — no matter how new/obscure — gets
    its TRUE compaction budget instead of the 128k heuristic default.
    Admin-configured values always win; probe failures are non-fatal.
    """
    if endpoint.context_window and endpoint.context_window > 0:
        return endpoint
    try:
        from app.services.context_probe import probe_context_window

        probed = probe_context_window(
            endpoint.base_url, endpoint.api_key, endpoint.model_id,
        )
        if probed and probed > 0:
            endpoint.context_window = probed
    except Exception as exc:  # noqa: BLE001 — never break resolution on probe
        logger.debug("context-window probe skipped: %s", exc)
    return endpoint


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

    Precedence: System-agent short-circuit > Project > Agent > catalog default > legacy global.

    Admin-lock: when ``user_is_admin`` is False and the resolved model comes
    from a project or agent that has ``resource_type='company'``, the result
    is locked.
    """
    if not settings.HIERARCHICAL_LLM_ENABLED:
        return EffectiveLLM(source="legacy")  # endpoint=None → legacy path

    # Rule 0: System-agent short-circuit — always use catalog default
    if agent_name and agent_name in _SYSTEM_META_AGENTS:
        default = (
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
        if default:
            api_key = decrypt_value(default.api_key) or settings.OPENAI_API_KEY
            endpoint = LLMEndpoint(
                base_url=default.base_url,
                api_key=api_key,
                model_id=default.model_id,
                is_private=default.is_private,
                bypass_hallucination_guardrail=default.bypass_hallucination_guardrail,
                provider=default.provider,
                context_window=default.context_window,
                max_output_tokens=default.max_output_tokens,
                supports_structured_tool_calls=default.supports_structured_tool_calls,
            )
            logger.debug(
                "System-agent short-circuit: agent=%s → default model=%s",
                agent_name, default.name,
            )
            endpoint = _apply_context_window_probe(endpoint)
            return EffectiveLLM(
                endpoint=endpoint,
                model_name=default.name,
                model_id=default.model_id,
                source="system_default",
            )
        # No catalog default → fall through to legacy
        logger.debug(
            "System-agent short-circuit: agent=%s → no default, legacy fallback",
            agent_name,
        )
        return EffectiveLLM(source="system_default")

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
        return EffectiveLLM(source="legacy")

    api_key = decrypt_value(resolved.api_key) or settings.OPENAI_API_KEY
    endpoint = LLMEndpoint(
        base_url=resolved.base_url,
        api_key=api_key,
        model_id=resolved.model_id,
        is_private=resolved.is_private,
        bypass_hallucination_guardrail=resolved.bypass_hallucination_guardrail,
        provider=resolved.provider,
        context_window=resolved.context_window,
        max_output_tokens=resolved.max_output_tokens,
        supports_structured_tool_calls=resolved.supports_structured_tool_calls,
    )

    logger.debug(
        "Resolved LLM: model=%s source=%s locked=%s",
        resolved.name, source, locked,
    )
    endpoint = _apply_context_window_probe(endpoint)
    return EffectiveLLM(
        endpoint=endpoint,
        model_name=resolved.name,
        model_id=resolved.model_id,
        source=source,
        locked=locked,
        locked_reason=locked_reason,
    )
