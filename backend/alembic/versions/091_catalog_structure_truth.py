"""Catalog structure truth — source comments, indexes, size, row provenance.

Revision ID: 091_catalog_structure_truth
Revises: 090_nl2sql_readiness
Create Date: 2026-09-20

Onboarding used to drop most of what a source will tell you about itself: a
MySQL column comment (``COLUMN_COMMENT``) was never SELECTed, ``COLUMN_KEY``'s
UNI/MUL were read and thrown away, PostgreSQL comments and index definitions
were read nowhere, ``kb_table_meta.size_bytes`` was declared and never written,
and a row count that timed out left no trace of whether the number was exact or
an engine estimate.

Adds:

- ``kb_table_meta.source_comment`` (TEXT) — the table's own comment, verbatim.
- ``kb_table_meta.indexes_json`` (JSON) — index inventory as declared
  ``[{"name","columns":[...],"unique":bool,"primary":bool}]``. ``[]`` means the
  source declares no index; NULL means not collected.
- ``kb_table_meta.row_count_source`` (VARCHAR(16)) — 'exact' | 'estimated'.
- ``kb_table_meta.description_source`` (VARCHAR(16)) — 'llm' | 'source_comment'.
- ``kb_column_meta.source_comment`` (TEXT) — the column's own comment, verbatim.
- ``kb_column_meta.key_role`` (VARCHAR(16)) — 'PRI' | 'UNI' | 'MUL'.
- ``kb_column_meta.description_source`` (VARCHAR(16)) — 'llm' | 'source_comment'.

Idempotent: column existence checks first (repo convention). Mirrored in
``main.py::_specific_migrations`` for deployments that never run alembic.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "091_catalog_structure_truth"
down_revision = "090_nl2sql_readiness"
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return False
    existing = {c["name"] for c in inspector.get_columns(table)}
    return column in existing


def _add(table: str, column: str, type_: sa.types.TypeEngine, **kw) -> None:
    if not _column_exists(table, column):
        op.add_column(table, sa.Column(column, type_, nullable=True, **kw))


def upgrade() -> None:
    # ── kb_table_meta ───────────────────────────────────────────────────
    _add("kb_table_meta", "source_comment", sa.Text())
    _add("kb_table_meta", "indexes_json", sa.JSON())
    _add("kb_table_meta", "row_count_source", sa.String(16))
    if not _column_exists("kb_table_meta", "description_source"):
        op.add_column(
            "kb_table_meta",
            sa.Column(
                "description_source",
                sa.String(16),
                nullable=False,
                server_default="",
            ),
        )

    # ── kb_column_meta ──────────────────────────────────────────────────
    _add("kb_column_meta", "source_comment", sa.Text())
    _add("kb_column_meta", "key_role", sa.String(16))
    if not _column_exists("kb_column_meta", "description_source"):
        op.add_column(
            "kb_column_meta",
            sa.Column(
                "description_source",
                sa.String(16),
                nullable=False,
                server_default="",
            ),
        )


def downgrade() -> None:
    for table, column in (
        ("kb_table_meta", "description_source"),
        ("kb_table_meta", "row_count_source"),
        ("kb_table_meta", "indexes_json"),
        ("kb_table_meta", "source_comment"),
        ("kb_column_meta", "description_source"),
        ("kb_column_meta", "key_role"),
        ("kb_column_meta", "source_comment"),
    ):
        if _column_exists(table, column):
            op.drop_column(table, column)
