"""Idempotent system-agent seeding.

Ensures the DB-backed system meta-agents (agent_builder, skill_agent,
automation_agent, general_assistant, power_user) always exist in the
``agent_apps`` table with up-to-date tool_config. These are normally
created by ``seed.py``, but if the database was partially seeded (e.g.
a user existed before the system agents were added) they may be
missing — which breaks tool_config resolution, data source binding, and
agent listing.

``data_agent`` is intentionally NOT created here: it is a code-only
builtin (lives in ``BUILTIN_AGENTS``) and is invoked via the
``ask_data_agent`` delegation tool, never as a top-level AgentApp.
"""

import logging

from app.database import SessionLocal

logger = logging.getLogger(__name__)


# Phase 9: power_user gets the FULL tool set; existing system agents
# stay focused but get a few new tools that make sense for their role.
# Names that are NOT in the tool_handlers registry (e.g. CRUD tools
# defined in agent_tools.py / _CRUD_DISPATCH) are kept here so the
# filter doesn't strip them — the schema is provided by
# _get_all_crud_schemas() in agent_prompts.py.
ALL_TOOL_NAMES = [
    # Core CRUD (system meta-agents)
    "create_agent", "update_agent", "list_market_agents", "list_tools",
    "create_skill", "update_skill", "search_skills",
    "create_automation", "update_automation", "execute_automation", "list_knowledge_bases",
    "ask_data_agent", "list_data_sources", "describe_schema",
    "execute_query", "answer_from_database",
    # Forecasting (Section 6)
    "forecast_discover", "forecast_run", "forecast_get",
    "forecast_accuracy", "forecast_rules", "forecast_report", "forecast_ppt",
    "ask_forecast_agent", "ask_report_agent",
    # EDIA pipeline delegation (Ecisco BI project)
    "ask_orchestrator", "ask_perception", "ask_rag_research",
    "ask_diagnosis", "ask_pricing",
    # Web
    "web_search", "web_extract", "x_search", "url_safety",
    # Memory / planning
    "memory", "todo", "kanban", "session_search", "checkpoint_manager",
    # NOTE: "interrupt" is deliberately omitted. The interrupt flag is not
    # polled by the v3 loop (nothing calls is_interrupted()); the LLM-facing
    # tool is dead code that weak models poll every step ("interrupt(action=
    # check)"), burning the tool budget. See interrupt_tool.py (registered
    # enabled_by_default=False, kept callable for explicit re-enable).
    "clarify", "slash_confirm", "approval",
    # Files / code
    "read_file", "write_file", "execute_code", "run_sandbox_skill",
    "fuzzy_match", "path_security", "patch_parser",
    "process_registry_list", "process_registry_tail", "process_registry_kill",
    "env_passthrough", "credential_files",
    # Media
    "image_generation", "video_generation", "tts", "transcription",
    "vision", "voice_mode",
    # Browser (CLI-backed)
    "agent_browser", "computer_use",
    # LLM
    "openrouter", "xai_http", "yuanbao", "mixture_of_agents",
    "delegate_task",
    # Skills
    "skills", "skills_hub", "skills_sync", "skills_guard", "skill_manager",
    "skill_provenance", "skill_usage", "Skill",
    # Artifacts
    "create_artifact", "edit_artifact", "load_skill_body",
    # Dashboards — full-stack real-time pipeline (primary,
    # FULLSTACK_DASHBOARD_ENABLED; handlers registered in dashboard_tools.py)
    "create_fullstack_dashboard", "update_fullstack_dashboard",
    "revert_fullstack_dashboard", "list_fullstack_dashboards",
    # Dashboards — legacy rollback pipeline (LEGACY_DASHBOARD_ENABLED)
    "create_dashboard", "update_dashboard", "undo_dashboard_edit",
    # Companion design intelligence for professional dashboards / visual artifacts
    "uiux_search", "uiux_design_system",
    # Security
    "osv_check", "tirith_security",
    # Communication
    "discord", "feishu_doc", "feishu_drive", "send_message",
    "homeassistant", "microsoft_graph", "microsoft_graph_auth",
    # MCP
    "mcp", "mcp_oauth", "mcp_oauth_manager",
    # Admin
    "update_env_config", "docker_compose_restart", "cronjob",
]

