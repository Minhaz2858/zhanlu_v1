"""FastAPI application entry point — Zhanlu Backend (Phase 1)."""

import os
import sys
from contextlib import asynccontextmanager

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import engine, Base
from app.models import (
    Organization, AppWorkspace,
    User,     Project, ChatSession, ChatMessage, AgentApp, KnowledgeBase,
    AutomationTask, AutomationExecution, AutomationFile,
    Tool, UserFile, Report, DecisionFlow, MarketAgent,
    McpServer, UserSetting, AgentConversation,
    AnalyticsEvent, OtpCode, PasswordResetToken,
    AgentMemory, AgentTodo, WorkspaceSetting,
    MarketplaceSkill, MarketplaceRating,
    ProjectMemory, ProjectAgent,
    AgentRun,
    AgentRunStep,
    LlmModel,
)

# Import tool_handlers to register all 8 new capability tools in the registry
import app.services.tool_handlers  # noqa: F401
from app.routers.entities import register_entity_router, register_project_kb_router
from app.routers.auth import router as auth_router
from app.routers.public import router as public_router
from app.routers.diagnostics import router as diagnostics_router
from app.routers.functions import router as functions_router
from app.routers.integrations import router as integrations_router
from app.routers.agents import router as agents_router
from app.routers.analytics import router as analytics_router
from app.routers.openharness import router as openharness_router
from app.routers.artifacts import router as artifacts_router
from app.routers.sandbox import router as sandbox_router
from app.routers.data_snapshots import router as data_snapshots_router
from app.routers.agent_runs import router as agent_runs_router  # P0-3
from app.routers.executions import router as executions_router
from app.routers.agent_studio import router as agent_studio_router
from app.routers.skill_studio import router as skill_studio_router
from app.routers.governance import router as governance_router
from app.routers.workspace_settings import router as workspace_settings_router
from app.routers.marketplace import router as marketplace_router
from app.routers.mcp import router as mcp_router
from app.routers.nl2sql import router as nl2sql_router
from app.routers.automation_api import router as automation_api_router
from app.routers.knowledge_bases import router as knowledge_bases_router
from app.routers.dashboards import router as dashboards_router
from app.routers.hooks import router as hooks_router
from app.routers.admin_users import router as admin_users_router
from app.routers.admin_evals import router as admin_evals_router
from app.routers.admin_invocations import router as admin_invocations_router
from app.routers.resource_shares import router as resource_shares_router
from app.routers.access_policies import router as access_policies_router
from app.routers.chat_tools import router as chat_tools_router
from app.routers.chat_tools import public_router as chat_share_public_router
from app.routers.llm import router as llm_router
from app.routers.app_logs import router as app_logs_router
from app.routers.project_memories import router as project_memories_router  # project-scoped memory review/edit
from app.services.project_knowledge.router import router as project_knowledge_router


ENTITY_MODELS = {
    "Organization": Organization,
    "AppWorkspace": AppWorkspace,
    "User": User,
    "Project": Project,
    "ProjectMemory": ProjectMemory,
    "ProjectAgent": ProjectAgent,
    "ChatSession": ChatSession,
    "ChatMessage": ChatMessage,
    "AgentApp": AgentApp,
    "KnowledgeBase": KnowledgeBase,
    "AutomationTask": AutomationTask,
    "AutomationExecution": AutomationExecution,
    "AutomationFile": AutomationFile,
    "Tool": Tool,
    "UserFile": UserFile,
    "Report": Report,
    "DecisionFlow": DecisionFlow,
    "MarketAgent": MarketAgent,
    "McpServer": McpServer,
    "UserSetting": UserSetting,
    "AgentConversation": AgentConversation,
    "AnalyticsEvent": AnalyticsEvent,
    "OtpCode": OtpCode,
    "PasswordResetToken": PasswordResetToken,
    "WorkspaceSetting": WorkspaceSetting,
    "AgentRun": AgentRun,
    "AgentRunStep": AgentRunStep,
}


def _cors_origins() -> list[str]:
    """Build the allowed-origin list from settings.

    Combines the configured ``FRONTEND_URL``, any extra origins in
    ``CORS_ORIGINS`` (comma-separated, e.g. a production domain), and the
    localhost dev origins so local development keeps working out of the box.
    Duplicates are removed while preserving order.
    """
    origins = [
        settings.FRONTEND_URL,
        "http://localhost:5157",
        "http://127.0.0.1:5157",
        "http://localhost:5002",
    ]
    extra = getattr(settings, "CORS_ORIGINS", "") or ""
    for raw in extra.split(","):
        o = raw.strip()
        if o and o not in origins:
            origins.append(o)
    # De-duplicate (case-sensitive) while keeping order.
    seen = set()
    unique = []
    for o in origins:
        if o and o not in seen:
            seen.add(o)
            unique.append(o)
    return unique


