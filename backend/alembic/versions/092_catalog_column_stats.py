"""Catalog column statistics — the measured distribution of a column's data.

Revision ID: 092_catalog_column_stats
Revises: 091_catalog_structure_truth
Create Date: 2026-09-20

The structure pass (091) collected what a source DECLARES. Nothing collected
what the data IS, so a catalog entry could say ``FSTATUS varchar(10)`` but not
that the column holds one of three codes, or that a measure column is 97 % NULL.
Three code paths already read that information and always found it empty:
``data_source_runtime._load_catalog_meta`` (answer-verification catalog
validation), ``domain_discovery._cluster_digest`` and
``semantic_store.sample_values``.

Adds:

- ``kb_column_meta.stats_json`` (JSON) — per-column measurement from a bounded
  row sample: ``rows_sampled``, ``basis`` ('full' | 'sample'), ``nulls``,
  ``null_rate``, ``distinct``, ``distinct_is_exact``, ``top_values``
  ``[{"value","count"}]``, plus type-dependent ``min``/``max``/``avg``/``zeros``
  (numeric) or ``min_len``/``max_len`` (string). An ABSENT key means "not
  measured" — never 0 and never [].

``kb_column_meta.sample_values`` and ``kb_table_meta.sample_values`` already
exist and are now populated by the indexer rather than always NULL.

Schema only — no data backfill: statistics are collected on the next index of
each source, because a stored value that no pass measured would be a
fabrication. Idempotent: column existence check first (repo convention).
Mirrored in ``main.py::_specific_migrations`` for deployments that never run
alembic.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "092_catalog_column_stats"
down_revision = "091_catalog_structure_truth"
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return False
    existing = {c["name"] for c in inspector.get_columns(table)}
    return column in existing


def upgrade() -> None:
    if not _column_exists("kb_column_meta", "stats_json"):
        op.add_column(
            "kb_column_meta", sa.Column("stats_json", sa.JSON(), nullable=True)
        )


def downgrade() -> None:
    if _column_exists("kb_column_meta", "stats_json"):
        op.drop_column("kb_column_meta", "stats_json")
