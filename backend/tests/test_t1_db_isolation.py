"""T1 — test database isolation verification (2026-10-31).

Proves the conftest T1 contract:
  1. Sessions get INDEPENDENT connections on a real (file-backed)
     SQLite database — one session's uncommitted writes are invisible
     to another session, and never committable by it. Under the old
     StaticPool setup this exact pattern was the diagnosed root cause
     of order-dependent combined-run flakiness.
  2. The module-boundary reset (``_reset_database``) truncates every
     table and re-seeds the admin user — the mechanism behind module
     order independence.
  3. No StaticPool anywhere on the global engine; the database is a
     real file, not the shared-cache ":memory:" URI.
"""

from __future__ import annotations

import os
import sys
import uuid

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.database import SessionLocal, engine  # noqa: E402
from app.models.llm_model import LlmModel  # noqa: E402
from app.services.crypto_utils import encrypt_value  # noqa: E402
from tests.conftest import _reset_database  # noqa: E402


def _mk_model(org_id: str) -> LlmModel:
    row_id = f"llm-t1-{uuid.uuid4().hex[:8]}"
    return LlmModel(
        id=row_id,
        name=f"t1-{row_id[-4:]}",
        model_id=f"model-t1-{row_id[-4:]}",
        provider="openai_compatible",
        base_url="https://t1.example.invalid/v1",
        api_key=encrypt_value("sk-t1-key"),
        org_id=org_id,
        app_id="app-t1",
        enabled=True,
        is_deleted=False,
    )


def test_engine_uses_independent_connections_not_staticpool():
    """Contract 1: independent connections, real file DB, no StaticPool."""
    from sqlalchemy.pool import StaticPool

    assert not isinstance(engine.pool, StaticPool)
    url = str(engine.url)
    assert ":memory:" not in url and "cache=shared" not in url, (
        f"test database must be a real file, got {url}"
    )
    assert engine.url.database and os.path.exists(engine.url.database), (
        "the T1 temp-file test database does not exist on disk"
    )


def test_uncommitted_writes_are_invisible_to_independent_sessions():
    """The core isolation property the old StaticPool violated: session 1
    inserts WITHOUT committing; session 2 (its own connection) must see
    nothing. After session 1 commits, session 2 must see it — and the
    commit must not have swept in any second session's pending writes."""
    org = f"org-t1-{uuid.uuid4().hex[:8]}"
    s1, s2 = SessionLocal(), SessionLocal()
    try:
        assert s1 is not s2
        s1.add(_mk_model(org))
        s1.flush()  # SQL sent on session 1's connection, NOT committed

        # Session 2: independent connection + transaction — must not see
        # the uncommitted row (under StaticPool both "sessions" shared
        # one connection and this assertion could not even be expressed).
        assert s2.query(LlmModel).filter(
            LlmModel.org_id == org
        ).first() is None, (
            "an uncommitted write leaked into an independent session — "
            "connections are not isolated"
        )

        s1.commit()
        s2.expire_all()  # force a fresh transaction on s2's connection
        assert s2.query(LlmModel).filter(
            LlmModel.org_id == org
        ).first() is not None, "a committed write never became visible"

        # Cleanup
        s2.query(LlmModel).filter(LlmModel.org_id == org).delete(
            synchronize_session=False
        )
        s2.commit()
    finally:
        s1.close()
        s2.close()


def test_module_reset_truncates_all_tables_and_reseeds_admin():
    """Contract 2: the module-boundary reset wipes tenant/test rows and
    restores the exact bootstrap state (schema + admin user only)."""
    from app.models.user import User

    org = f"org-t1-{uuid.uuid4().hex[:8]}"
    db = SessionLocal()
    try:
        db.add(_mk_model(org))
        db.commit()
        assert db.query(LlmModel).filter(
            LlmModel.org_id == org
        ).first() is not None

        _reset_database()

        assert db.query(LlmModel).filter(
            LlmModel.org_id == org
        ).first() is None, "the module reset did not truncate test rows"
        assert db.query(User).filter(
            User.email == "admin@zhanlu.dev"
        ).first() is not None, "the module reset did not re-seed the admin"
    finally:
        db.close()
        # Leave the DB clean for subsequent modules regardless of outcome.
        _reset_database()


def test_commit_of_one_session_never_sweeps_another_sessions_pending_writes():
    """The interleaving failure mode: with one shared DBAPI connection,
    session B's commit flushed and committed session A's uncommitted
    rows. With independent connections each transaction has its own fate:
    A's rollback discards only A's pending write; B's committed write is
    untouched by A's rollback; and B's commit could never have swept A's
    pending row (separate connections — SQLite even refuses a second
    writer while A's write transaction is open, see the next test)."""
    org_a = f"org-t1-a-{uuid.uuid4().hex[:8]}"
    org_b = f"org-t1-b-{uuid.uuid4().hex[:8]}"
    sa, sb = SessionLocal(), SessionLocal()
    try:
        # B commits first — its row is durable on B's connection.
        sb.add(_mk_model(org_b))
        sb.commit()

        # A starts an independent pending write.
        sa.add(_mk_model(org_a))
        sa.flush()

        sa.rollback()  # A's fate decided by A's transaction alone
        sa.expire_all()
        assert sa.query(LlmModel).filter(
            LlmModel.org_id == org_a
        ).first() is None, "A's rollback left its own pending row behind"
        assert sa.query(LlmModel).filter(
            LlmModel.org_id == org_b
        ).first() is not None, "session B's committed write vanished"

        sb.query(LlmModel).filter(LlmModel.org_id == org_b).delete(
            synchronize_session=False
        )
        sb.commit()
    finally:
        sa.close()
        sb.close()


def test_concurrent_write_transactions_cannot_silently_interleave():
    """The positive proof of independence: while session A holds an open
    write transaction, session B's write CANNOT join it. Under the old
    StaticPool both sessions shared one connection and silently
    interleaved statements — the diagnosed root cause of combined-run
    corruption. With independent connections SQLite serializes writers,
    so B's write fails with 'database is locked' until A resolves its
    transaction."""
    from sqlalchemy.exc import OperationalError

    org_a = f"org-t1-a-{uuid.uuid4().hex[:8]}"
    org_b = f"org-t1-b-{uuid.uuid4().hex[:8]}"
    sa, sb = SessionLocal(), SessionLocal()
    try:
        sa.add(_mk_model(org_a))
        sa.flush()  # A's write transaction now holds the file write lock

        blocked = False
        try:
            sb.add(_mk_model(org_b))
            sb.flush()
        except OperationalError as exc:
            assert "database is locked" in str(exc), (
                f"unexpected sqlite error: {exc}"
            )
            blocked = True
            sb.rollback()
        assert blocked, (
            "a second session wrote into the first session's open write "
            "transaction — connections are shared, not independent"
        )

        sa.rollback()  # release the lock
        # B can now write on its own connection.
        sb.add(_mk_model(org_b))
        sb.commit()
        sa.expire_all()
        assert sa.query(LlmModel).filter(
            LlmModel.org_id == org_b
        ).first() is not None

        sb.query(LlmModel).filter(LlmModel.org_id == org_b).delete(
            synchronize_session=False
        )
        sb.commit()
    finally:
        sa.close()
        sb.close()