def _is_postgres_url(url: str) -> bool:
    """True for postgres://, postgresql://, postgresql+psycopg2://, etc."""
    if not url:
        return False
    return url.split("://", 1)[0].split("+", 1)[0] in ("postgres", "postgresql")


def _run_postgres_auto_migrations() -> None:
    """Apply ``specific_migrations`` on Postgres (no PRAGMA available).

    ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` is a no-op when the column
    already exists, so this is safe to re-run on every startup.  This runs
    BEFORE the SQLite path (which would crash trying to ``sqlite3.connect``
    a Postgres URL).
    """
    import logging

    logger = logging.getLogger(__name__)
    from sqlalchemy import create_engine, text

    specific_migrations = _specific_migrations()
    engine = create_engine(settings.DATABASE_URL)
    with engine.begin() as conn:
        for table_name, columns in specific_migrations.items():
            for col_name, col_type in columns:
                try:
                    # Postgres booleans need DEFAULT false, not DEFAULT 0
                    # (0 is an integer literal — DatatypeMismatch).  Also,
                    # when the column already exists, Postgres STILL
                    # validates the default type, so normalize up front.
                    if "BOOLEAN" in str(col_type).upper():
                        col_type = str(col_type).replace("DEFAULT 0", "DEFAULT false")
                    conn.execute(text(
                        f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {col_name} {col_type}"
                    ))
                    logger.info(
                        "Postgres auto-migration: ensured column %s.%s (%s)",
                        table_name, col_name, col_type,
                    )
                except Exception as e:
                    logger.warning(
                        "Postgres auto-migration failed for %s.%s: %s",
                        table_name, col_name, e,
                    )


def _specific_migrations() -> dict:
    """The shared specific_migrations contract (SQLite + Postgres use it)."""
    return {
        "agent_apps": [
            ("tool_config", "JSON"),
            ("manifest_json", "JSON"),
            ("data_bindings", "JSON"),
            ("skill_bindings", "JSON"),
            ("memory_scope", "VARCHAR(30) DEFAULT 'user_only'"),
            ("policy_profile", "JSON"),
            ("output_contract", "JSON"),
            ("evaluation_profile", "JSON"),
            ("project_id", "VARCHAR(36)"),
            ("resource_type", "VARCHAR(20) DEFAULT 'personal' NOT NULL"),
        ],
        "projects": [
            ("resource_type", "VARCHAR(20) DEFAULT 'personal' NOT NULL"),
        ],
        "resource_shares": [],  # table creation only — triggers schema check

        "agent_memories": [
            ("content_hash", "VARCHAR(64)"),
            ("importance", "INTEGER DEFAULT 0"),
            ("ttl_days", "INTEGER"),
            ("usage_count", "INTEGER DEFAULT 0"),
            # 2026-08-05 added `project_id` to the SQLAlchemy model for memory
            # scoping (Q2 2026 sales-report leak — see MEMORY), but no Alembic
            # migration was written; consolidation turns it into a 500. Mirror
            # it in the runtime auto-migrate list so SQLite + Postgres both
            # gain the column on next restart without needing alembic upgrade.
            ("project_id", "VARCHAR(36)"),
            # 2026-08-27 pinned memory (interactive review/edit UI).
            ("pinned", "BOOLEAN DEFAULT 0"),
        ],
        "agent_run_steps": [
            # 2026-08-27 run-timeline observability (tokens + outcome).
            ("prompt_tokens", "INTEGER"),
            ("completion_tokens", "INTEGER"),
            ("total_tokens", "INTEGER"),
            ("status", "VARCHAR(20)"),
            ("error", "TEXT"),
            ("retry_count", "INTEGER DEFAULT 0"),
        ],
        "knowledge_bases": [
            ("project_id", "VARCHAR(36)"),
        ],
        "user_files": [
            ("project_id", "VARCHAR(36)"),
        ],
        "automation_tasks": [
            ("project_id", "VARCHAR(36)"),
            ("prompt", "TEXT"),
            ("cron_expression", "VARCHAR(100)"),
            ("timezone", "VARCHAR(64) DEFAULT 'UTC'"),
            ("next_run_at", "DATETIME"),
            ("last_run_at", "DATETIME"),
            ("output_format", "VARCHAR(30) DEFAULT 'html'"),
            ("notify_chat", "VARCHAR(10) DEFAULT 'true'"),
            ("max_retries", "VARCHAR(10) DEFAULT '2'"),
            ("skip_confirmation", "VARCHAR(10) DEFAULT 'false'"),
            ("agent_id", "VARCHAR(36)"),
            # 2026-08-27 opt-in LLM-informed tick (smart scheduled runs).
            ("llm_informed_tick", "BOOLEAN DEFAULT 0"),
        ],
        "automation_executions": [
            # Phase 1 reliability columns. Also added portably at runtime
            # by automation_dispatcher._ensure_schema() (covers Postgres),
            # but mirrored here so SQLite deployments pick them up in the
            # existing startup migration pass.
            ("timeout_at", "DATETIME"),
            ("lease_owner", "VARCHAR(64)"),
            # Phase 2 live-run observability (Manus-style activity feed).
            ("activity_steps", "JSON"),
            ("current_phase", "VARCHAR(50)"),
        ],
        "chat_sessions": [
            ("project_id", "VARCHAR(36)"),
        ],
        "agent_conversations": [
            ("project_id", "VARCHAR(36)"),
            ("dashboard_id", "VARCHAR(36)"),
        ],
        "skill_sources": [
            # Visual branding for the source cards on the Browse Marketplace
            # tab. Both columns are nullable — the API and the UI fall back
            # to a neutral gray + the first letter of the name when missing.
            ("brand_color", "VARCHAR(7)"),
            ("icon_emoji", "VARCHAR(8)"),
        ],
    }


