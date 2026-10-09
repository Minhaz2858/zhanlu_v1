"""Turn-health monitor tests (empty-bubble root-fix layer 3)."""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///file::memory:?cache=shared&uri=true")

import pytest  # noqa: E402

from app.database import Base, SessionLocal, engine  # noqa: E402
import app.models  # noqa: F401, E402
from app.models.chat_message import ChatMessage  # noqa: E402
from app.models.chat_session import ChatSession  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.turn_health_monitor import scan_recent_turns  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _tables():
    Base.metadata.create_all(engine)


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _mk_session(db) -> str:
    existing = db.query(User).filter_by(email="admin@zhanlu.dev").first()
    if existing is None:
        existing = User(
            email="admin@zhanlu.dev", full_name="Admin", password_hash="x",
            role="admin", org_id="default-org",
        )
        db.add(existing)
        db.commit()
    sess = ChatSession(
        title="t", agent_name="general_assistant",
        created_by_id=existing.id, project_id=None,
    )
    db.add(sess)
    db.commit()
    return sess.id


def _mk_assistant(db, session_id: str, content: str, order: int) -> ChatMessage:
    row = ChatMessage(
        session_id=session_id, role="assistant", content=content, order=order,
    )
    db.add(row)
    db.commit()
    return row


def test_healthy_turns_no_breach(db):
    sid = _mk_session(db)
    for i in range(6):
        _mk_assistant(db, sid, f"A perfectly fine answer {i} with detail.", i)
    out = scan_recent_turns(db)
    assert out["flagged"] == 0
    assert out["threshold_breached"] is False


def test_fallback_apology_and_empty_classification(db):
    """The scan is global over the shared DB, so seed one session with a
    known mix and assert exact counts (test_healthy_turns added 6 healthy
    rows before this)."""
    sid = _mk_session(db)
    rows = [
        "Here is a proper answer with data.",               # healthy
        "I gathered some information but had trouble "       # fallback x4
        "putting it all together. Try again more specifically (1).",
        "I gathered some information but had trouble "
        "putting it all together. Try again more specifically (2).",
        "I gathered some information but had trouble "
        "putting it all together. Try again more specifically (3).",
        "I gathered some information but had trouble "
        "putting it all together. Try again more specifically (4).",
        "I was unable to synthesize the results.",           # apology
        "正常的中文回答，内容充实。",                              # healthy
        "",                                                  # empty → ignored
    ]
    for i, c in enumerate(rows):
        _mk_assistant(db, sid, c, i)

    out = scan_recent_turns(db)
    # 6 healthy (earlier test) + 8 here = 14 non-empty; 4 fallback + 1 apology flagged
    assert out["flagged"] == 5
    assert out["threshold_breached"] is True
    assert out["examples"], "examples must reference flagged rows"


def test_never_raises_on_garbage_db(db):
    class _Boom:
        def query(self, *a, **kw):
            raise RuntimeError("boom")

    out = scan_recent_turns(_Boom())
    assert "error" in out


def test_monitor_cycle_respects_flag(monkeypatch):
    import asyncio

    from app.config import settings
    from app.services.turn_health_monitor import monitor_cycle

    monkeypatch.setattr(settings, "TURN_HEALTH_MONITOR_ENABLED", False)
    out = asyncio.run(monitor_cycle())
    assert out == {"enabled": False}
