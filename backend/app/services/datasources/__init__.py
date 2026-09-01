"""Datasource adapter protocol and factory.

Defines the ``DatasourceAdapter`` Protocol that every concrete adapter
(SQLite, Postgres, …) must satisfy.  The adapter is the **only** place
that touches a database driver — all NL2SQL logic works through this
interface, making it trivial to add new dialects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class ColumnInfo:
    name: str
    dtype: str          # e.g. "TEXT", "INTEGER", "VARCHAR(255)"
    nullable: bool = True
    default: str | None = None
    is_pk: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExplainResult:
    plan_json: dict[str, Any] | list[Any]     # structured plan
    plan_text: str                              # human-readable plan
    estimated_cost: float = 0.0                 # normalised cost (0–1+)
    estimated_rows: int = 0


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple[Any, ...]]
    row_count: int
    duration_ms: float


_ADAPTER_DB_TYPES = {
    "mysql": ("mysql", "mariadb"),
    "postgres": ("postgres", "postgresql", "postgresql+psycopg2", "postgresql+psycopg"),
    "sqlite": ("sqlite", "sqlite3"),
}


def build_adapter(kb: Any) -> DatasourceAdapter:
    """Return the correct datasource adapter for a KnowledgeBase, by ``db_type``.

    This mirrors ``connector_factory.get_connector`` (which dispatches the
    SQLAlchemy connector used by ``QueryService``) so the M-Schema/schema-build
    path picks the same dialect as the query-execution path. Unknown types
    raise ``ValueError`` so the caller can surface a clear error to the LLM.

    ``kb`` may be a ``KnowledgeBase`` ORM row or any object exposing
    ``db_type`` and the connection fields (host/port/database_name/username/
    password/schema). Imported lazily so a missing driver only blocks the
    dialect that actually needs it.
    """
    db_type = (getattr(kb, "db_type", None) or "").strip().lower()
    if not db_type:
        raise ValueError(
            f"KnowledgeBase {getattr(kb, 'id', '?')!r} has no db_type set — "
            "cannot pick a datasource adapter."
        )

    if db_type in _ADAPTER_DB_TYPES["mysql"]:
        from app.services.datasources.mysql_adapter import MySQLAdapter
        return MySQLAdapter(kb)

    if db_type in _ADAPTER_DB_TYPES["postgres"]:
        from app.services.datasources.postgres_adapter import PostgresAdapter
        return PostgresAdapter(
            host=getattr(kb, "host", None) or "localhost",
            port=int(getattr(kb, "port", None) or 5432),
            dbname=getattr(kb, "database_name", None) or "zhanlu",
            user=getattr(kb, "username", None) or "zhanlu",
            password=getattr(kb, "password", None) or "",
            schema=getattr(kb, "schema", None) or "public",
        )

    if db_type in _ADAPTER_DB_TYPES["sqlite"]:
        from app.services.datasources.sqlite_adapter import SQLiteAdapter
        return SQLiteAdapter(
            db_path=getattr(kb, "database_name", None) or ":memory:"
        )

    raise ValueError(
        f"Unsupported db_type: {db_type!r}. Supported adapters: "
        f"{sorted(_ADAPTER_DB_TYPES.keys())}"
    )


@runtime_checkable
class DatasourceAdapter(Protocol):
    """Protocol that every datasource adapter must implement.

    All methods MUST be synchronous (callers wrap with ``run_in_executor``
    when running inside an async FastAPI handler).
    """

    @property
    def quote_char(self) -> str:
        """Identifier quoting character for this dialect (e.g. ``"`` for Postgres/SQLite, ```` `` `` for MySQL)."""
        ...

    def test_connection(self) -> bool:
        """Return ``True`` if the datasource can be reached."""
        ...

    def list_tables(self) -> list[str]:
        """Return a sorted list of user-facing table names."""
        ...

    def describe_table(self, table: str) -> list[ColumnInfo]:
        """Return column metadata for a single table."""
        ...

    def refresh_schema(self) -> dict[str, list[ColumnInfo]]:
        """Re-discover the entire schema and return ``{table: [ColumnInfo, …]}``."""
        ...

    def explain(self, sql: str, params: dict[str, Any] | None = None) -> ExplainResult:
        """Return the query plan + estimated cost for *sql*."""
        ...

    def query(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        row_limit: int = 1000,
        timeout_ms: int = 5000,
    ) -> QueryResult:
        """Execute *sql* and return rows (subject to row_limit and timeout)."""
        ...
