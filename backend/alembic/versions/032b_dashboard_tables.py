"""Create the ``dashboards`` and ``dashboard_versions`` tables (explicit DDL).

The ORM models ``app.models.dashboard.Dashboard`` (``__tablename__ =
"dashboards"``) and ``app.models.dashboard_version.DashboardVersion``
(``__tablename__ = "dashboard_versions"``) exist, but NO prior migration ever
created these tables. Migration ``033_add_agent_conversation_dashboard_id``
adds a FOREIGN KEY ``agent_conversations.dashboard_id → dashboards.id`` and
therefore hard-requires ``dashboards`` to already exist — so ``alembic upgrade
head`` broke at 033 with ``relation "dashboards" does not exist`` on a clean
database.

This corrective migration inserts the missing table creation immediately before
033 in the chain so the FK resolves. Every column, type, index, constraint and
FK is defined EXPLICITLY here (mirroring the ORM models) — there is no
``Base.metadata.create_all`` dependency. It is existence-guarded
(``_table_exists``) so it is safe to re-run and safe on databases where the
tables were already created out-of-band (e.g. a legacy ``create_all``).

``dashboard_versions`` is included here too because the model references
``dashboards.id`` and the application reads/writes it; leaving it unmigrated
would let ``alembic upgrade head`` "succeed" while the app still failed at
runtime.

Revision ID: 032b_dashboard_tables
Revises: 032
Create Date: 2026-09-04
"""

from alembic import op
import sqlalchemy as sa


revision = "032b_dashboard_tables"
down_revision = "032"
branch_labels = None
depends_on = None


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table in inspector.get_table_names()


def _ts_columns() -> list:
    """The shared TimestampedBase columns, declared explicitly to match the
    ORM models (no ``create_all`` dependency)."""
    return [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime(), nullable=True),
        sa.Column("updated_date", sa.DateTime(), nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column(
            "is_deleted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("org_id", sa.String(36), nullable=False, index=True),
        sa.Column("app_id", sa.String(36), nullable=False, index=True),
    ]


def upgrade() -> None:
    if not _table_exists("dashboards"):
        op.create_table(
            "dashboards",
            *_ts_columns(),
            # Dual project binding (id-OR-name convention used across zhanlu).
            sa.Column(
                "project_id",
                sa.String(36),
                sa.ForeignKey("projects.id"),
                nullable=True,
                index=True,
            ),
            sa.Column("project", sa.String(200), nullable=False),
            # The ONE database KnowledgeBase this dashboard queries.
            sa.Column(
                "datasource_kb_id",
                sa.String(36),
                sa.ForeignKey("knowledge_bases.id"),
                nullable=False,
                index=True,
            ),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("definition", sa.JSON(), nullable=False),
            sa.Column("refresh_interval_seconds", sa.Integer(), nullable=False),
        )

    if not _table_exists("dashboard_versions"):
        op.create_table(
            "dashboard_versions",
            *_ts_columns(),
            sa.Column(
                "dashboard_id",
                sa.String(36),
                sa.ForeignKey("dashboards.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("version_number", sa.Integer(), nullable=False),
            # Full snapshot of the dashboard definition + scalar fields.
            sa.Column("config", sa.JSON(), nullable=False),
            sa.Column("source", sa.String(50), nullable=True),
            sa.Column("summary", sa.String(500), nullable=True),
        )


def downgrade() -> None:
    if _table_exists("dashboard_versions"):
        op.drop_table("dashboard_versions")
    if _table_exists("dashboards"):
        op.drop_table("dashboards")
