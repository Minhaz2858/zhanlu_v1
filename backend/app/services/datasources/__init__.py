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


@runtime_checkable
class DatasourceAdapter(Protocol):
    """Protocol that every datasource adapter must implement.

    All methods MUST be synchronous (callers wrap with ``run_in_executor``
    when running inside an async FastAPI handler).
    """

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
