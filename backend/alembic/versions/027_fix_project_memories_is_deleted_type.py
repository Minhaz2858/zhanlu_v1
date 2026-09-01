"""Fix project_memories.is_deleted to BOOLEAN.

Model/schema drift: ``app.models.base.TimestampedBase.is_deleted`` is
declared as ``Mapped[bool]`` (Postgres BOOLEAN) and every other
``*_is_deleted`` column in the schema is BOOLEAN, but
``project_memories.is_deleted`` was created as INTEGER by migration 021
(``add project memory table``). Every ``WHERE ... is_deleted = false``
query against this table raised::

    psycopg2.errors.UndefinedFunction: operator does not exist: integer = boolean

This silently broke ``prepare_data_source_runtime`` (the ProjectMemory
lookup is wrapped in try/except, but the failed query left the session
in an aborted transaction so the subsequent KB-load query also failed).
The end-user symptom: a chat in a project with a connected database
saying "no data sources bound" even though the project KB was correctly
bound.

The migration is idempotent: it skips when the column is already BOOLEAN
so it can be re-run safely on databases that were patched by hand.

Revision ID: 027
Revises: 026
Create Date: 2026-07-24
"""

from alembic import op
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision = "027"
down_revision = "026"
branch_labels = None
depends_on = None


def _is_already_boolean(conn) -> bool:
    """True if ``project_memories.is_deleted`` is already BOOLEAN."""
    row = conn.execute(
        text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'project_memories' "
            "AND column_name = 'is_deleted'"
        )
    ).fetchone()
    return bool(row and row[0] == "boolean")


def upgrade() -> None:
    conn = op.get_bind()
    if _is_already_boolean(conn):
        # Already fixed (e.g. a hot-patch ran ALTER directly). Nothing to do.
        return
    # The column was created as INTEGER with default 0 in migration 021.
    # Drop the integer default, cast to BOOLEAN, then re-set a boolean
    # default. Doing the cast via CASE is portable across Postgres
    # versions that don't accept ``USING <int>::boolean`` directly.
    op.execute("ALTER TABLE project_memories ALTER COLUMN is_deleted DROP DEFAULT")
    op.execute(
        "ALTER TABLE project_memories "
        "ALTER COLUMN is_deleted TYPE BOOLEAN "
        "USING CASE WHEN is_deleted = 0 THEN FALSE ELSE TRUE END"
    )
    op.execute("ALTER TABLE project_memories ALTER COLUMN is_deleted SET DEFAULT FALSE")


def downgrade() -> None:
    conn = op.get_bind()
    if not _is_already_boolean(conn):
        # Already integer — nothing to revert.
        return
    # Cast back to INTEGER so an old code revision that compares against
    # integers can still query. Drop the boolean default first.
    op.execute("ALTER TABLE project_memories ALTER COLUMN is_deleted DROP DEFAULT")
    op.execute(
        "ALTER TABLE project_memories "
        "ALTER COLUMN is_deleted TYPE INTEGER "
        "USING CASE WHEN is_deleted THEN 1 ELSE 0 END"
    )
    op.execute("ALTER TABLE project_memories ALTER COLUMN is_deleted SET DEFAULT 0")
