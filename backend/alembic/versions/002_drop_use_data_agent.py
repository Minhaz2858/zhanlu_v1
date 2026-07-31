"""Drop `use_data_agent` key from agent_apps.tool_config JSON.

Revision ID: 002
Revises: 001
Create Date: 2026-07-11

The runtime no longer reads `tool_config.use_data_agent` — the Data
Agent is always-on. This migration removes the key from every existing
`agent_apps.tool_config` JSON row so the DB is consistent with the new
contract.

Dialect notes
-------------
- SQLite: JSON is stored as TEXT, so we rewrite the column by parsing
  and re-serializing in Python.
- PostgreSQL: JSON is a real type; we use `jsonb - 'key'`.
- MySQL: JSON type, `JSON_REMOVE(col, '$.key')`.

We use SQLAlchemy's dialect detection and run the appropriate UPDATE
for the target dialect.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "sqlite":
        # SQLite stores JSON as TEXT. Use json_extract/json_set to remove
        # the key from any object that contains it. We guard with a
        # json_type check to avoid touching rows that don't have it.
        op.execute(
            sa.text(
                """
                UPDATE agent_apps
                SET tool_config = json_remove(tool_config, '$.use_data_agent')
                WHERE tool_config IS NOT NULL
                  AND json_type(tool_config, '$.use_data_agent') IS NOT NULL
                """
            )
        )
    elif dialect == "postgresql":
        # Cast json to jsonb for operator support
        op.execute(
            sa.text(
                """
                UPDATE agent_apps
                SET tool_config = (tool_config::jsonb - 'use_data_agent')::json
                WHERE tool_config::jsonb ? 'use_data_agent'
                """
            )
        )
    elif dialect == "mysql":
        op.execute(
            sa.text(
                """
                UPDATE agent_apps
                SET tool_config = JSON_REMOVE(tool_config, '$.use_data_agent')
                WHERE JSON_CONTAINS_PATH(tool_config, 'one', '$.use_data_agent')
                """
            )
        )
    else:
        # Unknown dialect — skip silently. The runtime ignores the key
        # either way, so no data corruption occurs.
        pass


def downgrade() -> None:
    """No-op downgrade.

    We do not restore the key on downgrade. The runtime ignores the
    field, so the DB staying free of the key is safe regardless of
    which revision is current.
    """
    pass
