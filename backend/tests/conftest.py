"""Pytest configuration for backend tests — enable async test support."""
import os

# Must be set BEFORE any app.* imports so that app.config.settings
# reads SQLite. Pydantic-settings env vars take priority over .env files.
# Use file::memory:?cache=shared + StaticPool so the in-memory database
# persists across sessions — without sharing, each SessionLocal() opens
# a fresh connection and the tables created in _migrate_schema fixtures
# vanish (the classic SQLite ":memory:" multi-connection pitfall).
os.environ["DATABASE_URL"] = "sqlite:///file::memory:?cache=shared&uri=true"

import pytest
from sqlalchemy.pool import StaticPool
from sqlalchemy import event

pytest_plugins = ("pytest_asyncio",)


@pytest.fixture(autouse=True, scope="session")
def _ensure_shared_sqlite_engine():
    """Force the global SQLAlchemy engine to use StaticPool so the shared
    in-memory database is reachable from every SessionLocal() call.
    Without this, each new SessionLocal() opens a fresh :memory: connection
    and sees an empty schema (tables created in test fixtures vanish).
    """
    from app.database import engine  # noqa: F401
    # Re-create the engine with StaticPool if not already set
    if not isinstance(engine.pool, StaticPool):
        from sqlalchemy import create_engine as _ce
        from app.config import settings
        new_engine = _ce(
            settings.DATABASE_URL,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        # Replace module-level engine + SessionLocal
        import app.database as _db_mod
        _db_mod.engine = new_engine
        _db_mod.SessionLocal = _db_mod.sessionmaker(
            autocommit=False, autoflush=False, bind=new_engine,
        )
        # Re-enable FK pragma on the new engine
        @event.listens_for(new_engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    yield