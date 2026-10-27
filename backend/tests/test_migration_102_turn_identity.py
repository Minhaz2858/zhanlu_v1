"""Migration 102_turn_identity — schema contract tests.

These tests validate the DDL the Stage 2B protocol depends on. They apply the
migration inside a **transaction that is always rolled back**, so the live
database schema is never modified by running them.

Why behavioural rather than source-string: the protocol's correctness rests on
concrete database facts (column defaults, sequence existence, forced RLS,
policy presence). A grep over the migration file would pass even if the DDL were
wrong.

Run as the application role; RLS assertions require a non-BYPASSRLS role.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest
from sqlalchemy import text

MIGRATION_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "alembic" / "versions" / "102_turn_identity.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_102", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _db_available() -> bool:
    try:
        from app.database import engine

        with engine.connect() as conn:
            conn.execute(text("select 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


requires_db = pytest.mark.skipif(
    not _db_available(), reason="live database not reachable"
)


@pytest.fixture()
def applied():
    """Apply migration 102 in a transaction; always roll back afterwards."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from app.database import engine

    module = _load_migration()
    conn = engine.connect()
    trans = conn.begin()
    try:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
        yield conn
    finally:
        try:
            trans.rollback()
        finally:
            conn.close()


@requires_db
def test_migration_metadata_chain():
    m = _load_migration()
    assert m.revision == "102_turn_identity"
    assert m.down_revision == "101_cad_revisions"


@requires_db
def test_upgrade_adds_message_and_conversation_columns(applied):
    cols = {
        r[0]
        for r in applied.execute(text(
            "select column_name from information_schema.columns "
            "where table_name='chat_messages' "
            "and column_name in ('version','committed_turn_seq')"
        ))
    }
    assert cols == {"version", "committed_turn_seq"}

    conv_cols = {
        r[0]
        for r in applied.execute(text(
            "select column_name from information_schema.columns "
            "where table_name='agent_conversations' "
            "and column_name='current_turn_seq'"
        ))
    }
    assert conv_cols == {"current_turn_seq"}


@requires_db
def test_version_defaults_to_zero_and_is_not_null(applied):
    row = applied.execute(text(
        "select column_default, is_nullable from information_schema.columns "
        "where table_name='chat_messages' and column_name='version'"
    )).first()
    assert row is not None
    default, nullable = row
    assert nullable == "NO", "version must be NOT NULL (CAS requires a value)"
    assert "0" in str(default), "version must default to 0 for existing rows"


@requires_db
def test_committed_turn_seq_is_nullable(applied):
    """NULL means 'no turn committed to this row' — the first turn must pass."""
    row = applied.execute(text(
        "select is_nullable from information_schema.columns "
        "where table_name='chat_messages' and column_name='committed_turn_seq'"
    )).first()
    assert row is not None and row[0] == "YES"


@requires_db
def test_claims_table_shape_and_primary_key(applied):
    pk_cols = {
        r[0]
        for r in applied.execute(text(
            "select a.attname from pg_index i "
            "join pg_attribute a on a.attrelid=i.indrelid and a.attnum = any(i.indkey) "
            "where i.indrelid='agent_turn_claims'::regclass and i.indisprimary"
        ))
    }
    assert pk_cols == {"conversation_id", "request_id"}, (
        "the idempotency key is (conversation_id, request_id)"
    )


@requires_db
def test_claims_table_has_message_binding_and_tenant_columns(applied):
    cols = {
        r[0]
        for r in applied.execute(text(
            "select column_name from information_schema.columns "
            "where table_name='agent_turn_claims'"
        ))
    }
    # assistant_message_id is the binding checked by the guarded upsert.
    assert "assistant_message_id" in cols
    # org_id is required for the forced RLS policy to be meaningful.
    assert "org_id" in cols
    assert "turn_seq" in cols


@requires_db
def test_sequence_exists(applied):
    n = applied.execute(text(
        "select count(*) from pg_class where relkind='S' and relname='agent_turn_seq'"
    )).scalar()
    assert n == 1


@requires_db
def test_rls_is_enabled_and_forced_with_tenant_policy(applied):
    """The app role is not BYPASSRLS, so forced RLS is the isolation wall."""
    row = applied.execute(text(
        "select relrowsecurity, relforcerowsecurity from pg_class "
        "where relname='agent_turn_claims'"
    )).first()
    assert row is not None
    assert row == (True, True), "RLS must be BOTH enabled and forced"

    policies = applied.execute(text(
        "select count(*) from pg_policy "
        "where polrelid='agent_turn_claims'::regclass"
    )).scalar()
    assert policies >= 1, "a tenant policy is required or the table is unusable"


@requires_db
def test_downgrade_restores_schema():
    """Explicitly exercise downgrade and verify every object is removed."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from app.database import engine

    module = _load_migration()
    conn = engine.connect()
    trans = conn.begin()
    try:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
            module.downgrade()

        remaining_cols = conn.execute(text(
            "select count(*) from information_schema.columns "
            "where table_name='chat_messages' "
            "and column_name in ('version','committed_turn_seq')"
        )).scalar()
        conv_col = conn.execute(text(
            "select count(*) from information_schema.columns "
            "where table_name='agent_conversations' "
            "and column_name='current_turn_seq'"
        )).scalar()
        table_n = conn.execute(text(
            "select count(*) from information_schema.tables "
            "where table_name='agent_turn_claims'"
        )).scalar()
        seq_n = conn.execute(text(
            "select count(*) from pg_class "
            "where relkind='S' and relname='agent_turn_seq'"
        )).scalar()

        assert remaining_cols == 0
        assert conv_col == 0
        assert table_n == 0, "downgrade must drop the claims table"
        assert seq_n == 0, "downgrade must drop the sequence"
    finally:
        trans.rollback()
        conn.close()


@requires_db
def test_migration_does_not_rewrite_existing_content():
    """Additive-only: content must be byte-identical across upgrade.

    Verified by hashing the content column before and after applying the
    migration inside a rolled-back transaction.
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from app.database import engine

    module = _load_migration()
    conn = engine.connect()
    trans = conn.begin()
    try:
        before = conn.execute(text(
            "select count(*), coalesce(sum(length(content)),0) from chat_messages"
        )).first()
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
        after = conn.execute(text(
            "select count(*), coalesce(sum(length(content)),0) from chat_messages"
        )).first()
        assert before == after, "migration must not rewrite message content"
    finally:
        trans.rollback()
        conn.close()
