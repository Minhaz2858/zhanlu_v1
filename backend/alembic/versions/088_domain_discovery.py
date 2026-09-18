"""Business Domain layer — kb_domain / kb_domain_member + column semantic typing.

Revision ID: 088_domain_discovery
Revises: 087_mcp_granted_agents
Create Date: 2026-09-18

Adds:
- ``kb_domain`` / ``kb_domain_member`` — the named business-domain map produced
  by ``services/knowledge_graph/domain_discovery.py`` (Louvain + K-Means +
  centroid-cosine merge + LLM naming).
- ``kb_table_meta.domain_name`` — denormalised domain label for cheap TOC
  rendering / prompt grouping.
- ``kb_column_meta.semantic_type`` / ``enum_values`` / ``unit`` — per-column
  semantic typing and low-cardinality value dictionaries.

Idempotent: every object is existence-checked first.
"""

from alembic import op
import sqlalchemy as sa


revision = "088_domain_discovery"
down_revision = "087_mcp_granted_agents"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table: str) -> bool:
    return table in _inspector().get_table_names()


def _has_column(table: str, column: str) -> bool:
    if not _has_table(table):
        return False
    return column in {c["name"] for c in _inspector().get_columns(table)}


def _base_columns() -> list[sa.Column]:
    return [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime(), nullable=True),
        sa.Column("updated_date", sa.DateTime(), nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("org_id", sa.String(36), nullable=False, server_default="default-org"),
        sa.Column("app_id", sa.String(36), nullable=False, server_default="default-app"),
    ]


def upgrade() -> None:
    if not _has_column("kb_table_meta", "domain_name"):
        op.add_column(
            "kb_table_meta", sa.Column("domain_name", sa.String(191), nullable=True)
        )
        op.create_index("ix_kb_table_meta_domain_name", "kb_table_meta", ["domain_name"])

    if not _has_column("kb_column_meta", "semantic_type"):
        op.add_column(
            "kb_column_meta",
            sa.Column(
                "semantic_type", sa.String(16), nullable=False, server_default="unknown"
            ),
        )
    if not _has_column("kb_column_meta", "enum_values"):
        op.add_column(
            "kb_column_meta", sa.Column("enum_values", sa.JSON(), nullable=True)
        )
    if not _has_column("kb_column_meta", "unit"):
        op.add_column("kb_column_meta", sa.Column("unit", sa.String(32), nullable=True))

    if not _has_table("kb_domain"):
        op.create_table(
            "kb_domain",
            *_base_columns(),
            sa.Column("kb_id", sa.String(36), nullable=False),
            sa.Column("name_en", sa.String(191), nullable=False),
            sa.Column("name_zh", sa.String(191), nullable=True),
            sa.Column("slug", sa.String(191), nullable=True),
            sa.Column("description_en", sa.Text(), nullable=True),
            sa.Column("description_zh", sa.Text(), nullable=True),
            sa.Column("member_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("method", sa.String(16), nullable=False, server_default="derived"),
            sa.Column("confidence", sa.Float(), nullable=True),
            sa.Column("indexed_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("kb_id", "name_en", name="uq_kb_domain"),
        )
        op.create_index("ix_kb_domain_kb_id", "kb_domain", ["kb_id"])

    if not _has_table("kb_domain_member"):
        op.create_table(
            "kb_domain_member",
            *_base_columns(),
            sa.Column("domain_id", sa.String(36), nullable=False),
            sa.Column("kb_id", sa.String(36), nullable=False),
            sa.Column("table_meta_id", sa.String(36), nullable=False),
            sa.Column("table_name", sa.String(256), nullable=False, server_default=""),
            sa.Column("rank", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("weight", sa.Float(), nullable=True),
            sa.UniqueConstraint("domain_id", "table_meta_id", name="uq_kb_domain_member"),
        )
        op.create_index("ix_kb_domain_member_domain_id", "kb_domain_member", ["domain_id"])
        op.create_index("ix_kb_domain_member_kb_id", "kb_domain_member", ["kb_id"])
        op.create_index(
            "ix_kb_domain_member_table_meta_id", "kb_domain_member", ["table_meta_id"]
        )


def downgrade() -> None:
    if _has_table("kb_domain_member"):
        op.drop_table("kb_domain_member")
    if _has_table("kb_domain"):
        op.drop_table("kb_domain")
    if _has_column("kb_table_meta", "domain_name"):
        op.drop_column("kb_table_meta", "domain_name")
    for col in ("unit", "enum_values", "semantic_type"):
        if _has_column("kb_column_meta", col):
            op.drop_column("kb_column_meta", col)
