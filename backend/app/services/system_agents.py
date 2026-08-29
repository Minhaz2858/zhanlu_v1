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
    # Institutional-grade research-analyst pipeline (2026-08-25).
    # Universal directive tells the LLM to call comprehensive_data
    # BEFORE create_artifact, but it can only do that if the tool
    # is actually in the agent's toolset. Adding both the new name
    # AND the legacy alias covers any model-side confusion.
    "comprehensive_data",
    "collect_enterprise_data",
    # Phase 9: multi-agent swarm (team creation / spawning / messaging /
    # orchestration). Registered in swarm_tools.py; gated by the
    # AGENT_HARNESS_ENABLED runtime switch for the harness execution path.
    "swarm_create_team", "swarm_spawn_agent",
    "swarm_send_message", "swarm_get_messages",
    "swarm_list_teams", "swarm_scratch_set", "swarm_scratch_get",
    "swarm_orchestrate",
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
            # agent chip. Chats that
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
                    # Phase 9: multi-agent swarm — the chat agent can create
                    # teams, spawn sub-agents, exchange messages, and
                    # orchestrate parallel workers (AGENT_HARNESS_ENABLED).
                    "swarm_create_team", "swarm_spawn_agent",
                    "swarm_send_message", "swarm_get_messages",
                    "swarm_list_teams", "swarm_scratch_set", "swarm_scratch_get",
                    "swarm_orchestrate",
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

        # ── Purge removed default BI agent (2026-08-27) ────────────────
        # The enterprise_bi_assistant / ecisco_bi_assistant /
        # orchestrator_agent default agents were REMOVED from the platform
        # (industry-specific default). Any rows left in existing DBs
        # are hard-deleted here (plus their ProjectAgent links and the
        # system "Enterprise BI" project that only existed to host them).
        # Idempotent — runs on every startup.
        try:
            from app.models.project import Project
            from app.models.project_agent import ProjectAgent

            _REMOVED_AGENT_NAMES = (
                "ecisco_bi_assistant",
                "enterprise_bi_assistant",
                "orchestrator_agent",
            )
            removed_agent_ids: list[str] = []
            for _name in _REMOVED_AGENT_NAMES:
                _rows = db.query(AgentApp).filter(
                    AgentApp.name == _name,
                    AgentApp.is_deleted == False,  # noqa: E712
                ).all()
                for _row in _rows:
                    removed_agent_ids.append(str(_row.id))
                    db.delete(_row)
            if removed_agent_ids:
                # Remove ProjectAgent links pointing at the deleted agents.
                db.query(ProjectAgent).filter(
                    ProjectAgent.agent_id.in_(removed_agent_ids)
                ).delete(synchronize_session=False)
                db.commit()
                logger.info(
                    "Purged removed default BI agents (hard delete): %s",
                    removed_agent_ids,
                )

            # Delete the system projects that only existed to host the
            # removed default agents (is_system=True, created_by_id NULL).
            # Legacy names: "Enterprise BI", "Ecisco BI".
            # Cascade: dependent rows reference projects via FK, so delete
            # them in dependency order first (hard delete, no orphans).
            _bi_projects = db.query(Project).filter(
                Project.name.in_(["Enterprise BI", "Ecisco BI"]),
                Project.is_deleted == False,  # noqa: E712
                Project.is_system == True,  # noqa: E712
            ).all()
            for _proj in _bi_projects:
                from sqlalchemy import text as _sql_text
                _pid = _proj.id
                # Hard-delete ALL rows that reference this project, in every
                # table that has a project_id column, plus the transitive
                # children (kb_table_meta/kb_column_meta, chat_messages,
                # dashboard_versions, automation_files/executions, agent
                # bindings) which reference the project indirectly. FK checks
                # are disabled for this transaction (SET LOCAL, auto-restored
                # on commit) so delete order cannot block the purge.
                db.execute(
                    _sql_text("SET LOCAL session_replication_role = 'replica'")
                )
                # Tables with a direct project_id column.
                _direct_tables = [
                    "agent_conversations", "chat_sessions", "automation_tasks",
                    "dashboards", "knowledge_bases", "project_agents",
                    "project_memories", "user_files", "agent_apps",
                ]
                _has_col = (
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name=:t "
                    "AND column_name='project_id'"
                )
                for _tbl in _direct_tables:
                    _n = db.execute(
                        _sql_text(_has_col), {"t": _tbl}
                    ).scalar()
                    if _n:
                        db.execute(
                            _sql_text(
                                f"DELETE FROM {_tbl} WHERE project_id = :pid"
                            ),
                            {"pid": _pid},
                        )
                # Transitive children (no direct project_id) — delete via
                # subquery from their parent. Missing tables are skipped.
                _transitive = [
                    (
                        "kb_column_meta",
                        "DELETE FROM kb_column_meta WHERE table_meta_id IN "
                        "(SELECT id FROM kb_table_meta WHERE kb_id IN "
                        "(SELECT id FROM knowledge_bases WHERE project_id = :pid))",
                    ),
                    (
                        "kb_table_relation",
                        "DELETE FROM kb_table_relation WHERE kb_id IN "
                        "(SELECT id FROM knowledge_bases WHERE project_id = :pid)",
                    ),
                    (
                        "kb_table_meta",
                        "DELETE FROM kb_table_meta WHERE kb_id IN "
                        "(SELECT id FROM knowledge_bases WHERE project_id = :pid)",
                    ),
                    (
                        "chat_messages",
                        "DELETE FROM chat_messages WHERE session_id IN "
                        "(SELECT id FROM chat_sessions WHERE project_id = :pid)",
                    ),
                    (
                        "dashboard_versions",
                        "DELETE FROM dashboard_versions WHERE dashboard_id IN "
                        "(SELECT id FROM dashboards WHERE project_id = :pid)",
                    ),
                    (
                        "automation_executions",
                        "DELETE FROM automation_executions WHERE automation_task_id IN "
                        "(SELECT id FROM automation_tasks WHERE project_id = :pid)",
                    ),
                    (
                        "automation_files",
                        "DELETE FROM automation_files WHERE automation_task_id IN "
                        "(SELECT id FROM automation_tasks WHERE project_id = :pid)",
                    ),
                    (
                        "agent_data_bindings",
                        "DELETE FROM agent_data_bindings WHERE agent_app_id IN "
                        "(SELECT id FROM agent_apps WHERE project_id = :pid)",
                    ),
                    (
                        "agent_skill_bindings",
                        "DELETE FROM agent_skill_bindings WHERE agent_app_id IN "
                        "(SELECT id FROM agent_apps WHERE project_id = :pid)",
                    ),
                ]
                _existing = {
                    row[0]
                    for row in db.execute(
                        _sql_text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = 'public'"
                        )
                    ).fetchall()
                }
                for _tbl, _sql in _transitive:
                    if _tbl in _existing:
                        db.execute(_sql_text(_sql), {"pid": _pid})
                db.delete(_proj)
                db.commit()
                logger.info(
                    "Purged legacy BI system project (id=%s) — hosted removed default agent",
                    _proj.id,
                )
        except Exception as _purge_err:
            db.rollback()
            logger.warning("ensure_system_agents: removed-agent purge failed (non-fatal): %s", _purge_err)

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
