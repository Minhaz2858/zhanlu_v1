"""SQLAlchemy engine, session factory, declarative base, and Redis connection."""

import logging
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from app.config import settings

logger = logging.getLogger(__name__)

# SQLite needs check_same_thread=False for FastAPI's threaded request handling
connect_args = {"check_same_thread": False} if settings.is_sqlite else {}

engine = create_engine(
    settings.DATABASE_URL,
    connect_args=connect_args,
    echo=False,
)

# Enable foreign key enforcement for SQLite
if settings.is_sqlite:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    """FastAPI dependency: yield a DB session and close it after request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class Base(DeclarativeBase):
    """Declarative base for all SQLAlchemy models."""
    pass


# --- Redis connection (lazy, optional) ---
_redis_client = None


def get_redis():
    """Return a Redis client if REDIS_URL is configured, else None.

    Used by the sandbox job queue, event fanout, and distributed locks.
    In local dev (no REDIS_URL), callers fall back to in-process alternatives.
    """
    global _redis_client
    if not settings.REDIS_URL:
        return None
    if _redis_client is None:
        try:
            import redis
            _redis_client = redis.from_url(
                settings.REDIS_URL, decode_responses=True
            )
            _redis_client.ping()
            logger.info("Redis connected: %s", settings.REDIS_URL)
        except ImportError:
            logger.warning("redis package not installed — Redis features disabled")
            return None
        except Exception as e:
            logger.warning("Redis connection failed (non-fatal): %s", e)
            return None
    return _redis_client
