"""Ensure the Project Knowledge Graph / semantic-layer tables exist (explicit DDL).

``app.models.knowledge_catalog`` declares ``kb_table_meta``, ``kb_column_meta``,
``kb_table_relation``, ``project_entity``, ``project_entity_link``,
``project_catalog_overlay`` and ``project_metric``. Their module docstring
stated they followed the "metadata / create_all convention (no alembic
migration required)" — i.e. they were previously only materialised by
``Base.metadata.create_all`` at app startup.

However migrations ``064_business_semantic_layer`` and ``066_entity_master_table_role``
ALTER these tables (e.g. ``coverage_json``, ``table_role``, ``entity_master_hints``)
and therefore hard-require them to already exist. On a clean database where
``create_all`` has not been run, ``alembic upgrade head`` broke with
``NoSuchTableError: kb_table_meta``.

This corrective migration inserts the missing table creation immediately before
064. Every column, type, index, constraint and FK is defined EXPLICITLY here
(mirroring the ORM models) — there is NO ``Base.metadata.create_all`` call and
NO ORM-model import. Each table is existence-guarded so it is safe to re-run and
safe on databases where the tables were already created out-of-band.
"""

from alembic import op
import sqlalchemy as sa


revision = "063b_ensure_kb_catalog_tables"
down_revision = "063_add_weekly_digest_summaries"
branch_labels = None
depends_on = None


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table in inspector.get_table_names()


def _ts_columns() -> list:
    """Shared TimestampedBase columns, declared explicitly to match the ORM."""
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
    if not _table_exists("kb_table_meta"):
        op.create_table(
            "kb_table_meta",
            *_ts_columns(),
            sa.Column(
                "kb_id",
                sa.String(36),
                sa.ForeignKey("knowledge_bases.id"),
                nullable=False,
                index=True,
            ),
            sa.Column("schema_name", sa.String(128), nullable=False),
            sa.Column("table_name", sa.String(256), nullable=False),
            sa.Column("table_type", sa.String(32), nullable=False),
            sa.Column("row_count", sa.BigInteger(), nullable=True),
            sa.Column("size_bytes", sa.BigInteger(), nullable=True),
            sa.Column("description_zh", sa.Text(), nullable=True),
            sa.Column("description_en", sa.Text(), nullable=True),
            sa.Column("sample_values", sa.JSON(), nullable=True),
            sa.Column("coverage_json", sa.JSON(), nullable=True),
            sa.Column("table_role", sa.String(32), nullable=False),
            sa.Column("entity_master_hints", sa.JSON(), nullable=True),
            sa.Column("indexed_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "kb_id", "schema_name", "table_name", name="uq_kb_table_meta"
            ),
        )

    if not _table_exists("kb_column_meta"):
        op.create_table(
            "kb_column_meta",
            *_ts_columns(),
            sa.Column(
                "table_meta_id",
                sa.String(36),
                sa.ForeignKey("kb_table_meta.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("column_name", sa.String(256), nullable=False),
            sa.Column("ordinal", sa.Integer(), nullable=False),
            sa.Column("data_type", sa.String(64), nullable=False),
            sa.Column("is_nullable", sa.Boolean(), nullable=False),
            sa.Column("is_primary_key", sa.Boolean(), nullable=False),
            sa.Column("default_value", sa.Text(), nullable=True),
            sa.Column("description_zh", sa.Text(), nullable=True),
            sa.Column("description_en", sa.Text(), nullable=True),
            sa.Column("sample_values", sa.JSON(), nullable=True),
            sa.UniqueConstraint(
                "table_meta_id", "column_name", name="uq_kb_column_meta"
            ),
        )

    if not _table_exists("kb_table_relation"):
        op.create_table(
            "kb_table_relation",
            *_ts_columns(),
            sa.Column(
                "kb_id",
                sa.String(36),
                sa.ForeignKey("knowledge_bases.id"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "source_table_meta_id",
                sa.String(36),
                sa.ForeignKey("kb_table_meta.id"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "target_table_meta_id",
                sa.String(36),
                sa.ForeignKey("kb_table_meta.id"),
                nullable=False,
                index=True,
            ),
            sa.Column("relation_type", sa.String(16), nullable=False),
            sa.Column("source_columns", sa.JSON(), nullable=False),
            sa.Column("target_columns", sa.JSON(), nullable=False),
            sa.Column("source_cardinality", sa.String(16), nullable=True),
            sa.Column("target_cardinality", sa.String(16), nullable=True),
            sa.Column("confidence", sa.Float(), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.UniqueConstraint(
                "source_table_meta_id",
                "target_table_meta_id",
                name="uq_kb_table_relation",
            ),
        )

    if not _table_exists("project_entity"):
        op.create_table(
            "project_entity",
            *_ts_columns(),
            sa.Column("project_id", sa.String(36), nullable=False, index=True),
            sa.Column("name", sa.String(191), nullable=False),
            sa.Column("aliases", sa.JSON(), nullable=True),
            sa.Column("entity_type", sa.String(50), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("source", sa.String(50), nullable=False),
            sa.Column("source_ref", sa.String(36), nullable=True, index=True),
            sa.UniqueConstraint("project_id", "name", name="uq_project_entity"),
        )

    if not _table_exists("project_entity_link"):
        op.create_table(
            "project_entity_link",
            *_ts_columns(),
            sa.Column(
                "entity_id",
                sa.String(36),
                sa.ForeignKey("project_entity.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("target_type", sa.String(30), nullable=False),
            sa.Column("target_id", sa.String(191), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False),
            sa.Column("source", sa.String(50), nullable=False),
            sa.UniqueConstraint(
                "entity_id",
                "target_type",
                "target_id",
                name="uq_project_entity_link",
            ),
        )

    if not _table_exists("project_catalog_overlay"):
        op.create_table(
            "project_catalog_overlay",
            *_ts_columns(),
            sa.Column("project_id", sa.String(36), nullable=False, index=True),
            sa.Column("kb_id", sa.String(36), nullable=True),
            sa.Column("table_name", sa.String(191), nullable=True),
            sa.Column("alias", sa.String(191), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("metric_definition", sa.Text(), nullable=True),
            sa.Column("table_role", sa.String(32), nullable=True),
            sa.Column("scope", sa.String(20), nullable=False),
            sa.UniqueConstraint(
                "project_id",
                "kb_id",
                "table_name",
                name="uq_project_catalog_overlay",
            ),
        )

    if not _table_exists("project_metric"):
        op.create_table(
            "project_metric",
            *_ts_columns(),
            sa.Column("project_id", sa.String(36), nullable=False, index=True),
            sa.Column("kb_id", sa.String(36), nullable=True, index=True),
            sa.Column("name", sa.String(191), nullable=False),
            sa.Column("aliases", sa.JSON(), nullable=True),
            sa.Column("definition", sa.Text(), nullable=True),
            sa.Column("sql_expression", sa.Text(), nullable=True),
            sa.Column("query_pattern", sa.Text(), nullable=True),
            sa.Column("unit", sa.String(32), nullable=True),
            sa.Column("default_aggregation", sa.String(16), nullable=True),
            sa.Column("bindings", sa.JSON(), nullable=True),
            sa.Column("source", sa.String(16), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.UniqueConstraint("project_id", "name", name="uq_project_metric"),
        )


def downgrade() -> None:
    for t in (
        "project_metric",
        "project_catalog_overlay",
        "project_entity_link",
        "project_entity",
        "kb_table_relation",
        "kb_column_meta",
        "kb_table_meta",
    ):
        if _table_exists(t):
            op.drop_table(t)
