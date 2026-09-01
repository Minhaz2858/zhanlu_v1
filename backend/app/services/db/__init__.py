"""Database connectivity and NL2SQL services.

This module provides SQLAlchemy-based connectors for MySQL, PostgreSQL,
MSSQL, Oracle, and SQLite, plus higher-level services for schema
introspection, query execution, and natural-language answer generation.

Connectors are short-lived (open/execute/close per request in v1). A
single `max_rows=1000` and `timeout_s=10` cap is enforced in
`QueryService.execute` as a v1 safety net — not a security control.
"""

from app.services.db.connector_factory import get_connector
from app.services.db.schema_service import SchemaService
from app.services.db.query_service import QueryService
from app.services.db.nl_answer_service import NLAnswerService

__all__ = [
    "get_connector",
    "SchemaService",
    "QueryService",
    "NLAnswerService",
]
