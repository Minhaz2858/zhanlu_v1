"""Factory that returns the right connector for a `KnowledgeBase` row.

Dispatch is by `kb.db_type` (case-insensitive). Unknown types raise
`ValueError` so the caller surfaces a clear error to the LLM.
"""

from __future__ import annotations

import logging
from typing import Any

from app.services.db.base import BaseConnector

logger = logging.getLogger(__name__)

# Map db_type → connector class. Imported lazily so a missing driver only
# blocks the dialect that needs it.
_REGISTRY: dict[str, type] = {}


class DriverUnavailable(ValueError):
    """Raised when a DB driver is missing and cannot be auto-installed.

    This is a ValueError subclass so existing ``except Exception`` blocks
    still catch it, but callers can distinguish "driver missing" from
    "bad SQL" via ``error_kind="driver_missing"``.
    """


# Map db_type → lazy_deps feature key. The connector factory calls
# ensure(feature) before constructing the connector, so the user
# never sees a raw "ModuleNotFoundError: No module named 'pymysql'".
_DRIVER_FEATURE: dict[str, str] = {
    "mysql": "db_mysql",
    "mariadb": "db_mysql",
    "postgres": "db_postgres",
    "postgresql": "db_postgres",
    "mssql": "db_mssql",
    "sqlserver": "db_mssql",
    "oracle": "db_oracle",
    "sqlite": "db_sqlite",
}


def _humanize_db_type(db_type: str) -> str:
    """Return a human-readable label for a db_type key."""
    return {
        "mysql": "MySQL",
        "mariadb": "MariaDB",
        "postgres": "PostgreSQL",
        "postgresql": "PostgreSQL",
        "mssql": "SQL Server",
        "sqlserver": "SQL Server",
        "oracle": "Oracle",
        "sqlite": "SQLite",
    }.get(db_type, db_type)


def _ensure_registry() -> None:
    if _REGISTRY:
        return
    from app.services.db.sqlite import SQLiteConnector
    from app.services.db.mysql import MySQLConnector
    from app.services.db.postgres import PostgresConnector
    from app.services.db.mssql import MSSQLConnector
    from app.services.db.oracle import OracleConnector

    _REGISTRY.update({
        "sqlite": SQLiteConnector,
        "mysql": MySQLConnector,
        "mariadb": MySQLConnector,  # MariaDB uses the MySQL wire protocol
        "postgres": PostgresConnector,
        "postgresql": PostgresConnector,
        "mssql": MSSQLConnector,
        "sqlserver": MSSQLConnector,
        "oracle": OracleConnector,
    })


def get_connector(kb: Any) -> BaseConnector:
    """Return an UNENTERED connector instance for the given KB.

    Caller is responsible for using it as a context manager:

        with get_connector(kb) as conn:
            ...

    When the required DB driver is missing, the factory auto-installs it
    via the lazy_deps allowlisted pip pathway. If auto-install is disabled
    or fails, a DriverUnavailable is raised with a user-friendly message
    that includes both the pip command and the ``ZHANLU_ALLOW_LAZY_INSTALLS=1``
    opt-in — the caller should surface this with ``error_kind="driver_missing"``
    so the LLM can apologise without leaking the raw stack.
    """
    _ensure_registry()
    db_type = (kb.db_type or "").strip().lower()
    if not db_type:
        raise ValueError(
            f"KnowledgeBase {kb.id!r} has no db_type set — cannot pick a connector."
        )
    # ---- auto-install the DB driver if it's missing ---------------
    feature = _DRIVER_FEATURE.get(db_type)
    if feature:
        # fmt: off
        from app.services.tool_handlers.lazy_deps import (
            ensure,
            FeatureUnavailable,
            LAZY_DEPS,
        )
        # fmt: on
        try:
            ensure(feature)  # idempotent: no-op once pymysql is installed
        except FeatureUnavailable as exc:
            specs = LAZY_DEPS.get(feature, [])
            install_hint = (
                f"pip install {' '.join(specs)}"
                if specs
                else "(no pip specs — check LAZY_DEPS)"
            )
            raise DriverUnavailable(
                f"{_humanize_db_type(db_type)} driver is not installed. "
                f"Run `{install_hint}` in the backend venv, "
                f"or set ZHANLU_ALLOW_LAZY_INSTALLS=1 to allow auto-install. "
                f"Underlying error: {exc}"
            ) from exc
    # ----------------------------------------------------------------
    cls = _REGISTRY.get(db_type)
    if cls is None:
        raise ValueError(
            f"Unsupported db_type: {db_type!r}. "
            f"Supported: {sorted(_REGISTRY.keys())}"
        )
    return cls(kb)


def supported_db_types() -> list[str]:
    """Return the list of supported db_type values (for UI dropdowns)."""
    _ensure_registry()
    return sorted(_REGISTRY.keys())
