"""Conversation <-> dashboard binding: column exists + 1:1 FK semantics."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import inspect as sa_inspect


def test_agent_conversation_has_dashboard_id_column(tmp_path):
    # Fresh sqlite DB to inspect the column set.
    db_file = tmp_path / "t.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{db_file}"
    from app.database import Base, engine
    import app.models  # noqa: registers all mappers
    Base.metadata.create_all(engine)
    cols = {c["name"] for c in sa_inspect(engine).get_columns("agent_conversations")}
    assert "dashboard_id" in cols


def test_create_dashboard_binds_to_conversation(tmp_path):
    """_create_dashboard sets AgentConversation.dashboard_id when context has conversation_id.

    The handler is unit-tested DIRECTLY (not via the agent loop / HTTP) because
    wiring a full agent turn is out of scope for this task, and the seeded user
    has a dummy password_hash so /api/auth/login would 401. We call the async
    handler via asyncio.run() on a dedicated SessionLocal against a fresh
    per-test sqlite DB.

    We build a PRIVATE engine + SessionLocal bound to the tmp DB file (rather
    than reloading app.config/app.database, which would create a NEW Declarative
    Base whose metadata has no registered models → create_all creates nothing).
    The shared Base.metadata (populated once by `import app.models`) is reused.
    """
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker

    db_file = tmp_path / "t.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, connection_record):  # noqa: D401
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    from app.database import Base
    import app.models  # noqa: registers all mappers on Base.metadata
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Seed: org, user, project, a DB-type KnowledgeBase, and a conversation.
    # Commit parent rows (org/user/project) BEFORE children that FK them —
    # without a declared relationship(), SQLAlchemy's UnitOfWork may not
    # infer the insert order, and sqlite enforces FKs at commit.
    from app.models.organization import Organization
    from app.models.user import User
    from app.models.project import Project
    from app.models.knowledge_base import KnowledgeBase
    from app.models.agent_conversation import AgentConversation
    with SessionLocal() as db:
        db.add(Organization(id="o1", name="org", slug="org"))
        db.add(User(id="u1", email="a@b.c", full_name="Admin", org_id="o1", role="admin", password_hash="x"))
        db.add(Project(id="p1", name="proj", org_id="o1"))
        db.commit()
        db.add(KnowledgeBase(id="kb1", name="salesdb", org_id="o1", project_id="p1"))
        db.add(AgentConversation(id="conv1", org_id="o1", app_id="default-app"))
        db.commit()

    from app.services.tool_handlers.dashboard_tools import _create_dashboard
    from app.models.dashboard import Dashboard

    args = {
        "datasource_id": "kb1",
        "title": "Sales",
        "widgets": [{"id": "w1", "type": "kpi", "title": "Rev", "sql": "SELECT 1 AS revenue"}],
    }
    with SessionLocal() as db:
        result = asyncio.run(_create_dashboard(args, db, "u1", context={
            "org_id": "o1", "project_id": "p1", "conversation_id": "conv1",
        }))
    assert result["success"]

    # The conversation must now point at the newly-created dashboard.
    with SessionLocal() as db:
        conv = db.get(AgentConversation, "conv1")
        assert conv.dashboard_id is not None
        assert conv.dashboard_id == result["dashboard"]["id"]
        # And that dashboard actually exists.
        d = db.get(Dashboard, conv.dashboard_id)
        assert d is not None
        assert d.name == "Sales"


def _setup_binding_db(tmp_path, with_bound=True):
    """Private engine + shared Base.metadata + seeded rows for binding tests.

    Returns ``(SessionLocal, ctx)``. Seeds org/user/project/kb and ``conv1``.
    When ``with_bound`` is True, also creates+binds a dashboard to ``conv1``
    via ``_create_dashboard`` so ``update_dashboard`` can resolve it.

    Uses a private engine bound to the SHARED ``Base.metadata`` (populated by
    ``import app.models``) — NOT ``importlib.reload(app.database)``, which
    orphans model registrations so ``create_all`` creates zero tables.
    """
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker
    from app.database import Base
    import app.models  # noqa: registers all mappers on Base.metadata
    from app.models.organization import Organization
    from app.models.user import User
    from app.models.project import Project
    from app.models.knowledge_base import KnowledgeBase
    from app.models.agent_conversation import AgentConversation

    db_file = tmp_path / "t.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, connection_record):  # noqa: D401
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Commit parent rows (org/user/project) BEFORE children that FK them.
    with SessionLocal() as db:
        db.add(Organization(id="o1", name="org", slug="org"))
        db.add(User(id="u1", email="a@b.c", full_name="Admin", org_id="o1", role="admin", password_hash="x"))
        db.add(Project(id="p1", name="proj", org_id="o1"))
        db.commit()
        db.add(KnowledgeBase(id="kb1", name="salesdb", org_id="o1", project_id="p1"))
        db.add(AgentConversation(id="conv1", org_id="o1", app_id="default-app"))
        db.commit()

    ctx = {"org_id": "o1", "project_id": "p1", "conversation_id": "conv1"}

    if with_bound:
        from app.services.tool_handlers.dashboard_tools import _create_dashboard
        args = {
            "datasource_id": "kb1", "title": "Sales",
            "widgets": [{"id": "w1", "type": "kpi", "title": "Rev", "sql": "SELECT 1 AS revenue"}],
        }
        with SessionLocal() as db:
            created = asyncio.run(_create_dashboard(args, db, "u1", context=ctx))
        assert created["success"], created

    return SessionLocal, ctx


def test_update_dashboard_resolves_from_conversation(tmp_path):
    """update_dashboard WITHOUT dashboard_id targets the conversation's bound dashboard."""
    SessionLocal, ctx = _setup_binding_db(tmp_path, with_bound=True)
    from app.services.tool_handlers.dashboard_tools import _update_dashboard

    # No dashboard_id passed — resolution must target conv1's bound dashboard.
    with SessionLocal() as db:
        updated = asyncio.run(_update_dashboard({"title": "Sales — Q3"}, db, "u1", context=ctx))
    assert updated["success"], updated
    assert updated["dashboard"]["name"] == "Sales — Q3"


