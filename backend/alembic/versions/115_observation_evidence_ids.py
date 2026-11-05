"""115: observation_records.evidence_ids (Phase 3.1 durable evidence refs).

ObservationRecord rows are the durable per-node tool results, but evidence/
citation references lived only inside result_data text. This column persists
the evidence-registry references so a restarted run can restore provenance
without re-querying.

Guarded with information_schema checks so stacks whose alembic lineage
diverged (deployed containers) can also apply it idempotently via raw SQL.

Revision ID: 115_observation_evidence
Revises: 114_enterprise_process
"""
from alembic import op
import sqlalchemy as sa

revision = "115_observation_evidence"
down_revision = "114_enterprise_process"
branch_labels = None
depends_on = None

TABLE = "observation_records"
COLUMN = "evidence_ids"


def _column_exists(conn, table: str, column: str) -> bool:
    row = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    ).first()
    return row is not None


def upgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn, TABLE, COLUMN):
        conn.execute(
            sa.text(
                f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} JSON"
            )
        )


def downgrade() -> None:
    conn = op.get_bind()
    if _column_exists(conn, TABLE, COLUMN):
        conn.execute(sa.text(f"ALTER TABLE {TABLE} DROP COLUMN {COLUMN}"))
