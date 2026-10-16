"""P-SV2: query lineage metadata + org alignment for the semantic stores.

Two audit-closure changes:

1. ``nl2sql_query_logs.lineage_json`` — per-execution data lineage (tables,
   columns, join pairs, filter columns) extracted from the generated SQL and
   persisted alongside it, so "where did this figure come from" is answerable
   from the log alone.
2. org alignment backfill — every semantic-store row is stamped with the
   org_id of the knowledge base (or project) it derives from. TimestampedBase
   already carried ``org_id`` (default 'default-org'), but nothing enforced
   it inside the stores; after this backfill the stores can filter strictly
   by org without hiding legacy rows.
"""

from alembic import op
import sqlalchemy as sa

revision = "100_lineage_org_alignment"
down_revision = "099_grain_authority"
branch_labels = None
depends_on = None

# (table, kb column) pairs whose rows must carry the owning KB's org_id.
_KB_SCOPED = [
    ("kb_table_meta", "kb_id"),
    ("kb_table_relation", "kb_id"),
    ("kb_domain", "kb_id"),
    ("kb_domain_member", "kb_id"),
    ("query_plan_memory", "kb_id"),
    ("query_workload_stats", "kb_id"),
    ("nl2sql_query_logs", "datasource_id"),
    ("q_sql_examples", "datasource_id"),
    ("terminologies", None),  # datasource_ids JSON list — handled separately
    ("semantic_guidelines", "datasource_id"),
]


def upgrade() -> None:
    op.add_column(
        "nl2sql_query_logs",
        sa.Column("lineage_json", sa.JSON(), nullable=True),
    )
    bind = op.get_bind()
    for table, kb_col in _KB_SCOPED:
        if kb_col is None:
            continue
        bind.execute(sa.text(
            f"UPDATE {table} t SET org_id = k.org_id "
            f"FROM knowledge_bases k WHERE t.{kb_col} = k.id "
            f"AND t.org_id <> k.org_id"
        ))
    # kb_column_meta is scoped through table_meta_id (two hops).
    bind.execute(sa.text(
        "UPDATE kb_column_meta c SET org_id = k.org_id "
        "FROM kb_table_meta t JOIN knowledge_bases k ON t.kb_id = k.id "
        "WHERE c.table_meta_id = t.id AND c.org_id <> k.org_id"
    ))


def downgrade() -> None:
    op.drop_column("nl2sql_query_logs", "lineage_json")
