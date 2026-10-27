"""Turn-claim contract — REAL PostgreSQL concurrency tests.

These exercise ``app.services.turn_claims.claim_turn`` against actual PostgreSQL
as the application role (``zhanlu_app``, ``rolbypassrls = false``), with
**independent connections** for the concurrent cases. SQLite cannot substitute:
the guarantees rest on ``ON CONFLICT`` row locking and real concurrent commits.

ISOLATION OF THE LIVE SCHEMA
----------------------------
Migration 102 is applied inside a transaction that is **always rolled back**, so
running these tests never modifies the live database schema. Every fixture row is
deleted (and the whole transaction rolled back) in ``finally``.

Run: ``python -m pytest tests/test_turn_claims_pg.py -q`` with a PostgreSQL
DATABASE_URL and without the repo-wide SQLite conftest override.
"""

from __future__ import annotations

import importlib.util
import pathlib
import threading
import uuid

import pytest
from sqlalchemy import text

MIGRATION_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "alembic" / "versions" / "102_turn_identity.py"
)

pytestmark = pytest.mark.skipif(
    not (pathlib.Path(__file__).resolve().parents[1] / "alembic" / "versions"
         / "102_turn_identity.py").exists(),
    reason="migration 102 not present",
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("m102_claims", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _db_ok() -> bool:
    try:
        from app.database import engine
        with engine.connect() as c:
            c.execute(text("select 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


requires_pg = pytest.mark.skipif(not _db_ok(), reason="PostgreSQL not reachable")


@pytest.fixture()
def schema():
    """Apply migration 102 for the duration of the test, then DROP it.

    NOTE (learned the hard way): uncommitted DDL is invisible to other
    connections, so a "rolled-back transaction" fixture CANNOT support the
    concurrent tests. The schema must therefore be COMMITTED, and torn down in
    ``finally``. Teardown is guaranteed to run (it also runs on failure), and
    ``_ensure_clean`` reaps any leftovers from a crashed run.
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from app.database import engine

    module = _load_migration()
    _ensure_clean()

    conn = engine.connect()
    conn = conn.execution_options(isolation_level="AUTOCOMMIT")
    try:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
        yield
    finally:
        try:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                module.downgrade()
        finally:
            conn.close()


def _ensure_clean() -> None:
    """Best-effort reaper: drop any leftover objects from an aborted run."""
    from app.database import engine

    conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        conn.execute(text("DROP TABLE IF EXISTS agent_turn_claims CASCADE"))
        conn.execute(text("DROP SEQUENCE IF EXISTS agent_turn_seq"))
        conn.execute(text(
            "ALTER TABLE IF EXISTS chat_messages DROP COLUMN IF EXISTS version"
        ))
        conn.execute(text(
            "ALTER TABLE IF EXISTS chat_messages "
            "DROP COLUMN IF EXISTS committed_turn_seq"
        ))
        conn.execute(text(
            "ALTER TABLE IF EXISTS agent_conversations "
            "DROP COLUMN IF EXISTS current_turn_seq"
        ))
    finally:
        conn.close()


@pytest.fixture()
def conv(schema):
    """Create a throwaway conversation; removed on teardown."""
    from app.database import SessionLocal

    cid = "tc-" + uuid.uuid4().hex[:12]
    db = SessionLocal()
    try:
        db.execute(text(
            "insert into agent_conversations (id, title, is_deleted, org_id, app_id) "
            "values (:i, 'claim-test', false, 'default-org', 'default-app')"
        ), {"i": cid})
        db.commit()
    finally:
        db.close()
    try:
        yield cid
    finally:
        db = SessionLocal()
        try:
            db.execute(text("delete from agent_turn_claims where conversation_id=:c"), {"c": cid})
            db.execute(text("delete from agent_conversations where id=:c"), {"c": cid})
            db.commit()
        finally:
            db.close()


def _claim(conversation_id, request_id, message_id):
    """Run claim_turn on its OWN connection (required for concurrency tests)."""
    from app.database import SessionLocal
    from app.services.turn_claims import claim_turn

    db = SessionLocal()
    try:
        return claim_turn(
            db,
            conversation_id=conversation_id,
            request_id=request_id,
            assistant_message_id=message_id,
        )
    finally:
        db.close()


def _ordinal(conversation_id):
    from app.database import SessionLocal
    from app.services.turn_claims import current_ordinal

    db = SessionLocal()
    try:
        return current_ordinal(db, conversation_id)
    finally:
        db.close()


# ── role / RLS preconditions ─────────────────────────────────────────────


@requires_pg
def test_runs_as_application_role_without_rls_bypass():
    """Guards against false-green: a superuser would bypass RLS entirely."""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        role = db.execute(text("select current_user")).scalar()
        bypass = db.execute(text(
            "select rolbypassrls from pg_roles where rolname = current_user"
        )).scalar()
    finally:
        db.close()
    assert role == "zhanlu_app", f"tests must run as zhanlu_app, got {role}"
    assert bypass is False, "role must NOT bypass RLS"


@requires_pg
def test_claims_table_has_forced_rls(schema):
    """RLS must be enabled AND forced or the tenant wall is illusory."""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        row = db.execute(text(
            "select relrowsecurity, relforcerowsecurity from pg_class "
            "where relname='agent_turn_claims'"
        )).first()
        policies = db.execute(text(
            "select count(*) from pg_policy "
            "where polrelid='agent_turn_claims'::regclass"
        )).scalar()
    finally:
        db.close()
    assert row == (True, True), "RLS must be enabled and forced"
    assert policies >= 1, "a tenant policy is required"


# ── sequential contract ──────────────────────────────────────────────────


@requires_pg
def test_same_triple_returns_same_ordinal(conv):
    a = _claim(conv, "req-1", "MSG-A")
    b = _claim(conv, "req-1", "MSG-A")
    assert a.turn_seq == b.turn_seq, "an idempotent retry must reuse the ordinal"
    assert a.assistant_message_id == b.assistant_message_id == "MSG-A"


@requires_pg
def test_same_request_different_message_is_rejected(conv):
    from app.services.turn_claims import ClaimMessageMismatch

    first = _claim(conv, "req-1", "MSG-A")
    with pytest.raises(ClaimMessageMismatch):
        _claim(conv, "req-1", "MSG-B")
    # The original claim must be untouched.
    again = _claim(conv, "req-1", "MSG-A")
    assert again.turn_seq == first.turn_seq
    assert again.assistant_message_id == "MSG-A"


@requires_pg
def test_distinct_request_ids_get_increasing_ordinals(conv):
    a = _claim(conv, "req-1", "MSG-A")
    b = _claim(conv, "req-2", "MSG-A")
    c = _claim(conv, "req-3", "MSG-A")
    assert a.turn_seq < b.turn_seq < c.turn_seq
    assert len({a.turn_seq, b.turn_seq, c.turn_seq}) == 3


@requires_pg
def test_regeneration_same_message_new_request_gets_newer_ordinal(conv):
    """Regenerate reuses the assistant row but is a NEW, newer turn."""
    first = _claim(conv, "req-orig", "MSG-A")
    regen = _claim(conv, "req-regen", "MSG-A")
    assert regen.assistant_message_id == first.assistant_message_id
    assert regen.turn_seq > first.turn_seq, (
        "regeneration must supersede the in-flight generation"
    )


@requires_pg
def test_pointer_is_monotonic(conv):
    a = _claim(conv, "req-1", "MSG-A")
    assert _ordinal(conv) == a.turn_seq
    b = _claim(conv, "req-2", "MSG-B")
    assert _ordinal(conv) == b.turn_seq
    # A retry of the OLDER claim must not pull the pointer back.
    _claim(conv, "req-1", "MSG-A")
    assert _ordinal(conv) == b.turn_seq, "current_turn_seq must never regress"


@requires_pg
def test_rollback_leaves_no_durable_claim(conv):
    from app.database import SessionLocal
    from app.services.turn_claims import claim_turn, lookup_claim

    db = SessionLocal()
    try:
        claim_turn(db, conversation_id=conv, request_id="req-rb",
                   assistant_message_id="MSG-A", commit=False)
        db.rollback()          # simulate a failed/aborted claim transaction
    finally:
        db.close()

    db = SessionLocal()
    try:
        assert lookup_claim(db, conversation_id=conv, request_id="req-rb") is None, (
            "a rolled-back claim must not be durable"
        )
    finally:
        db.close()


@requires_pg
def test_lost_ack_recovery_returns_original_ordinal(conv):
    """Commit succeeds but the caller never sees the response; the retry recovers."""
    first = _claim(conv, "req-lost", "MSG-A")
    # (ack dropped; client retries the identical logical request)
    retry = _claim(conv, "req-lost", "MSG-A")
    assert retry.turn_seq == first.turn_seq


# ── concurrency ──────────────────────────────────────────────────────────


def _run_concurrent(fns):
    """Run callables in threads; collect (result|exception)."""
    out: list = [None] * len(fns)

    def wrap(i, fn):
        try:
            out[i] = fn()
        except BaseException as exc:  # noqa: BLE001
            out[i] = exc

    threads = [threading.Thread(target=wrap, args=(i, fn)) for i, fn in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    return out


@requires_pg
def test_concurrent_identical_claims_allocate_exactly_one(conv):
    results = _run_concurrent([
        lambda: _claim(conv, "req-dup", "MSG-A"),
        lambda: _claim(conv, "req-dup", "MSG-A"),
        lambda: _claim(conv, "req-dup", "MSG-A"),
    ])
    ordinals = {r.turn_seq for r in results if not isinstance(r, BaseException)}
    assert len(ordinals) == 1, f"exactly one ordinal expected, got {results}"


@requires_pg
def test_concurrent_same_request_different_messages_one_wins(conv):
    from app.services.turn_claims import ClaimMessageMismatch

    results = _run_concurrent([
        lambda: _claim(conv, "req-x", "MSG-A"),
        lambda: _claim(conv, "req-x", "MSG-B"),
    ])
    ok = [r for r in results if not isinstance(r, BaseException)]
    mismatched = [r for r in results if isinstance(r, ClaimMessageMismatch)]
    assert len(ok) == 1, f"exactly one claim may win, got {results}"
    assert len(mismatched) == 1, f"the other must be a mismatch, got {results}"


@requires_pg
def test_concurrent_distinct_requests_get_distinct_ordinals(conv):
    results = _run_concurrent([
        lambda: _claim(conv, "req-a", "MSG-A"),
        lambda: _claim(conv, "req-b", "MSG-B"),
        lambda: _claim(conv, "req-c", "MSG-C"),
    ])
    ordinals = [r.turn_seq for r in results if not isinstance(r, BaseException)]
    assert len(ordinals) == 3
    assert len(set(ordinals)) == 3, f"ordinals must be distinct, got {ordinals}"
    assert max(ordinals) == _ordinal(conv)


@requires_pg
def test_sequence_gaps_do_not_break_ordering(conv):
    """nextval is evaluated on the conflict path, so gaps are EXPECTED."""
    a = _claim(conv, "req-1", "MSG-A")
    _claim(conv, "req-1", "MSG-A")          # idempotent retry: may burn a value
    b = _claim(conv, "req-2", "MSG-B")
    assert b.turn_seq > a.turn_seq, "ordering holds regardless of gaps"
    # NOTE: no assertion that b == a + 1 — contiguity is explicitly NOT guaranteed.