# 2026-07-28: Skill Agent must not call `create_artifact` to produce
# sample/demo artifacts after creating a skill. Same as ALL_TOOL_NAMES
# but with `create_artifact` removed. See spec
# docs/superpowers/specs/2026-07-28-skill-agent-no-create-artifact-design.md.
SKILL_AGENT_TOOL_NAMES = [t for t in ALL_TOOL_NAMES if t != "create_artifact"]


# Names that are NOT registered via ``registry.register()`` because they
# live in ``_CRUD_DISPATCH`` (``agent_tools.py``) and the static schema
# lists (``AGENT_BUILDER_TOOLS`` etc. in ``agent_prompts.py``). Their
# schemas are returned by ``_get_all_crud_schemas()``, so the LLM can
# call them — but ``_tools_in_registry`` must NOT strip them out,
# otherwise the system meta-agents lose the ability to create / update
# the very records they manage (the agent_builder bug: the LLM had
# ``skills``/``skills_hub`` but no ``create_agent``).
# Kept in sync with ``_CRUD_DISPATCH.keys()`` in ``agent_tools.py``.
_CRUD_TOOL_NAMES = frozenset({
    "create_agent", "update_agent",
    "list_tools", "list_market_agents",
    "create_skill", "update_skill", "search_skills",
    "create_automation", "update_automation",
    "list_knowledge_bases",
})


# Mirrors seed.py tool_config definitions so the behaviour stays in sync.
# Phase 9: each system agent's enabled_tools is now derived to be
# the intersection of (its focused tool set) ∩ (registered tool names),
# so missing modules don't break seeding. CR DU tools are kept even
# when not in the registry (they are dispatched via _CRUD_DISPATCH and
# their schemas come from _get_all_crud_schemas()).
def _tools_in_registry(registry, names):
    if registry is None:
        # Preserve order while deduplicating
        return list(dict.fromkeys(names))
    available = set(registry.list_available()) | _CRUD_TOOL_NAMES
    return [n for n in dict.fromkeys(names) if n in available]


