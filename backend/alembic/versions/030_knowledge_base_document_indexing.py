"""030_knowledge_base_document_indexing

Revision ID: 030
Revises: 029
Create Date: 2026-07-27

Add document-indexing state columns to ``knowledge_bases`` so file-kind
KBs can track chunking/embedding progress for the RAG pipeline.

Columns added (all nullable so existing database-kind KBs are unaffected):

  * ``indexing_status`` — None | "pending" | "indexing" | "ready" | "failed"
  * ``chunk_count``      — int, number of embedded chunks stored
  * ``index_error``      — short error string when status == "failed"
  * ``last_indexed_at``  — UTC timestamp of the last successful ingest

Idempotent in live mode (safe to re-run on a DB where the columns
already exist); degrades to unconditional SQL in offline ``--sql`` mode.
Modeled on ``029_chat_session_conversation_and_agent.py``.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "030"
down_revision: Union[str, None] = "029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(conn, table: str, column: str) -> bool:
    """Live-mode column-existence check. Always False in offline mode."""
    if op.get_context().as_sql:
        return False
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    ).fetchone()
    return result is not None


def upgrade() -> None:
    conn = op.get_bind()
    for col, typ in (
        ("indexing_status", sa.String(50)),
        ("chunk_count", sa.Integer()),
        ("index_error", sa.Text()),
        ("last_indexed_at", sa.DateTime()),
    ):
        if not _column_exists(conn, "knowledge_bases", col):
            with op.batch_alter_table("knowledge_bases") as batch_op:
                batch_op.add_column(sa.Column(col, typ, nullable=True))


def downgrade() -> None:
    """Drop the four indexing columns. Intentionally NOT gated on
    ``_column_exists`` so offline ``--sql`` mode produces a complete script."""
    with op.batch_alter_table("knowledge_bases") as batch_op:
        batch_op.drop_column("last_indexed_at")
        batch_op.drop_column("index_error")
        batch_op.drop_column("chunk_count")
        batch_op.drop_column("indexing_status")
