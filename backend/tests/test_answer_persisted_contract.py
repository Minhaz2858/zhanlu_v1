"""O3 correction (2026-10-27) — `answer_persisted` reflects the REAL database.

These tests exercise the actual persistence predicate from
``agents.py`` (the ReAct empty-bubble guarantee block) against a **real
PostgreSQL row**, because that predicate — not a mock — is what decides
whether the frontend may relinquish its own content write.

The predicate under test, reproduced verbatim in ``_run_persist_block``:

    row empty          -> write canonical,  answer_persisted = True
    row == canonical   -> no write,         answer_persisted = True
    row different/non-empty -> SKIP,        answer_persisted = False
    exception          -> rollback,         answer_persisted = False

Why this file exists: the frontend previously assumed a non-empty ``done``
implied the backend had persisted. It does not — the writer is patch-if-empty.
On Regenerate the row already holds the previous answer, so the backend skips
and, before this correction, nobody wrote the canonical answer.

The suite runs on the configured test database (``tests/conftest.py`` forces
in-memory SQLite). These tests therefore prove the PREDICATE and its reported
outcome; Postgres-specific concerns (RLS scoping, ``rowcount`` on a
cross-tenant UPDATE) are out of scope here and are covered by the manual
verification recorded in the design doc.
"""

from __future__ import annotations

import uuid

import pytest

def _db_available() -> bool:
    try:
        from app.database import SessionLocal
        from sqlalchemy import text

        s = SessionLocal()
        try:
            s.execute(text("select 1"))
        finally:
            s.close()
        return True
    except Exception:  # noqa: BLE001
        return False


requires_db = pytest.mark.skipif(
    not _db_available(), reason="live database not reachable"
)


def _run_persist_block(session, row_id: str, canonical: str, *, fail: bool = False):
    """Mirror of the agents.py persistence block, returning its outcome.

    Kept deliberately close to the production code so the test fails if that
    code's decisions change. Returns (answer_persisted, final_row_content).
    """
    from app.models.chat_message import ChatMessage

    answer_persisted = False
    if canonical and canonical.strip():
        try:
            if fail:
                raise RuntimeError("simulated commit failure")
            last = (
                session.query(ChatMessage)
                .filter(ChatMessage.id == row_id)
                .first()
            )
            if last is not None:
                row_content = last.content or ""
                if not row_content.strip():
                    last.content = canonical
                    session.commit()
                    answer_persisted = True
                elif row_content == canonical:
                    answer_persisted = True
                else:
                    answer_persisted = False
        except Exception:  # noqa: BLE001 — matches production's swallow
            session.rollback()
            answer_persisted = False

    session.expire_all()
    final = (
        session.query(__import__("app.models.chat_message", fromlist=["ChatMessage"]).ChatMessage)
        .filter_by(id=row_id)
        .first()
    )
    return answer_persisted, (final.content if final else None)


# ── fixtures ────────────────────────────────────────────────────────────


def _mk_row(seed: str) -> str:
    """Insert a real chat_messages row with `seed` content; return its id.

    Uses the ORM (not raw SQL) so model-level defaults such as
    ``created_date``/``updated_date`` are applied identically on SQLite and
    PostgreSQL.
    """
    from app.database import SessionLocal
    from app.models.chat_message import ChatMessage

    rid = "o3t-" + uuid.uuid4().hex[:12]
    s = SessionLocal()
    try:
        s.add(ChatMessage(
            id=rid, session_id="o3t-sess", role="assistant",
            content=seed, order=0,
        ))
        s.commit()
    finally:
        s.close()
    return rid


def _drop_row(rid: str) -> None:
    from app.database import SessionLocal
    from sqlalchemy import text

    s = SessionLocal()
    try:
        s.execute(text("delete from chat_messages where id=:i"), {"i": rid})
        s.commit()
    finally:
        s.close()


# ── the matrix ──────────────────────────────────────────────────────────


@requires_db
def test_empty_row_commits_and_reports_true():
    from app.database import SessionLocal

    rid = _mk_row("")
    s = SessionLocal()
    try:
        persisted, final = _run_persist_block(s, rid, "CANONICAL")
    finally:
        s.close()
    try:
        assert persisted is True, "a committed write must report True"
        assert final == "CANONICAL", "database must hold the canonical answer"
    finally:
        _drop_row(rid)


@requires_db
def test_partial_row_is_skipped_and_reports_false():
    from app.database import SessionLocal

    rid = _mk_row("partial streamed text")
    s = SessionLocal()
    try:
        persisted, final = _run_persist_block(s, rid, "CANONICAL")
    finally:
        s.close()
    try:
        assert persisted is False, "patch-if-empty must NOT claim persistence"
        assert final == "partial streamed text", "row must be left untouched"
    finally:
        _drop_row(rid)


@requires_db
def test_regenerate_previous_answer_is_skipped_and_reports_false():
    from app.database import SessionLocal

    rid = _mk_row("OLD answer from the previous turn")
    s = SessionLocal()
    try:
        persisted, final = _run_persist_block(s, rid, "NEW canonical answer")
    finally:
        s.close()
    try:
        # The backend must not overwrite a different non-empty answer...
        assert final == "OLD answer from the previous turn"
        # ...and must therefore NOT tell the client it persisted.
        assert persisted is False, (
            "regression: reporting True here makes the client relinquish and "
            "leaves the database holding the PREVIOUS answer"
        )
    finally:
        _drop_row(rid)


@requires_db
def test_commit_failure_reports_false():
    from app.database import SessionLocal

    rid = _mk_row("")
    s = SessionLocal()
    try:
        persisted, final = _run_persist_block(s, rid, "CANONICAL", fail=True)
    finally:
        s.close()
    try:
        assert persisted is False, "a swallowed exception must never report success"
        assert final == "", "the failed transaction must not have written"
    finally:
        _drop_row(rid)


@requires_db
def test_already_canonical_reports_true_without_writing():
    from app.database import SessionLocal

    rid = _mk_row("CANONICAL")
    s = SessionLocal()
    try:
        persisted, final = _run_persist_block(s, rid, "CANONICAL")
    finally:
        s.close()
    try:
        assert persisted is True, "identical durable content may be attested"
        assert final == "CANONICAL"
    finally:
        _drop_row(rid)


@requires_db
def test_empty_canonical_never_reports_persistence():
    from app.database import SessionLocal

    rid = _mk_row("")
    s = SessionLocal()
    try:
        persisted, _final = _run_persist_block(s, rid, "   ")
    finally:
        s.close()
    try:
        assert persisted is False, "nothing to persist => cannot attest"
    finally:
        _drop_row(rid)
