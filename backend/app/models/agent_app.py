"""AgentApp model — the most complex entity with 30+ fields including JSON columns."""

from typing import TYPE_CHECKING, Optional

from sqlalchemy import String, Text, Integer, Float, Boolean, JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import TimestampedBase

if TYPE_CHECKING:
    from app.models.llm_model import LlmModel


class AgentApp(TimestampedBase):
    __tablename__ = "agent_apps"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    project: Mapped[str | None] = mapped_column(String(255), nullable=True, default="global")
    project_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("projects.id"), nullable=True, index=True
    )

    # Capabilities and model config
    capabilities: Mapped[list | None] = mapped_column(JSON, nullable=True)
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    agent_type: Mapped[str | None] = mapped_column(String(50), nullable=True, default="sequential")

    # Prompt layers
    prompt_identity: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_boundary: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_tools: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_output: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Skills and knowledge bases (JSON arrays of IDs)
    skills: Mapped[list | None] = mapped_column(JSON, nullable=True)
    knowledge_bases: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Topology (simple mode)
    topology: Mapped[str | None] = mapped_column(String(50), nullable=True, default="standalone")
    sub_agents: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Flow (advanced mode)
    flow_mode: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    flow: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Limits
    max_call_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_retries: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_iterations: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Data access
    data_read: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    data_write: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    human_fallback: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)

    # Tracing and logging
    trace_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    log_level: Mapped[str | None] = mapped_column(String(20), nullable=True, default="info")

    # Model parameters
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    top_p: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Status
    status: Mapped[str | None] = mapped_column(String(50), nullable=True, default="draft")

    # Hierarchical LLM config — FK to llm_models catalog (gated by HIERARCHICAL_LLM_ENABLED)
    llm_model_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("llm_models.id"), nullable=True, index=True,
    )
    llm_model: Mapped[Optional["LlmModel"]] = relationship("LlmModel", foreign_keys=[llm_model_id])

    # Per-agent tool configuration — controls which tools are available.
    # Expected shape: {"enabled_tools": ["web_search", "memory", ...], "disabled_tools": [...]}
    # When null, falls back to agent_name-based defaults.
    tool_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Progressive disclosure — when True, only skill name + description +
    # summary are injected into the system prompt. The full skill_md body
    # is loaded on-demand via the ``load_skill_body`` tool.
    # Default True for new agents; existing rows migrate to False for
    # backward compat (no behavior change for live agents).
    progressive_disclosure: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # ``is_system`` marks an agent that ships with the platform
    # (agent_builder, skill_agent, automation_agent, general_assistant,
    # power_user, data_agent, ...). System agents are seeded on startup
    # by ``ensure_system_agents()``; the frontend hides them from
    # user-facing agent lists (My Space, the agent picker, the active
    # agent chip) but the runtime still uses them — in particular
    # ``general_assistant`` is auto-selected silently for any chat that
    # has no user-picked agent, so Ungrouped sessions still get a
    # date anchor and a real-time toolset.
    is_system: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, index=True,
    )

    # resource_type is stamped server-side at creation from the creator's
    # role (admin→'company', user→'personal').  Immutable — clients can
    # never change it via PUT (added to _IMMUTABLE_FIELDS).
    resource_type: Mapped[str] = mapped_column(
        String(20), default="personal", nullable=False, index=True,
    )

    # Functional role of the agent. NULL = a normal user agent.
    # "automation_runtime" = the hidden executor that runs scheduled
    # automations — never shown in user-facing agent lists, never used
    # for interactive chat. Scoped to (org_id, app_id) so every tenant
    # gets exactly one.
    role: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True,
        doc="Functional role. NULL=user agent; "
            "'automation_runtime'=hidden scheduled-automation executor.",
    )

    # --- Enterprise architecture (Phase 6) ---

    # Agent Manifest — mission, task scope, boundaries, output contract
    manifest_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Data bindings — which datasources, tables, columns (read-only defaults)
    data_bindings: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Skill bindings — explicit allowed/blocked skills with versions
    skill_bindings: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Memory scope — "user_only" | "app_shared" | "org_shared"
    memory_scope: Mapped[str | None] = mapped_column(String(30), nullable=True, default="user_only")

    # Policy profile — risk tier, confirmation requirements
    policy_profile: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Output contract — allowed artifact types, must-include source refs
    output_contract: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Evaluation profile — test cases, trace replay, grounding checks
    evaluation_profile: Mapped[dict | None] = mapped_column(JSON, nullable=True)