def _build_system_agent_configs(registry=None) -> list[dict]:
    """Build the system-agent configs at import time.

    Tools are filtered through the live registry so seeding always
    succeeds even if some optional modules failed to import.
    """
    # Baseline Layer 3 Harness Agent fields shared by all system agents.
    # Each agent also gets its own fine-tuned manifest_json below.
    _BASE_HARNESS = {
        "trace_enabled": True,
        "log_level": "info",
        "memory_scope": "app_shared",
        "data_bindings": [],
        "skill_bindings": [],
        "output_contract": {
            "allowed_artifact_types": ["markdown", "json", "csv", "text", "docx", "pdf", "pptx", "html"],
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

    configs = [
        {
            "name": "agent_builder",
            "description": "Builds and configures new AI agents",
            "project": "global",
            "capabilities": ["agent_creation", "configuration"],
            "model": "gpt-4o-mini",
            "agent_type": "sequential",
            "topology": "standalone",
            "status": "active",
            "tool_config": {
                "enabled_tools": _tools_in_registry(registry, [
                    "create_agent", "update_agent",
                    "list_tools", "list_market_agents",
                    # Add new tools that are useful for the agent builder
                    "skills", "skills_hub", "skill_manager", "skills_guard",
                    "osv_check", "tirith_security",
                    # Web collection + dynamic skill discovery
                    "web_search", "web_extract", "Skill",
                    "create_artifact",
                ]),
            },
            **_BASE_HARNESS,
            "manifest_json": {
                "agent_name": "agent_builder",
                "version": "1.0.0",
                "mission": "Build and configure enterprise-grade AI agents through conversational requirements gathering",
                "task_scope": ["agent_creation", "agent_configuration", "skill_discovery"],
                "boundaries": {
                    "allowed": ["create_agent", "update_agent", "list_tools", "list_market_agents", "skills", "skills_hub", "create_artifact"],
                    "forbidden": ["delete_agent", "access_user_data", "call_user_agent_tools"],
                },
                "risk_tier": "medium",
                "created_by": "system",
            },
            "policy_profile": {
                "risk_tier": "medium",
                "requires_confirmation": True,
                "max_concurrent_calls": 3,
                "rate_limit_per_minute": 30,
                "allowed_domains": [],
                "retention_days": 30,
            },
        },
        {
            "name": "skill_agent",
            "description": "Creates and manages skills/tools",
            "project": "global",
            "capabilities": ["skill_creation", "tool_management"],
            "model": "gpt-4o",
            "agent_type": "sequential",
            "topology": "standalone",
            "status": "active",
            "tool_config": {
                # 2026-07-28: skill_agent is a full zhanlu agent specialized
                # for skill creation. Uses SKILL_AGENT_TOOL_NAMES (= ALL_TOOL_NAMES
                # minus create_artifact) so the model can't produce stray
                # sample/demo artifacts after creating a skill. See spec
                # docs/superpowers/specs/2026-07-28-skill-agent-no-create-artifact-design.md.
                "enabled_tools": _tools_in_registry(registry, SKILL_AGENT_TOOL_NAMES),
            },
            **_BASE_HARNESS,
            "manifest_json": {
                "agent_name": "skill_agent",
                "version": "1.0.0",
                "mission": "Create, discover, and manage reusable skill methodology documents for agents",
                "task_scope": ["skill_creation", "skill_discovery", "skill_management"],
                "boundaries": {
                    "allowed": ["create_skill", "update_skill", "search_skills", "list_tools"],
                    "forbidden": ["delete_skill", "modify_system_skills"],
                },
                "risk_tier": "low",
                "created_by": "system",
            },
            "policy_profile": {
                "risk_tier": "low",
                "requires_confirmation": False,
                "max_concurrent_calls": 5,
                "rate_limit_per_minute": 60,
                "allowed_domains": [],
                "retention_days": 30,
            },
        },
        {
            "name": "automation_agent",
            "description": "Creates automation tasks and schedules",
            "project": "global",
            "capabilities": ["automation", "scheduling"],
            "model": "gpt-4o-mini",
            "agent_type": "sequential",
            "topology": "standalone",
            "status": "active",
            "tool_config": {
                "enabled_tools": _tools_in_registry(registry, [
                    "create_automation", "update_automation",
                    "execute_automation", "list_knowledge_bases",
                    "list_data_sources", "clarify",
                    "cronjob", "send_message",
                    # Web collection + dynamic skill discovery
                    "web_search", "web_extract", "Skill",
                    "create_artifact",
                    # General assistant surface (deduped — skip tools already above)
                    "url_safety", "x_search",
                    "agent_browser",
                    "memory", "todo", "kanban", "session_search",
                    "read_file", "write_file", "image_generation",
                    "execute_code", "delegate_task", "ask_data_agent",
                    "run_sandbox_skill", "fuzzy_match", "patch_parser",
                    "process_registry_list", "process_registry_tail",
                    "process_registry_kill",
                    "slash_confirm", "approval",
                    "checkpoint_manager", "osv_check", "tirith_security",
                    "skills", "skills_hub", "skills_guard",
                    "skill_provenance", "skill_usage", "load_skill_body",
                    "update_env_config", "docker_compose_restart",
                ]),
            },
            **_BASE_HARNESS,
            "manifest_json": {
                "agent_name": "automation_agent",
                "version": "1.1.0",
                "mission": "Own the full automation lifecycle in chat: create tasks, fix/update existing ones (bind data sources, adjust schedule/prompt), trigger runs, and report run status",
                "task_scope": [
                    "automation_creation", "automation_fix", "automation_run",
                    "schedule_management", "data_source_binding",
                ],
                "boundaries": {
                    "allowed": [
                        "create_automation", "update_automation", "execute_automation",
                        "list_knowledge_bases", "list_data_sources", "clarify",
                        "cronjob", "create_artifact",
                    ],
                    "forbidden": ["delete_automation", "access_user_data", "report_generation", "data_analysis"],
                },
                "risk_tier": "medium",
                "created_by": "system",
            },
            "policy_profile": {
                "risk_tier": "medium",
                "requires_confirmation": True,
                "max_concurrent_calls": 3,
                "rate_limit_per_minute": 20,
                "allowed_domains": [],
                "retention_days": 30,
            },
            # Hidden from the agent picker, My Space, and the active-
            # agent chip — same as ecisco_bi_assistant. Chats that
            # originate from the Automation dialog / draft card auto-
            # bind to this agent via Chat.jsx's isAutomationOrigin
            # check; other chats keep their current agent.
            "is_system": True,
        },
        {
            "name": "general_assistant",
            "description": (
                "A versatile AI agent with web search, browser automation, "
                "memory, code execution, file operations, image generation, "
                "task delegation, dynamic skill discovery, and "
                "self-configuring tools (handles missing config "
                "conversationally)."
            ),
            "project": "global",
            "capabilities": [
                "web_search", "memory", "todo", "code_execution",
                "file_operations", "image_generation", "delegation",
                "missing_config_handling",
            ],
            "model": "gpt-4o",
            "agent_type": "sequential",
            "topology": "standalone",
            "status": "active",
            "tool_config": {
                "enabled_tools": _tools_in_registry(registry, [
                    "web_search", "web_extract", "url_safety", "x_search",
                    "agent_browser",
                    "memory", "todo", "kanban", "session_search",
                    "read_file", "write_file", "image_generation",
                    "execute_code", "delegate_task", "ask_data_agent",
                    "run_sandbox_skill", "fuzzy_match", "patch_parser",
                    "process_registry_list", "process_registry_tail",
                    "process_registry_kill",
                    "clarify", "slash_confirm", "approval",
                    "checkpoint_manager", "osv_check", "tirith_security",
                    "skills", "skills_hub", "skills_guard",
                    "skill_provenance", "skill_usage", "Skill", "load_skill_body",
                    "update_env_config", "docker_compose_restart",
                    "create_artifact",
                    "execute_automation",  # chat can trigger user's own tasks
                ]),
            },
            **_BASE_HARNESS,
            "manifest_json": {
                "agent_name": "general_assistant",
                "version": "1.0.0",
                "mission": "Serve as a versatile AI assistant with the full zhanlu toolset for general-purpose tasks",
                "task_scope": ["web_search", "memory", "code_execution", "file_operations", "delegation"],
                "boundaries": {
                    "allowed": ["web_search", "memory", "code_execution", "file_operations", "delegate_task", "create_artifact"],
                    "forbidden": ["destructive_ops", "impersonation", "production_mutation"],
                },
                "risk_tier": "low",
                "created_by": "system",
            },
            "policy_profile": {
                "risk_tier": "low",
                "requires_confirmation": False,
                "max_concurrent_calls": 5,
                "rate_limit_per_minute": 60,
                "allowed_domains": [],
                "retention_days": 30,
            },
        },
        {
            "name": "power_user",
            "description": (
                "A full-capability agent with every tool registered in "
                "the zhanlu toolset: web, media, browser, communication, "
                "MCP, LLM routing, skills, security, and admin. Use for "
                "power-user workflows where the full tool surface is "
                "expected."
            ),
            "project": "global",
            "capabilities": [
                "everything", "all_tools", "self_configuring",
            ],
            "model": "gpt-4o",
            "agent_type": "sequential",
            "topology": "standalone",
            "status": "active",
            "tool_config": {
                "enabled_tools": _tools_in_registry(registry, ALL_TOOL_NAMES),
            },
            **_BASE_HARNESS,
            "manifest_json": {
                "agent_name": "power_user",
                "version": "1.0.0",
                "mission": "Execute high-complexity, multi-system workflows using the complete zhanlu toolset",
                "task_scope": ("web_search", "memory", "code_execution", "delegation", "admin"),
                "boundaries": {
                    "allowed": ["all_registered_tools"],
                    "forbidden": ["none"],
                },
                "risk_tier": "high",
                "created_by": "system",
            },
            "policy_profile": {
                "risk_tier": "high",
                "requires_confirmation": True,
                "max_concurrent_calls": 10,
                "rate_limit_per_minute": 100,
                "allowed_domains": [],
                "retention_days": 30,
            },
        },
        # ── Ecisco BI Assistant Agent ────────────────────────────────────
        # Default agent for the Ecisco BI project.  Replaces the old
        # ``orchestrator_agent`` and ``ecisco_bi`` single-agent configs.
        # Covers the full EDIA pipeline: perception, RAG research,
        # diagnosis, forecast, pricing, decision, knowledge graph,
        # weekly report, ERP writeback, and intelligence layers.
        # Uses hybrid routing: direct Python calls for fast paths,
        # delegation tools for complex reasoning.
        # Auto-selected when the user is in the Ecisco BI project.
        {
            "name": "ecisco_bi_assistant",
            "description": (
                "Ecisco BI Assistant — default general-purpose agent for "
                "the Ecisco BI C5/C9 petrochemical domain intelligence "
                "platform. Covers market perception, diagnosis, forecasting, "
                "pricing, decision, knowledge graph, weekly reports, ERP "
                "writeback, and intelligence layers. Uses hybrid routing: "
                "direct Python service calls for fast paths, delegation "
                "tools for complex multi-step reasoning."
            ),
            "project": "Ecisco BI",
            "capabilities": [
                "orchestration", "perception", "diagnosis", "forecasting",
                "pricing", "decision", "knowledge_graph", "reporting",
                "weekly_report", "erp_writeback", "data_query",
                "intelligence", "web_research",
            ],
            "model": "gpt-4o",
            "agent_type": "sequential",
            "topology": "standalone",
            "status": "active",
            "tool_config": {
                "enabled_tools": _tools_in_registry(registry, list(dict.fromkeys(
                    ALL_TOOL_NAMES +
                    # EDIA pipeline delegation tools (14 ask_* tools)
                    [
                        "ask_perception", "ask_rag_research", "ask_diagnosis",
                        "ask_forecast_agent", "ask_pricing", "ask_data_agent",
                        "ask_report_agent", "ask_intelligence",
                        "ask_knowledge_graph", "ask_decision", "ask_macro_override",
                        "ask_erp_kpi", "ask_erp_writeback",
                        # Direct forecast read path
                        "forecast_brief",
                    ]
                ))),
            },
            **_BASE_HARNESS,
            "manifest_json": {
                "agent_name": "ecisco_bi_assistant",
                "version": "1.1.0",
                "mission": "Serve as the default Ecisco BI assistant for C5/C9 petrochemical domain intelligence, covering the full EDIA 6-stage pipeline",
                "task_scope": [
                    "market_analysis", "forecasting", "pricing",
                    "decision", "diagnosis", "weekly_report",
                    "knowledge_graph", "erp_writeback", "data_analysis",
                    "dashboard", "data_query", "automation",
                ],
                "boundaries": {
                    "allowed": ["all_registered_tools"],
                    "forbidden": [],
                },
                "risk_tier": "low",
                "created_by": "system",
            },
            "policy_profile": {
                "risk_tier": "low",
                "requires_confirmation": False,
                "max_concurrent_calls": 5,
                "rate_limit_per_minute": 30,
                "allowed_domains": [],
                "retention_days": 30,
            },
            # ecisco_bi_assistant is a silent platform system agent.
            # Frontend/src/pages/Chat.jsx auto-selects it when the user
            # opens a chat in the "Ecisco BI" project, so users never need
            # to see it. Hiding it via is_system=True means
            # filterUserAgents() drops it from My Space, the agent picker,
            # the active-agent chip, and the Company Agents section. The
            # flag is also enforced at the API layer (see
            # entity_service.soft_delete_record) so it cannot be removed
            # through any HTTP path. ensure_system_agents() back-fills
            # this flag on every backend startup.
            "is_system": True,
        },
    ]

    return configs


def ensure_system_agents(db=None) -> None:
    """Create the DB-backed system agents if they don't exist; refresh
    tool_config on existing rows to pick up new tools.

    Idempotent: queries by name first, only inserts when missing.
    For existing rows, refreshes the enabled_tools list and the
    description so newly-registered tools appear in the agent's
    palette without requiring a re-seed.

    Also stamps ``is_system=True`` on every system-agent row so the
    frontend can hide them from user-facing agent lists (My Space,
    the agent picker, the active-agent chip) while the runtime still
    uses them — in particular ``general_assistant`` is auto-selected
    silently for any chat with no user-picked agent.

    Safe to call on every startup.
    """
    from app.services.tool_registry import registry
    from app.models.agent_app import AgentApp

    configs = _build_system_agent_configs(registry)

    own_session = db is None
    if own_session:
        db = SessionLocal()

    try:
        # Runtime schema-ensure: add the 'role' column to agent_apps if it
        # is missing, mirroring the _ensure_schema pattern in
        # automation_dispatcher. Works on SQLite + Postgres.
        try:
            from sqlalchemy import inspect as sa_inspect, text
            inspector = sa_inspect(db.bind)
            existing_cols = {c["name"] for c in inspector.get_columns("agent_apps")}
            if "role" not in existing_cols:
                db.execute(text("ALTER TABLE agent_apps ADD COLUMN role VARCHAR(64)"))
                db.commit()
                logger.info("Added 'role' column to agent_apps")
            existing_ix = {ix["name"] for ix in inspector.get_indexes("agent_apps")}
            if "ix_agent_apps_role" not in existing_ix:
                try:
                    db.execute(text("CREATE INDEX ix_agent_apps_role ON agent_apps (role)"))
                    db.commit()
                except Exception:
                    db.rollback()  # index may already exist; non-fatal
        except Exception as _schema_err:
            db.rollback()
            logger.warning("ensure_system_agents: role-column ensure failed (non-fatal): %s", _schema_err)

        # Runtime schema-ensure: add the 'is_system' column to projects if
        # it is missing.  Mirrors the agent_apps 'role' migration above
        # and the _ensure_schema pattern in automation_dispatcher.  Works
        # on SQLite + Postgres.
        try:
            from sqlalchemy import inspect as sa_inspect, text
            inspector = sa_inspect(db.bind)
            existing_proj_cols = {
                c["name"] for c in inspector.get_columns("projects")
            }
            if "is_system" not in existing_proj_cols:
                # PostgreSQL rejects DEFAULT 0 for BOOLEAN — use FALSE.
                # Works on SQLite too (SQLite coerces 0→FALSE).
                db.execute(text(
                    "ALTER TABLE projects ADD COLUMN is_system BOOLEAN "
                    "NOT NULL DEFAULT FALSE"
                ))
                db.commit()
                logger.info("Added 'is_system' column to projects")
        except Exception as _proj_schema_err:
            db.rollback()
            logger.warning(
                "ensure_system_agents: projects.is_system column ensure failed "
                "(non-fatal): %s", _proj_schema_err,
            )

        created = []
        updated = []
        promoted_to_system = []
        for cfg in configs:
            existing = db.query(AgentApp).filter(
                AgentApp.name == cfg["name"],
                AgentApp.is_deleted == False,
            ).first()
            # Pop is_system from the config so we don't pass it twice
            # to AgentApp(**cfg, is_system=...).
            cfg_is_system = cfg.pop("is_system", True)
            if not existing:
                agent = AgentApp(**cfg, is_system=cfg_is_system)
                db.add(agent)
                created.append(cfg["name"])
                continue
            # Refresh tool_config + description + manifest_json on existing
            # rows so newly-registered tools appear without a manual re-seed,
            # and the manifest boundaries stay in sync with enabled_tools
            # (otherwise the descriptive manifest drifts from the enforced
            # tool_config, e.g. when wiring a new tool like forecast_brief).
            # Also sync knowledge_bases and data_bindings to prevent stale
            # bindings from persisting after UI edits (e.g. general_assistant
            # leaking cross-project data sources when no project is selected).
            existing.tool_config = cfg["tool_config"]
            if cfg.get("description"):
                existing.description = cfg["description"]
            if cfg.get("manifest_json"):
                existing.manifest_json = cfg["manifest_json"]
            if cfg.get("knowledge_bases") is not None:
                existing.knowledge_bases = cfg["knowledge_bases"]
            if cfg.get("data_bindings") is not None:
                existing.data_bindings = cfg["data_bindings"]
            # Stamp is_system=True even on rows that pre-date the
            # ``is_system`` column — they ARE system agents, even if
            # the column was False when the row was originally written
            # (e.g. via an older migration path). Without this backfill
            # the frontend would show legacy general_assistant rows in
            # the user agent list.
            # Exception: configs that explicitly set is_system=False
            # (e.g. orchestrator_agent) are user-facing and must stay visible.
            if not existing.is_system and cfg_is_system:
                existing.is_system = True
                promoted_to_system.append(cfg["name"])
            db.add(existing)
            updated.append(cfg["name"])
        if created or updated:
            db.commit()
            if created:
                logger.info("System agents created: %s", created)
            if promoted_to_system:
                logger.info(
                    "Promoted pre-existing rows to is_system=True: %s",
                    promoted_to_system,
                )
            if updated:
                logger.info("System agents tool_config refreshed: %s", updated)
        else:
            logger.debug("System agents already present — nothing to do")

        # ── Ensure the Ecisco BI project exists and the ecisco_bi_assistant
        # is linked to it via ProjectAgent.  This makes the EDIA pipeline
        # available on every deployment without requiring a manual re-seed.
        #
        # The Ecisco BI project is a *system* project (is_system=True) and
        # has created_by_id=NULL, so the entity_service.py filter
        # ``(created_by_id == owner_id) OR created_by_id IS NULL`` includes
        # it in every user's "My Space → Projects" listing.
        try:
            from app.models.project import Project
            from app.models.project_agent import ProjectAgent

            # ── One-time rename: orchestrator_agent → ecisco_bi_assistant ──
            # Idempotent — renames any remaining old rows on each startup.
            # If both old and new rows exist, deletes the old one (dedup).
            old_agent = db.query(AgentApp).filter(
                AgentApp.name == "orchestrator_agent",
                AgentApp.is_deleted == False,
            ).first()
            new_agent_check = db.query(AgentApp).filter(
                AgentApp.name == "ecisco_bi_assistant",
                AgentApp.is_deleted == False,
            ).first()
            if old_agent and new_agent_check:
                # Both exist — soft-delete the old row, keep the new one.
                old_agent.is_deleted = True
                db.commit()
                logger.info(
                    "Dedup: soft-deleted leftover orchestrator_agent row "
                    "(agent_id=%s, newer ecisco_bi_assistant at agent_id=%s)",
                    old_agent.id, new_agent_check.id,
                )
            elif old_agent:
                old_agent.name = "ecisco_bi_assistant"
                db.commit()
                logger.info(
                    "Renamed orchestrator_agent → ecisco_bi_assistant "
                    "(agent_id=%s)", old_agent.id,
                )

            ecisco_project = db.query(Project).filter(
                Project.name == "Ecisco BI",
                Project.is_deleted == False,
            ).first()
            if not ecisco_project:
                ecisco_project = Project(
                    name="Ecisco BI",
                    description=(
                        "Ecisco BI — EDIA pipeline for C5/C9 petrochemical "
                        "domain intelligence. Contains Ecisco BI Assistant, "
                        "Perception, RAG Research, Diagnosis, Forecasting, "
                        "Pricing, Decision, Knowledge Graph, Weekly Report, "
                        "ERP Writeback, and Report Generation agents."
                    ),
                    color="#8B5CF6",
                    status="active",
                    is_system=True,
                    # created_by_id left NULL so the entity_service filter
                    # surfaces this project in every user's listing.
                )
                db.add(ecisco_project)
                db.commit()
                db.refresh(ecisco_project)
                logger.info("Created Ecisco BI system project (id=%s)", ecisco_project.id)
            else:
                # Promote any pre-existing Ecisco BI row to system status
                # so it surfaces in every user's listing.
                if not getattr(ecisco_project, "is_system", False):
                    ecisco_project.is_system = True
                if ecisco_project.created_by_id is not None:
                    ecisco_project.created_by_id = None
                # Always force status='active' on the system project — a
                # stale row from before this migration may have been
                # archived by a user, which hides it from the default
                # "active" filter in the My Space → Projects view.
                if ecisco_project.status != "active":
                    logger.info(
                        "Un-archiving Ecisco BI project (was status=%r)",
                        ecisco_project.status,
                    )
                    ecisco_project.status = "active"
                db.commit()
                logger.info("Promoted Ecisco BI project to is_system=True (id=%s)", ecisco_project.id)

            # Link the ecisco_bi_assistant AgentApp to the Ecisco BI project
            bio_agent = db.query(AgentApp).filter(
                AgentApp.name == "ecisco_bi_assistant",
                AgentApp.is_deleted == False,
            ).first()
            if bio_agent:
                # Sync the legacy string field too
                bio_agent.project = "Ecisco BI"
                bio_agent.project_id = ecisco_project.id

                # Ensure the ProjectAgent many-to-many row exists
                existing_pa = db.query(ProjectAgent).filter(
                    ProjectAgent.project_id == ecisco_project.id,
                    ProjectAgent.agent_id == bio_agent.id,
                ).first()
                if not existing_pa:
                    pa = ProjectAgent(
                        project_id=ecisco_project.id,
                        agent_id=bio_agent.id,
                        role="primary",
                    )
                    db.add(pa)
                    db.commit()
                    logger.info(
                        "Linked ecisco_bi_assistant to Ecisco BI project (agent_id=%s)",
                        bio_agent.id,
                    )
        except Exception as _proj_err:
            db.rollback()
            logger.warning("ensure_system_agents: Ecisco BI project ensure failed (non-fatal): %s", _proj_err)

        # Eager provisioning: ensure a runtime agent exists for every
        # (org, app) that already has automation tasks, and rebind any
        # tasks with a NULL/invalid agent_id. Idempotent + safe on every
        # startup.
        try:
            from app.services.automation_runtime import backfill_automation_runtime_agents
            backfill_automation_runtime_agents(db)
        except Exception as _bf_err:
            db.rollback()
            logger.warning("ensure_system_agents: runtime backfill failed (non-fatal): %s", _bf_err)
    except Exception as e:
        db.rollback()
        logger.warning("ensure_system_agents failed (non-fatal): %s", e)
    finally:
        if own_session:
            db.close()
