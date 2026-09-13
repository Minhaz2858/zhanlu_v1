"""Alembic environment configuration for migrations."""

from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool, text
from alembic import context

from app.config import settings
from app.database import Base
from app.models import (
    Organization, AppWorkspace,
    User, Project, ChatSession, ChatMessage, AgentApp, KnowledgeBase,
    AutomationTask, Tool, UserFile, Report, DecisionFlow, MarketAgent,
    McpServer, UserSetting, AgentConversation,
    Artifact, ArtifactVersion, ArtifactBlob, MessageArtifact, ArtifactSourcePart,
    SandboxJob, SandboxJobEvent, SandboxCommand,
    DataSnapshot, SnapshotArtifactLink,
    Execution, Plan, PlanNode, ObservationRecord,
    AgentDataBinding, AgentSkillBinding, SkillProfile, SkillCandidate,
    PolicyDecision, ApprovalRequest, CostLedger, AuditLog,
)

config = context.config
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=settings.is_sqlite,
        )
        with context.begin_transaction():
            # Several revision ids in this chain exceed the 32 chars Alembic's
            # default version_num column allows (e.g.
            # 044_agent_run_steps_and_reasoning,
            # 056_add_llm_models_bypass_hallucination_guardrail). SQLite does
            # not enforce VARCHAR length, but PostgreSQL does — so ensure the
            # version table is VARCHAR(128) in every DB state:
            #   * fresh DB — no table yet: create it wide (Alembic skips its
            #     own VARCHAR(32) create when the table already exists);
            #   * existing DB — narrow table left by an earlier Alembic run:
            #     widen it in place.
            if not settings.is_sqlite:
                connection.execute(
                    text(
                        "DO $$ BEGIN "
                        "IF EXISTS (SELECT FROM pg_catalog.pg_tables "
                        "           WHERE schemaname = current_schema() "
                        "             AND tablename = 'alembic_version') THEN "
                        "  ALTER TABLE alembic_version "
                        "    ALTER COLUMN version_num TYPE VARCHAR(128); "
                        "ELSE "
                        "  CREATE TABLE alembic_version ("
                        "    version_num VARCHAR(128) NOT NULL, "
                        "    CONSTRAINT alembic_version_pkc "
                        "      PRIMARY KEY (version_num)); "
                        "END IF; END $$;"
                    )
                )
            else:
                connection.execute(
                    text(
                        "CREATE TABLE IF NOT EXISTS alembic_version ("
                        "version_num VARCHAR(128) NOT NULL, "
                        "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
                    )
                )
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