def test_update_dashboard_without_binding_returns_structured_error(tmp_path):
    """update_dashboard with no bound dashboard and no dashboard_id → structured error."""
    SessionLocal, _ = _setup_binding_db(tmp_path, with_bound=False)
    from app.services.tool_handlers.dashboard_tools import _update_dashboard
    from app.models.agent_conversation import AgentConversation

    # A conversation with NO bound dashboard.
    with SessionLocal() as db:
        db.add(AgentConversation(id="conv_empty", org_id="o1", app_id="default-app"))
        db.commit()

    with SessionLocal() as db:
        result = asyncio.run(_update_dashboard(
            {"title": "x"}, db, "u1",
            context={"org_id": "o1", "conversation_id": "conv_empty"},
        ))
    assert not result["success"]
    assert "no dashboard" in result["error"].lower() or "create" in result["error"].lower()


def _test_dashboards_app(SessionLocal, user_id="u1", org_id="o1"):
    """Minimal FastAPI app mounting the dashboards router with overridden deps.

    Overrides ``get_db`` (private engine) and ``get_current_user_required``
    (fake user) so endpoint tests run against an isolated sqlite DB without
    real auth — avoiding the dummy-password_hash login problem.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers.dashboards import router
    from app.database import get_db
    from app.deps import get_current_user_required

    app = FastAPI()
    app.include_router(router, prefix="/api")

    def _db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    class _User:
        pass

    u = _User()
    u.id = user_id
    u.org_id = org_id

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user_required] = lambda: u
    return TestClient(app)


def test_by_conversation_returns_bound_dashboard(tmp_path):
    """GET /api/dashboards/by-conversation/{id} returns the bound dashboard (200)."""
    SessionLocal, _ = _setup_binding_db(tmp_path, with_bound=True)
    client = _test_dashboards_app(SessionLocal)
    r = client.get("/api/dashboards/by-conversation/conv1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["can_edit"] is True  # u1 created the dashboard
    assert body["id"]  # the bound dashboard's id


def test_by_conversation_404_when_unbound(tmp_path):
    """GET /by-conversation/{id} → 404 with a 'no dashboard' detail when unbound.

    Asserts the detail message (not just the status) so it cannot spuriously
    pass on a missing-route 404 ('Not Found') before the endpoint exists.
    """
    SessionLocal, _ = _setup_binding_db(tmp_path, with_bound=False)
    client = _test_dashboards_app(SessionLocal)
    r = client.get("/api/dashboards/by-conversation/conv1")
    assert r.status_code == 404
    assert "no dashboard" in r.json()["detail"].lower()
