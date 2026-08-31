"""Automation runtime agent — the hidden executor for scheduled automations.

One hidden ``automation_runtime_agent`` AgentApp exists per ``(org_id, app_id)``.
It is never shown in user-facing agent lists and never used for interactive
chat. ``ensure_automation_runtime_agent`` is idempotent and self-healing:
called at task-creation time, at executor-resolve time, and during startup
provisioning.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.models.agent_app import AgentApp
from app.models.agent_conversation import AgentConversation

logger = logging.getLogger(__name__)

RUNTIME_AGENT_NAME = "automation_runtime_agent"
RUNTIME_ROLE = "automation_runtime"

# Safe-parity toolset for the runtime agent. The runtime agent is the
# UNATTENDED scheduled-task executor, so it now carries the SAME
# general-purpose capabilities as ``general_assistant`` (web/browser,
# code/sandbox, media, skills, planning/memory, file ops, delegation, data
# query, artifacts, interactive control-flow tools, and ``execute_automation``)
# so any task the user can accomplish interactively can also be scheduled.
#
# DELIBERATE EXCLUSIONS (documented, intentional — NOT feature gaps):
#   * Admin tools (docker_compose_restart, update_env_config, cronjob) — an
#     unattended scheduler must not gain destructive admin power.
#   * Automation/agent CRUD (create_automation, update_automation,
#     delete_automation, create_agent, update_agent, delete_agent) — the
#     executor must not mutate its own schedules or the agent registry.
# ``execute_automation`` IS granted (the user can trigger their own tasks
# from within a run) but capped by a recursion-depth guard — see
# ``automation_chat_tool.execute_automation_tool`` and the
# ``parent_execution_id`` chain on ``AutomationExecution``.
RUNTIME_ENABLED_TOOLS = [
    # Data query
    "list_knowledge_bases", "list_data_sources", "describe_schema",
    "execute_query", "answer_from_database", "ask_data_agent",
    # Web / browser
    "web_search", "web_extract", "url_safety", "x_search", "agent_browser",
    # Memory / planning
    "memory", "todo", "kanban", "session_search",
    # Files / code / sandbox
    "read_file", "write_file", "execute_code", "run_sandbox_skill",
    "fuzzy_match", "patch_parser",
    "process_registry_list", "process_registry_tail", "process_registry_kill",
    # Media
    "image_generation",
    # Skills
    "skills", "skills_hub", "skills_guard", "skill_provenance",
    "skill_usage", "Skill", "load_skill_body",
    # Delegation / artifacts / notify
    "delegate_task", "create_artifact", "send_message",
    # Dashboard creation (required for scheduled dashboard/report tasks)
    "create_dashboard", "update_dashboard", "uiux_design_system",
    # Security
    "osv_check", "tirith_security",
    # Interactive control-flow (granted as-is per parity decision; in
    # unattended runs they post a message and rely on the run timeout).
    "clarify", "slash_confirm", "approval", "interrupt", "checkpoint_manager",
    # Run the user's own tasks (capped recursion)
    "execute_automation",
    # Forecasting (for nightly forecast scheduler)
    "forecast_discover", "forecast_run", "forecast_get",
    "forecast_accuracy", "forecast_rules", "forecast_report", "forecast_ppt",
]

_FORBIDDEN_TOOLS = frozenset({
    "create_automation", "update_automation", "delete_automation",
    "create_agent", "update_agent", "delete_agent",
    "docker_compose_restart", "update_env_config", "cronjob",
})


def _runtime_tool_config() -> dict:
    """Build the tool_config for the runtime agent (parity superset)."""
    return {
        "enabled_tools": [t for t in RUNTIME_ENABLED_TOOLS if t not in _FORBIDDEN_TOOLS],
        "disabled_tools": sorted(_FORBIDDEN_TOOLS),
    }


# Layer 3 Enterprise Harness fields — mirror ``general_assistant``'s
# ``_BASE_HARNESS`` (see system_agents.py) so the runtime agent has the same
# output contract, evaluation profile, trace/log, and bindings surface.
def _runtime_base_harness() -> dict:
    return {
        "trace_enabled": True,
        "log_level": "info",
        "memory_scope": "app_shared",
        "data_bindings": [],
        "skill_bindings": [],
        "output_contract": {
            "allowed_artifact_types": ["markdown", "json", "csv", "text", "docx", "pdf", "pptx", "html", "dashboard"],
            "must_include_sources": True,
            "citation_format": "inline",
            "max_response_length": 8192,
        },
        "evaluation_profile": {
            "test_cases": [],
            "trace_replay_enabled": True,
            "grounding_checks": ["source_citation", "hallucination_check"],
            "expected_accuracy": 0.85,
        },
    }


# Policy profile mirroring ``general_assistant``. requires_confirmation is
# False because the runtime agent runs unattended — a confirmation gate would
# stall every scheduled run indefinitely.
def _runtime_policy_profile() -> dict:
    return {
        "risk_tier": "low",
        "requires_confirmation": False,
        "max_concurrent_calls": 5,
        "rate_limit_per_minute": 60,
        "allowed_domains": [],
        "retention_days": 30,
    }


def _runtime_manifest() -> dict:
    return {
        "agent_name": RUNTIME_AGENT_NAME,
        "version": "1.1.0",
        "mission": (
            "Serve as a versatile unattended AI executor with the full zhanlu "
            "toolset for scheduled automation tasks: web/browser, code/sandbox, "
            "media, skills, planning/memory, file ops, delegation, data query, "
            "artifacts, and triggering the user's own tasks — while honoring "
            "the task's project_id for data isolation."
        ),
        "task_scope": [
            "scheduled_automation_execution", "report_generation", "data_read",
            "web_search", "code_execution", "file_operations", "delegation",
            "dashboard_creation", "dashboard_editing",
        ],
        "boundaries": {
            "allowed": [t for t in RUNTIME_ENABLED_TOOLS if t not in _FORBIDDEN_TOOLS],
            "forbidden": sorted(_FORBIDDEN_TOOLS),
        },
        "risk_tier": "low",
        "created_by": "system",
    }


# Extended identity prompt. Covers the full toolset + general_assistant's
# mission so the LLM knows it may use every granted tool, while keeping the
# data-isolation and anti-mutation guardrails appropriate for unattended runs.
# (Tool JSON schemas are injected separately via the ``tools`` API param, so
# the LLM sees both this identity text AND every enabled tool's schema.)
_RUNTIME_SYSTEM_PROMPT = (
    "You are the automation runtime agent — a versatile unattended AI executor "
    "that runs scheduled tasks on behalf of the user. You possess the FULL "
    "zhanlu toolset: web search and extraction (web_search, web_extract, "
    "url_safety, x_search), browser automation (agent_browser), memory and "
    "planning (memory, todo, kanban, session_search), file operations "
    "(read_file, write_file), code execution and sandbox skills (execute_code, "
    "run_sandbox_skill, fuzzy_match, patch_parser, process_registry_*), image "
    "generation (image_generation), dynamic skill discovery and management "
    "(skills, skills_hub, skills_guard, skill_provenance, skill_usage, Skill, "
    "load_skill_body), task delegation (delegate_task, ask_data_agent), data "
    "query (list_knowledge_bases, list_data_sources, describe_schema, "
    "execute_query, answer_from_database), artifact creation (create_artifact), "
    "dashboard creation and editing (create_dashboard, update_dashboard, "
    "uiux_design_system), chat notification (send_message), security checks (osv_check, "
    "tirith_security), interactive control-flow tools (clarify, "
    "slash_confirm, approval, interrupt, checkpoint_manager), and the ability "
    "to trigger the user's own automation tasks (execute_automation).\n\n"
    "For each run: read only data bound to the task's project_id for data "
    "isolation, produce the requested output (report/file/artifact), and send "
    "the result back to the user's chat. You may chain tools, delegate "
    "sub-tasks, search the web, execute code, and generate media exactly as "
    "the general assistant would. Treat each run as stateless from your own "
    "perspective, but read the per-project memory ledger for cross-run "
    "continuity.\n\n"
    "Guardrails (unchanged): NEVER create, update, or delete automations or "
    "agents, and NEVER use admin tools (docker_compose_restart, "
    "update_env_config, cronjob) — they are not in your toolset. When you "
    "trigger another automation via execute_automation, respect the recursion "
    "depth cap and do not create infinite chains."
)


def _runtime_model() -> str:
    """Inherit the project's configured LLM model so the runtime agent works
    with whatever provider (DeepSeek / OpenAI / etc.) is configured via
    OPENAI_BASE_URL. Hard-coding 'gpt-4o-mini' returned 400 against non-OpenAI
    endpoints. Parity here means 'resolves to a real working model', not the
    same model string as general_assistant (which hardcodes 'gpt-4o')."""
    try:
        from app.config import settings as _runtime_settings
        return getattr(_runtime_settings, "LLM_MODEL", None) or "deepseek-chat"
    except Exception:
        return "deepseek-chat"


def _runtime_agent_fields() -> dict:
    """Return the full field set for the runtime agent.

    Centralized so the create path and the idempotent refresh path can never
    drift. Mirrors the Layer 3 harness + policy fields general_assistant
    carries (see system_agents.py ``_BASE_HARNESS`` / ``policy_profile``).
    """
    harness = _runtime_base_harness()
    return {
        "description": (
            "Hidden unattended executor for scheduled automations. Carries "
            "safe-parity with general_assistant (full general-purpose toolset; "
            "admin + automation/agent CRUD excluded). Not user-editable."
        ),
        "capabilities": [
            "automation_execution", "report_generation", "web_search",
            "code_execution", "file_operations", "delegation", "memory",
            "dashboard_creation", "dashboard_editing",
        ],
        "tool_config": _runtime_tool_config(),
        "manifest_json": _runtime_manifest(),
        "prompt_identity": _RUNTIME_SYSTEM_PROMPT,
        "is_system": True,
        "role": RUNTIME_ROLE,
        "memory_scope": harness["memory_scope"],
        "trace_enabled": harness["trace_enabled"],
        "log_level": harness["log_level"],
        "data_bindings": harness["data_bindings"],
        "skill_bindings": harness["skill_bindings"],
        "output_contract": harness["output_contract"],
        "evaluation_profile": harness["evaluation_profile"],
        "policy_profile": _runtime_policy_profile(),
    }


def ensure_automation_runtime_agent(
    db: Session, org_id: str, app_id: str
) -> AgentApp:
    """Idempotently get-or-create the hidden automation runtime agent.

    Returns the existing ``AgentApp`` with ``name=RUNTIME_AGENT_NAME``,
    ``org_id``, ``app_id``, ``role=RUNTIME_ROLE`` if one exists; otherwise
    creates and returns it. Safe to call on every task creation and every
    executor resolve.

    On an existing row, refreshes the parity config (tool_config, prompt,
    harness, policy, manifest, model) so already-provisioned runtime agents
    upgrade to the current safe-parity toolset without a manual re-seed —
    mirroring ``ensure_system_agents``'s refresh behaviour. The refresh is a
    no-op when the stored config already matches, so repeated resolves stay
    cheap once steady-state is reached.
    """
    existing = db.query(AgentApp).filter(
        AgentApp.name == RUNTIME_AGENT_NAME,
        AgentApp.org_id == org_id,
        AgentApp.app_id == app_id,
        AgentApp.role == RUNTIME_ROLE,
        AgentApp.is_deleted == False,  # noqa: E712
    ).first()

    fields = _runtime_agent_fields()
    model = _runtime_model()

    if existing is None:
        agent = AgentApp(
            name=RUNTIME_AGENT_NAME,
            project="global",
            model=model,
            agent_type="sequential",
            topology="standalone",
            status="active",
            org_id=org_id,
            app_id=app_id,
            # created_by_id stays None — system-owned
            **fields,
        )
        db.add(agent)
        db.commit()
        db.refresh(agent)
        logger.info(
            "Created automation_runtime_agent for (org=%s, app=%s) id=%s",
            org_id, app_id, agent.id,
        )
        return agent

    # Idempotent refresh: upgrade existing rows to the current parity config.
    # Only commit when something actually changed, so a steady-state resolve
    # performs no writes.
    changed = False
    if existing.model != model:
        existing.model = model
        changed = True
    for key, value in fields.items():
        if getattr(existing, key, None) != value:
            setattr(existing, key, value)
            changed = True
    if changed:
        db.add(existing)
        db.commit()
        db.refresh(existing)
        logger.info(
            "Refreshed automation_runtime_agent config for (org=%s, app=%s) id=%s",
            org_id, app_id, existing.id,
        )
    return existing


def backfill_automation_runtime_agents(db: Session) -> int:
    """Ensure a runtime agent exists for every (org, app) that has automation
    tasks, and rebind tasks whose ``agent_id`` is NULL or points at a
    missing/deleted agent.

    Idempotent and safe to re-run. Returns the number of tasks rebound.
    """
    from app.models.automation_task import AutomationTask

    pairs = db.query(
        AutomationTask.org_id, AutomationTask.app_id
    ).filter(
        AutomationTask.is_deleted == False,  # noqa: E712
    ).distinct().all()

    rebound = 0
    for org_id, app_id in pairs:
        runtime = ensure_automation_runtime_agent(db, org_id, app_id)
        # Find tasks in this (org, app) with NULL or invalid agent_id.
        tasks = db.query(AutomationTask).filter(
            AutomationTask.org_id == org_id,
            AutomationTask.app_id == app_id,
            AutomationTask.is_deleted == False,  # noqa: E712
        ).all()
        for task in tasks:
            if task.agent_id:
                # Validate the pinned agent is still usable.
                pinned = db.query(AgentApp).filter(
                    AgentApp.id == task.agent_id,
                    AgentApp.is_deleted == False,  # noqa: E712
                ).first()
                if pinned is not None:
                    continue  # valid user/system agent — leave it
            task.agent_id = runtime.id
            rebound += 1
        db.commit()
    if rebound:
        logger.info("backfill_automation_runtime_agents: rebound %d tasks", rebound)
    return rebound


# ---------------------------------------------------------------------------
# Per-project memory ledger
# ---------------------------------------------------------------------------

# Max ledger entries kept in the per-project conversation's messages list.
# Older entries are trimmed to bound the LLM context window.
_MAX_MEMORY_ENTRIES = 50


def get_or_create_project_conversation(
    db: Session, runtime_agent: AgentApp, project_id: Optional[str]
) -> AgentConversation:
    """Idempotently get-or-create the per-project memory ledger conversation.

    All automations in the same project share this conversation so the
    runtime agent builds up project knowledge across runs (Notion-AI style).
    Keyed by (agent_name, project_id, org_id, app_id). When project_id is
    None, all "global" tasks in the same org/app share one conversation.
    """
    pid = project_id or "global"
    # Lookup: project_id may be None (FK-safe) or a real project id.
    if project_id is None:
        existing = db.query(AgentConversation).filter(
            AgentConversation.agent_name == RUNTIME_AGENT_NAME,
            AgentConversation.project_id.is_(None),
            AgentConversation.org_id == runtime_agent.org_id,
            AgentConversation.app_id == runtime_agent.app_id,
            AgentConversation.is_deleted == False,  # noqa: E712
        ).first()
    else:
        existing = db.query(AgentConversation).filter(
            AgentConversation.agent_name == RUNTIME_AGENT_NAME,
            AgentConversation.project_id == project_id,
            AgentConversation.org_id == runtime_agent.org_id,
            AgentConversation.app_id == runtime_agent.app_id,
            AgentConversation.is_deleted == False,  # noqa: E712
        ).first()
    if existing:
        return existing

    conv = AgentConversation(
        agent_name=RUNTIME_AGENT_NAME,
        title=f"Automation memory · project {pid}",
        messages=[{
            "role": "system",
            "content": (
                "Per-project automation memory ledger. Each entry records "
                "one scheduled run's prompt, status, and output summary. "
                "The runtime agent reads this for cross-run continuity."
            ),
        }],
        status="active",
        project_id=project_id,  # raw value; None is FK-safe (nullable column)
        org_id=runtime_agent.org_id,
        app_id=runtime_agent.app_id,
        metadata_={"kind": "automation_runtime_memory", "project_id": pid},
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def append_run_summary(
    db: Session,
    conv: AgentConversation,
    run_id: str,
    status: str,
    summary: str,
) -> None:
    """Append a compact ledger entry for one run, trimming old entries."""
    from datetime import datetime, timezone
    msgs = list(conv.messages or [])
    msgs.append({
        "role": "assistant",
        "content": f"[run {run_id} @ {datetime.now(timezone.utc).isoformat()}] "
                    f"status: {status} | {summary[:500]}",
    })
    # Trim to the most recent N entries (keep the system message at index 0).
    if len(msgs) > _MAX_MEMORY_ENTRIES:
        msgs = [msgs[0]] + msgs[-(_MAX_MEMORY_ENTRIES - 1):]
    conv.messages = msgs
    db.add(conv)
    db.commit()