def _run_auto_migrations():
    """Add missing columns to existing tables on either SQLite or Postgres.

    Base.metadata.create_all() only creates new tables — it doesn't add
    columns to existing ones. This function detects missing columns via
    PRAGMA table_info() (SQLite) or information_schema.columns (Postgres)
    and adds them with ALTER TABLE ADD COLUMN.

    Phase 1 enhancement: dynamically adds ``org_id`` and ``app_id`` (the
    multi-tenant isolation wall) to every existing table that lacks them.

    Postgres support was added on 2026-07-29 because new ``specific_migrations``
    entries were silently being skipped against the production Postgres
    (the function used to bail out on the SQLite-only path). SQLite is still
    the primary target — Postgres branches are kept narrow and only run
    when ``settings.DATABASE_URL`` indicates a Postgres backend.
    """
    import sqlite3
    import logging

    logger = logging.getLogger(__name__)

    # Postgres deployment: run the Postgres-specific migrations FIRST (the
    # SQLite path below would crash on a Postgres URL — sqlite3.connect
    # cannot open a postgresql:// string).  Early return keeps the legacy
    # SQLite behavior untouched.  Note the URL may carry a dialect suffix
    # (postgresql+psycopg2://) so we match on the bare scheme prefix.
    if _is_postgres_url(settings.DATABASE_URL):
        _run_postgres_auto_migrations()
        return

    try:
        db_path = settings.DATABASE_URL.replace("sqlite:///", "")
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # --- Multi-tenant columns: add org_id / app_id to ALL tables ---
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
        )
        all_tables = [row[0] for row in cursor.fetchall()]

        tenant_columns = [
            ("org_id", "VARCHAR(36) DEFAULT 'default-org' NOT NULL"),
            ("app_id", "VARCHAR(36) DEFAULT 'default-app' NOT NULL"),
        ]

        for table_name in all_tables:
            cursor.execute(f"PRAGMA table_info({table_name})")
            existing_cols = {row[1] for row in cursor.fetchall()}

            for col_name, col_type in tenant_columns:
                if col_name not in existing_cols:
                    try:
                        cursor.execute(
                            f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}"
                        )
                        logger.info(
                            "Auto-migration: added column %s.%s", table_name, col_name
                        )
                    except sqlite3.OperationalError as e:
                        if "duplicate column" not in str(e).lower():
                            logger.warning(
                                "Auto-migration failed for %s.%s: %s",
                                table_name, col_name, e,
                            )

        # --- Specific column additions for older schemas ---
        specific_migrations = _specific_migrations()

        for table_name, columns in specific_migrations.items():
            try:
                cursor.execute(f"PRAGMA table_info({table_name})")
                existing_cols = {row[1] for row in cursor.fetchall()}
            except sqlite3.OperationalError:
                continue

            for col_name, col_type in columns:
                if col_name not in existing_cols:
                    try:
                        cursor.execute(
                            f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}"
                        )
                        logger.info(
                            "Auto-migration: added column %s.%s (%s)",
                            table_name, col_name, col_type,
                        )
                    except sqlite3.OperationalError as e:
                        if "duplicate column" not in str(e).lower():
                            logger.warning(
                                "Auto-migration failed for %s.%s: %s",
                                table_name, col_name, e,
                            )

        # (Postgres migrations now run via _run_postgres_auto_migrations()
        # before this SQLite path — see the early return above.  This path
        # is SQLite-only.)

        # --- Backfill project_id from existing project string fields ---
        # For any row whose `project` (legacy name string) is set but
        # `project_id` (new FK to projects.id) is null, look up the Project
        # record with matching name + org_id + app_id and copy its id.
        # Scoped to org_id + app_id to prevent cross-tenant collisions.
        # agent_conversations has no legacy `project` string field, so it's
        # omitted from this backfill.
        project_backfill_tables = [
            "agent_apps",
            "knowledge_bases",
            "user_files",
            "automation_tasks",
            "chat_sessions",
        ]
        for table_name in project_backfill_tables:
            try:
                # Match project name → project.id within same (org_id, app_id).
                # This is scoped per tenant so two orgs with the same
                # project name won't collide.
                cursor.execute(
                    f"""
                    UPDATE {table_name} AS t
                    SET project_id = (
                        SELECT p.id FROM projects p
                        WHERE p.name = t.project
                          AND p.org_id = t.org_id
                          AND p.app_id = t.app_id
                          AND p.is_deleted = 0
                        LIMIT 1
                    )
                    WHERE t.project IS NOT NULL
                      AND t.project_id IS NULL
                      AND EXISTS (
                          SELECT 1 FROM projects p
                          WHERE p.name = t.project
                            AND p.org_id = t.org_id
                            AND p.app_id = t.app_id
                      )
                    """
                )
                if cursor.rowcount > 0:
                    logger.info(
                        "Auto-migration: backfilled %d row(s) of %s.project_id",
                        cursor.rowcount, table_name,
                    )
            except sqlite3.OperationalError as e:
                logger.warning(
                    "Project backfill skipped for %s: %s", table_name, e,
                )

        # --- Backfill project_agents junction table from existing
        #     AgentApp.project_id values. For every agent_apps row with a
        #     non-null project_id, insert a corresponding project_agents
        #     row (idempotent — uses INSERT OR IGNORE so re-runs are safe
        #     and the unique constraint prevents duplicates). The agent's
        #     primary project_id is marked with role='primary' so the UI
        #     can show the home badge.
        try:
            cursor.execute("""
                SELECT name FROM sqlite_master
                WHERE type='table' AND name='project_agents'
            """)
            if cursor.fetchone():
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO project_agents
                        (id, project_id, agent_id, role,
                         org_id, app_id,
                         created_date, updated_date, is_deleted)
                    SELECT
                        lower(hex(randomblob(4))) || '-' ||
                        lower(hex(randomblob(2))) || '-4' ||
                        substr(lower(hex(randomblob(2))), 2) || '-' ||
                        substr('89ab', 1 + (abs(random()) % 4), 1) ||
                        substr(lower(hex(randomblob(2))), 2) || '-' ||
                        lower(hex(randomblob(6))),
                        a.project_id,
                        a.id,
                        'primary',
                        a.org_id,
                        a.app_id,
                        a.created_date,
                        a.updated_date,
                        0
                    FROM agent_apps a
                    WHERE a.project_id IS NOT NULL
                      AND a.is_deleted = 0
                      AND NOT EXISTS (
                          SELECT 1 FROM project_agents pa
                          WHERE pa.project_id = a.project_id
                            AND pa.agent_id   = a.id
                            AND pa.org_id     = a.org_id
                            AND pa.app_id     = a.app_id
                      )
                    """
                )
                if cursor.rowcount > 0:
                    logger.info(
                        "Auto-migration: backfilled %d project_agents row(s) "
                        "from existing agent_apps.project_id",
                        cursor.rowcount,
                    )
        except sqlite3.OperationalError as e:
            logger.warning(
                "ProjectAgent backfill skipped: %s", e,
            )

        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("Auto-migration error (non-fatal): %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Unified startup/shutdown lifecycle (replaces deprecated @app.on_event).

    Startup runs DB logging, SQLite schema creation + auto-migrations, system
    agent seeding, prompt normalisation, OpenHarness service init, the
    scheduled-task runner, the automation dispatcher, and the zombie reaper.
    Shutdown gracefully stops the automation dispatcher.
    """
    import logging
    startup_logger = logging.getLogger("zhanlu.startup")

    # ── Loudly report the active database ──
    db_logger = logging.getLogger("zhanlu.startup.db")
    db_logger.warning(
        "Connected to %s (org_id=%s, app_id=%s)",
        settings.database_url_safe,
        settings.DEFAULT_ORG_ID,
        settings.DEFAULT_APP_ID,
    )
    print(
        f"[zhanlu] Connected to {settings.database_url_safe} "
        f"(org_id={settings.DEFAULT_ORG_ID}, app_id={settings.DEFAULT_APP_ID})"
    )

    if settings.is_sqlite:
        Base.metadata.create_all(bind=engine)
        _run_auto_migrations()
    else:
        # Postgres: create_all with checkfirst=True is idempotent — it
        # only creates tables that don't exist. New models (added via
        # the auto-import in app.models) get their tables created on
        # next startup. Pre-existing tables are left untouched.
        # The new ``_run_auto_migrations`` Postgres branch (added
        # 2026-07-29) handles adding columns to existing tables.
        Base.metadata.create_all(bind=engine)
        _run_auto_migrations()

    # Load lifecycle hooks (built-in safety hooks + DB-backed org/app rules)
    # into the live HookExecutor. Runs after schema creation so the hook_rules
    # table exists. Non-fatal: builtins still load if the DB query fails.
    try:
        from app.database import SessionLocal
        from app.services.hooks.loader import load_hooks
        _hook_db = SessionLocal()
        try:
            n = load_hooks(_hook_db)
            startup_logger.info("Startup: loaded %d lifecycle hook(s)", n)
        finally:
            _hook_db.close()
    except Exception as e:
        startup_logger.warning("Hook loader error (non-fatal): %s", e)

    # Auto-sync curated marketplace sources on startup so the cards
    # populate their skill counts before the user even opens the tab.
    # The same hook fires on every GET /sources, but kicking it off
    # here means a fresh deploy doesn't need a user click to fill the
    # cards. The sync runs in a daemon thread so startup isn't blocked.
    try:
        import asyncio
        import threading
        from app.services.skill_source_service import (
            seed_curated_sources,
            get_curated_sources_needing_sync,
            sync_source,
        )
        from app.database import SessionLocal

        def _startup_sync():
            db = SessionLocal()
            try:
                seed_curated_sources(db)
                for src in get_curated_sources_needing_sync(db):
                    asyncio.run(sync_source(src.id))
            finally:
                db.close()

        t = threading.Thread(target=_startup_sync, daemon=True, name="startup-marketplace-sync")
        t.start()
    except Exception as e:
        startup_logger.warning("Startup marketplace auto-sync error (non-fatal): %s", e)

    # Ensure DB-backed system agents (agent_builder, skill_agent, etc.) exist
    try:
        from app.services.system_agents import ensure_system_agents
        ensure_system_agents()
    except Exception as e:
        startup_logger.warning("System agent seeding error (non-fatal): %s", e)

    # Ensure super-admin account exists (idempotent, reads env vars)
    try:
        from app.services.ensure_superadmin import ensure_superadmin
        ensure_superadmin()
    except Exception as e:
        startup_logger.warning("Super-admin seeding error (non-fatal): %s", e)

    # Normalize prompt_tools for all agents with bound KnowledgeBases.
    try:
        from app.services.agent_tools import normalize_all_agent_prompts
        from app.database import SessionLocal
        norm_db = SessionLocal()
        try:
            count = normalize_all_agent_prompts(norm_db)
            if count > 0:
                startup_logger.info(
                    "Startup: normalized prompt_tools for %d agent(s) with bound KBs", count
                )
        finally:
            norm_db.close()
    except Exception as e:
        startup_logger.warning(
            "Prompt normalization on startup error (non-fatal): %s", e
        )

    # Initialize OpenHarness-migrated services
    try:
        # Load agent definitions from .md files
        from app.services.agent_definitions import get_loader
        get_loader().load()

        # Load skills from backend/skills/ directory
        from app.services.skills_loader import get_skills_registry
        get_skills_registry().load()

        # Sync marketplace skills into DB catalog
        try:
            from app.services.skill_sync import sync_marketplace_to_db
            from app.database import SessionLocal
            sync_db = SessionLocal()
            try:
                sync_marketplace_to_db(sync_db)
            finally:
                sync_db.close()
        except Exception as sync_err:
            startup_logger.warning(
                "Marketplace skill sync error (non-fatal): %s", sync_err
            )

        # Register the unified Skill meta-tool
        try:
            from app.services.skill_routing.meta_tool import register_skill_meta_tool
            register_skill_meta_tool()
        except Exception as meta_err:
            startup_logger.warning(
                "Skill meta-tool registration error (non-fatal): %s", meta_err
            )

        # Initialize tool_artifacts directory
        from app.services.tool_output import get_tool_output_manager
        get_tool_output_manager()

        startup_logger.info("OpenHarness-migrated services initialized")
    except Exception as e:
        startup_logger.warning("Service initialization error (non-fatal): %s", e)

    # Start periodic background tasks (memory consolidation, skill curation)
    try:
        from app.services.scheduled_tasks import start_scheduled_tasks
        start_scheduled_tasks()
    except Exception as e:
        startup_logger.warning("Scheduled tasks startup error (non-fatal): %s", e)

    # Start the automation dispatcher (Manus-style execution engine)
    try:
        from app.services.automation_dispatcher import start_dispatcher
        start_dispatcher()
    except Exception as e:
        startup_logger.warning(
            "Automation dispatcher startup error (non-fatal): %s", e
        )

    # Reap executions left mid-flight by a previous process.
    try:
        from app.services.automation_dispatcher import reap_on_startup
        reaped = await reap_on_startup()
        if reaped:
            startup_logger.info(
                "Automation startup reaper: cleaned up %d zombie execution(s)", reaped
            )
    except Exception as e:
        startup_logger.warning(
            "Automation startup reaper error (non-fatal): %s", e
        )

    # Start the DataExecution cleanup loop (TTL sweep + per-session cap)
    if getattr(settings, "DATA_EXECUTION_CLEANUP_ENABLED", False):
        try:
            from app.services.data_execution.cleanup import scheduled_cleanup_loop
            asyncio.create_task(
                scheduled_cleanup_loop(),
                name="data-execution-cleanup",
            )
            startup_logger.info("DataExecution cleanup loop started (interval=%ds)", 3600)
        except Exception as e:
            startup_logger.warning(
                "DataExecution cleanup loop startup error (non-fatal): %s", e
            )

    # Start the catalog indexing watchdog (recovers from stuck indexing status)
    catalog_watchdog_stop = asyncio.Event()
    catalog_watchdog_task = None
    try:
        from app.services.knowledge_graph.catalog_watchdog import run_catalog_watchdog
        catalog_watchdog_task = asyncio.create_task(
            run_catalog_watchdog(catalog_watchdog_stop),
            name="catalog-watchdog",
        )
        # Use the catalog_watchdog module logger (INFO level) so it shows up.
        from app.services.knowledge_graph import catalog_watchdog as _cw
        _cw.logger.info("Catalog watchdog started (poll=%ds, stuck_after=%ds)",
                         _cw.POLL_INTERVAL_SECONDS, _cw.STUCK_AFTER_SECONDS)
    except Exception as e:
        startup_logger.warning("Catalog watchdog startup error (non-fatal): %s", e)

    # Mount persisted full-stack dashboard apps (generated sub-routers + pollers)
    try:
        from app.services.dashboard_app.manager import dashboard_app_manager
        dashboard_app_manager.init_app(app)
        n_apps = dashboard_app_manager.load_persisted_apps()
        if n_apps:
            startup_logger.info("Dashboard app manager: mounted %d app(s)", n_apps)
    except Exception as e:
        startup_logger.warning("Dashboard app manager startup error (non-fatal): %s", e)

    yield

    # ── Shutdown ──
    try:
        from app.services.dashboard_app.manager import dashboard_app_manager
        dashboard_app_manager.shutdown()
    except Exception as e:
        startup_logger.warning("Dashboard app manager shutdown error (non-fatal): %s", e)

    catalog_watchdog_stop.set()
    if catalog_watchdog_task is not None:
        try:
            await asyncio.wait_for(catalog_watchdog_task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        except Exception as e:
            startup_logger.warning("Catalog watchdog shutdown error: %s", e)

    try:
        from app.services.automation_dispatcher import stop_dispatcher
        await stop_dispatcher()
    except Exception as e:
        startup_logger.warning(
            "Automation dispatcher shutdown error (non-fatal): %s", e
        )


def create_app() -> FastAPI:
    app = FastAPI(
        title="Zhanlu Backend",
        description="Drop-in replacement for Base44 API - Phase 1",
        version="1.0.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization", "Content-Type",
            "X-Base44-Anonymous-Id", "X-Requested-With",
        ],
        expose_headers=["Content-Disposition"],
    )

    # All SDK requests go through /api/* (baseURL = serverUrl + "/api")
    # Auth router FIRST so User/me routes match before User/{record_id} in entity router
    app.include_router(auth_router, prefix="/api")
    app.include_router(public_router, prefix="/api")
    app.include_router(diagnostics_router, prefix="/api")  # /api/_db-info etc.

    for entity_name, model_class in ENTITY_MODELS.items():
        router = register_entity_router(entity_name, model_class)
        app.include_router(router, prefix="/api")

    # Project-bundle KB sharing: lists all KBs bound to a project
    # (including admin-created ones for shared project recipients)
    app.include_router(register_project_kb_router())

    # Project Data Map: catalog browser, overlays, entities, resource registry
    from app.routers.project_catalog import register_project_catalog_router

    app.include_router(register_project_catalog_router())

    app.include_router(functions_router, prefix="/api")
    app.include_router(integrations_router, prefix="/api")
    app.include_router(agents_router, prefix="/api")
    app.include_router(analytics_router, prefix="/api")
    app.include_router(openharness_router, prefix="/api")
    app.include_router(artifacts_router, prefix="/api")
    app.include_router(sandbox_router, prefix="/api")
    app.include_router(data_snapshots_router, prefix="/api")
    app.include_router(agent_runs_router, prefix="/api")  # P0-3
    app.include_router(executions_router, prefix="/api")
    app.include_router(agent_studio_router, prefix="/api")
    app.include_router(skill_studio_router, prefix="/api")
    app.include_router(governance_router, prefix="/api")
    app.include_router(workspace_settings_router, prefix="/api")
    app.include_router(marketplace_router, prefix="/api")
    app.include_router(mcp_router, prefix="/api")
    app.include_router(nl2sql_router, prefix="/api")
    app.include_router(automation_api_router, prefix="/api")
    app.include_router(knowledge_bases_router, prefix="/api")
    app.include_router(dashboards_router, prefix="/api")
    app.include_router(hooks_router, prefix="/api")
    app.include_router(admin_users_router, prefix="/api")
    app.include_router(admin_evals_router, prefix="/api")
    app.include_router(admin_invocations_router, prefix="/api")
    app.include_router(resource_shares_router, prefix="/api")
    app.include_router(access_policies_router, prefix="/api")
    app.include_router(chat_tools_router, prefix="/api")
    # Public read-only share pages — registered WITHOUT the /api prefix so
    # /share/c/<token> is a short, shareable URL (no auth required).
    app.include_router(chat_share_public_router)
    app.include_router(llm_router)
    app.include_router(app_logs_router, prefix="/api")
    app.include_router(project_memories_router, prefix="/api")  # /api/projects/{id}/memories
    app.include_router(project_knowledge_router, prefix="/api")  # 2026-08-25: project knowledge cache admin
    # NB: app_logs_router already declares its full path including ``/api/app-logs/...``
    # internally so it ignores the extra ``/api`` prefix and lives at
    # ``/api/app-logs/...`` — matching the @base44 plugin's expected URL.

    # Serve uploaded files under /api/uploads so the Vite proxy routes them to backend
    app.mount("/api/uploads", StaticFiles(directory=str(settings.upload_path)), name="uploads")

    return app


app = create_app()


@app.get("/healthz")
async def healthz():
    """Health check endpoint for Docker compose / load balancers."""
    return {"status": "ok", "service": "zhanlu-backend"}


@app.get("/")
async def root():
    return {"status": "ok", "service": "zhanlu-backend", "version": "1.0.0", "phase": 1}


@app.get("/api")
async def api_root():
    return {"status": "ok", "message": "Zhanlu Backend API"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.BACKEND_PORT,
        reload=True,
    )
