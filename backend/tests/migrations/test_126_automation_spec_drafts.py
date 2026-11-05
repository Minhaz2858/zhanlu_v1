"""Migration 126 — automation drafts + spec columns + confirm arbiter index.

Exercises the migration's upgrade()/downgrade() directly against a scratch
SQLite database (the full alembic chain contains PG-only RLS SQL in earlier
revisions, so a whole-chain `alembic upgrade head` on SQLite is not
representative; the chain-linearity check is asserted separately).

Verifies:
1. Revision metadata chains 126 -> 125_agent_run_provenance.
2. upgrade(): automation_drafts created with the §1.1 columns; spec /
   spec_version / confirmation_key added to automation_tasks; the partial
   unique arbiter index enforces one live confirmation_key per org and
   ignores soft-deleted rows.
3. The migration is idempotent under a partial deployment (re-run upgrade
   is a no-op, not a crash).
4. downgrade(): everything removed cleanly.
"""

import importlib.util
import os
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import (
    create_engine,
    inspect,
    text,
)
from alembic.runtime.migration import MigrationContext
from alembic.operations import Operations

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

# alembic/versions is not a package — load the migration module by path.
_MIGRATION_PATH = (
    Path(_BACKEND_ROOT) / "alembic" / "versions" / "126_automation_spec_drafts.py"
)
_spec = importlib.util.spec_from_file_location(
    "migration_126_automation_spec_drafts", _MIGRATION_PATH
)
migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migration)


@pytest.fixture
def scratch():
    """Scratch SQLite with a minimal legacy automation_tasks table."""
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE automation_tasks (
                    id VARCHAR(36) PRIMARY KEY,
                    org_id VARCHAR(36) NOT NULL DEFAULT 'default-org',
                    name VARCHAR(255) NOT NULL,
                    is_deleted BOOLEAN NOT NULL DEFAULT 0
                )
                """
            )
        )
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            migration.upgrade()
    yield engine
    engine.dispose()


def test_revision_chains_from_125():
    assert migration.down_revision == "125_agent_run_provenance"
    assert migration.revision == "126_automation_spec_drafts"


def test_upgrade_creates_drafts_and_columns(scratch):
    insp = inspect(scratch)
    assert "automation_drafts" in insp.get_table_names()
    draft_cols = {c["name"] for c in insp.get_columns("automation_drafts")}
    for expected in (
        "id", "org_id", "spec", "spec_version", "status", "confirmation_key",
        "origin_session_id", "origin_message_id", "validation_errors",
        "expires_at", "confirmed_definition_id",
    ):
        assert expected in draft_cols, f"automation_drafts missing {expected}"

    task_cols = {c["name"] for c in insp.get_columns("automation_tasks")}
    assert {"spec", "spec_version", "confirmation_key"} <= task_cols

    idx_names = {i["name"] for i in insp.get_indexes("automation_tasks")}
    assert "uq_automation_tasks_org_confirmation_key" in idx_names


def test_arbiter_index_enforces_uniqueness_per_org(scratch):
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=scratch)
    s = Session()
    try:
        s.execute(
            text(
                "INSERT INTO automation_tasks (id, org_id, name, is_deleted, "
                "confirmation_key) VALUES (:i, :o, :n, 0, :k)"
            ),
            [{"i": str(uuid.uuid4()), "o": "org-a", "n": "first", "k": "key-1"}],
        )
        s.commit()
        # Same org, same key -> rejected.
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            s.execute(
                text(
                    "INSERT INTO automation_tasks (id, org_id, name, is_deleted, "
                    "confirmation_key) VALUES (:i, :o, :n, 0, :k)"
                ),
                [{"i": str(uuid.uuid4()), "o": "org-a", "n": "dup", "k": "key-1"}],
            )
        s.rollback()
        # Soft-deleted row frees the key.
        s.execute(
            text(
                "UPDATE automation_tasks SET is_deleted = 1 WHERE confirmation_key = 'key-1'"
            )
        )
        s.commit()
        s.execute(
            text(
                "INSERT INTO automation_tasks (id, org_id, name, is_deleted, "
                "confirmation_key) VALUES (:i, :o, :n, 0, :k)"
            ),
            [{"i": str(uuid.uuid4()), "o": "org-a", "n": "after-delete", "k": "key-1"}],
        )
        s.commit()
    finally:
        s.close()


def test_upgrade_is_idempotent(scratch):
    """Partial-deployment safety: re-running upgrade() must not crash."""
    with scratch.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            migration.upgrade()  # no exception
    insp = inspect(scratch)
    assert "automation_drafts" in insp.get_table_names()


def test_downgrade_removes_everything(scratch):
    with scratch.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            migration.downgrade()
    insp = inspect(scratch)
    assert "automation_drafts" not in insp.get_table_names()
    task_cols = {c["name"] for c in insp.get_columns("automation_tasks")}
    assert not {"spec", "spec_version", "confirmation_key"} & task_cols
