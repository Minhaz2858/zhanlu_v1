"""Lazy SQLAlchemy engine for EDIA's MySQL warehouse (read-only mirror).

Mirrors EDIA's backend/src/db/engine_factory.py pattern: pool_pre_ping + small
pool + short connect_timeout so an unreachable host fails in ~3s, not 30s.

Empty EDIA_MYSQL_URL = feature off. The engine is created lazily on first
use so the rest of zhanlu is unaffected when EDIA is not configured.
"""
import logging
import time
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.config import settings

logger = logging.getLogger(__name__)

_engine: Optional[Engine] = None
_reachability_cache: dict = {"value": None, "ts": 0.0}
_REACHABILITY_TTL = 30.0  # seconds


def get_mysql_engine() -> Optional[Engine]:
    """Return the cached EDIA MySQL engine, or None when unconfigured."""
    global _engine
    if not settings.EDIA_MYSQL_URL:
        return None
    if _engine is None:
        _engine = create_engine(
            settings.EDIA_MYSQL_URL,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=2,
            pool_recycle=1800,
            connect_args={"connect_timeout": 3},
        )
    return _engine


def get_mysql_db():
    """FastAPI dependency: yield a Session against EDIA's MySQL.

    Raises HTTPException(503, "edia_unavailable") when EDIA_MYSQL_URL is empty.
    Callers wrap query errors as HTTPException(503, "edia_unreachable").
    """
    eng = get_mysql_engine()
    if eng is None:
        raise HTTPException(status_code=503, detail="edia_unavailable")
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=eng)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def is_edia_reachable() -> bool:
    """Return True if `SELECT 1` succeeds against EDIA's MySQL.

    Cached for _REACHABILITY_TTL seconds to bound probe cost.
    Returns False when unconfigured OR when the probe raises.
    """
    eng = get_mysql_engine()
    if eng is None:
        return False
    now = time.time()
    cached = _reachability_cache.get("value")
    ts = _reachability_cache.get("ts") or 0.0
    if cached is not None and (now - ts) < _REACHABILITY_TTL:
        return cached
    try:
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        _reachability_cache["value"] = True
    except Exception as exc:
        logger.warning("EDIA MySQL unreachable: %s", exc)
        _reachability_cache["value"] = False
    _reachability_cache["ts"] = now
    return bool(_reachability_cache.get("value"))
