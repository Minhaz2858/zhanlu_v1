"""Database tools — list_data_sources, describe_schema, execute_query, answer_from_database.

These four tools are always registered in the tool registry, but they are
NOT auto-injected onto the calling agent's tool list. The `data_agent`
builtin subagent is the only caller — when the user-facing agent needs
data, it delegates to the subagent via the `ask_data_agent` tool, and
the subagent invokes these tools internally.

Tool surface is intentionally minimal — each tool is one well-defined
operation the LLM can call. The "high-level" `answer_from_database` tool
is a thin wrapper around `NLAnswerService` so the LLM can either ask
for SQL it then runs itself (`execute_query`) or hand the whole
question off.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.services.db import NLAnswerService, QueryService, SchemaService
from app.services.db.connector_factory import DriverUnavailable
from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_bound_kb_ids(context: dict | None) -> list[str]:
    """Return the list of KB IDs the calling agent is allowed to use.

    The runtime injects `bound_kb_ids` into TOOL_CONTEXT when wiring up
    the agent. Falls back to an empty list when called outside an
    agent context (e.g. the builtin Data Agent, which receives its own
    scoped set via `data_source_id`).
    """
    if not context:
        return []
    return list(context.get("bound_kb_ids") or [])


def _require_kb_id(args: dict, context: dict | None) -> tuple[str | None, dict | None]:
    """Pick a KB id from `data_source_id` / `kb_id` / context.

    Returns (kb_id, error_dict). If error_dict is set, the caller
    should return it immediately.
    """
    kb_id = (
        args.get("data_source_id")
        or args.get("kb_id")
        or (context or {}).get("data_source_id")
    )
    if not kb_id:
        return None, {
            "success": False,
            "error": "data_source_id (or kb_id) is required",
        }
    # If the agent is scoped, enforce the bound set.
    bound = _resolve_bound_kb_ids(context)
    if bound and kb_id not in bound:
        return None, {
            "success": False,
            "error": (
                f"KnowledgeBase {kb_id!r} is not bound to this agent. "
                f"Bound data sources: {bound}"
            ),
        }
    return kb_id, None


# ---------------------------------------------------------------------------
# list_data_sources
# ---------------------------------------------------------------------------

async def _list_data_sources(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    """Return the data sources bound to the calling agent."""
    bound = _resolve_bound_kb_ids(context)
    if not bound:
        return {
            "success": True,
            "data_sources": [],
            "message": "This agent has no data sources bound. "
                       "Bind a KnowledgeBase of source_kind='database' or "
                       "source_kind='file' in the agent's Data Sources section.",
        }
    from app.models.knowledge_base import KnowledgeBase
    rows = (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.id.in_(bound), KnowledgeBase.is_deleted == False)  # noqa: E712
        .all()
    )
    return {
        "success": True,
        "data_sources": [
            {
                "id": kb.id,
                "name": kb.name,
                "description": kb.description or "",
                "source_kind": kb.source_kind or "database",
                "db_type": kb.db_type or "",
                "database_name": kb.database_name or "",
                "file_type": kb.file_type or "",
                "indexing_status": kb.indexing_status,
                "chunk_count": kb.chunk_count or 0,
            }
            for kb in rows
        ],
    }


# ---------------------------------------------------------------------------
# describe_schema
# ---------------------------------------------------------------------------

async def _describe_schema(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    """Return schema information for a bound data source."""
    kb_id, err = _require_kb_id(args, context)
    if err:
        return err
    table = args.get("table")
    full = bool(args.get("full", False))
    max_tables = int(args.get("max_tables", 30))

    # ── Schema linker path (flag-gated) — runs before _do_work ──
    if full and settings.SCHEMA_LINKING_ENABLED and settings.SEMANTIC_CATALOG_ENABLED:
        try:
            from app.services.knowledge_graph.schema_linker import link_schema
            question = args.get("question", "") or (context or {}).get("user_query", "") or ""
            linker_result = await link_schema(
                question=question,
                kb_ids=[kb_id],
                db=db,
                top_k=8,
            )
            if linker_result and linker_result.get("slice_text"):
                return {"success": True, "schema": linker_result["slice_text"], "source": "catalog"}
        except Exception:
            pass  # fall through to describe_all

    def _do_work() -> dict:
        svc = SchemaService(db)
        if table:
            return svc.describe_table(kb_id, table)
        if full:
            return svc.describe_all(kb_id, max_tables=max_tables)
        # Default: just list tables
        return svc.list_tables(kb_id)

    try:
        result = await asyncio.to_thread(_do_work)
        return {"success": True, **result}
    except DriverUnavailable as e:
        return {"success": False, "error": str(e), "error_kind": "driver_missing"}
    except ValueError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.warning("describe_schema failed: %s", e)
        return {"success": False, "error": f"describe_schema failed: {e}"}


# ---------------------------------------------------------------------------
# execute_query
# ---------------------------------------------------------------------------

async def _execute_query(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    """Run a SQL statement and return rows."""
    kb_id, err = _require_kb_id(args, context)
    if err:
        return err
    sql = (args.get("sql") or "").strip()
    if not sql:
        return {"success": False, "error": "sql is required"}
    max_rows = int(args.get("max_rows", 1000))
    timeout_s = int(args.get("timeout_s", 10))

    def _do_work() -> dict:
        svc = QueryService(db)
        return svc.execute(kb_id, sql, max_rows=max_rows, timeout_s=timeout_s)

    try:
        result = await asyncio.to_thread(_do_work)
        return {"success": True, **result}
    except DriverUnavailable as e:
        return {"success": False, "error": str(e), "error_kind": "driver_missing", "sql": sql}
    except ValueError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.warning("execute_query failed: %s", e)
        return {"success": False, "error": f"Query failed: {e}", "sql": sql}


# ---------------------------------------------------------------------------
# answer_from_database
# ---------------------------------------------------------------------------

async def _answer_from_database(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    """High-level NL2SQL: text question → structured + prose answer."""
    kb_id, err = _require_kb_id(args, context)
    if err:
        return err
    question = (args.get("question") or "").strip()
    if not question:
        return {"success": False, "error": "question is required"}

    try:
        svc = NLAnswerService(db)
        result = await svc.answer(kb_id, question)
        return result
    except DriverUnavailable as e:
        return {"success": False, "error": str(e), "error_kind": "driver_missing"}
    except Exception as e:
        logger.warning("answer_from_database failed: %s", e)
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Document tools (source_kind='file')
# ---------------------------------------------------------------------------

async def _search_documents(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    """Vector search over a bound file-kind KB. Returns raw chunks."""
    kb_id, err = _require_kb_id(args, context)
    if err:
        return err
    query = (args.get("query") or args.get("question") or "").strip()
    if not query:
        return {"success": False, "error": "query (or question) is required"}
    top_k = int(args.get("top_k", 5))
    from app.services.document_ingestion import retrieval
    return retrieval.search(db, kb_id, query, top_k=top_k)


async def _answer_from_documents(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    """End-to-end: vector search + LLM synthesis → prose answer with citations."""
    kb_id, err = _require_kb_id(args, context)
    if err:
        return err
    question = (args.get("question") or "").strip()
    if not question:
        return {"success": False, "error": "question is required"}
    from app.services.document_ingestion import retrieval
    return await retrieval.answer(db, kb_id, question)


# ---------------------------------------------------------------------------
# Schemas & Registration
# ---------------------------------------------------------------------------

LIST_DATA_SOURCES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_data_sources",
        "description": (
            "List the database data sources bound to this agent. "
            "Each source has an id, name, db_type, and database_name. "
            "Call this first to discover what data you can query."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

DESCRIBE_SCHEMA_SCHEMA = {
    "type": "function",
    "function": {
        "name": "describe_schema",
        "description": (
            "Return schema information for a bound data source. "
            "Pass a `table` to get columns of one table, or omit it "
            "to get a list of tables. Set `full=true` to get all "
            "columns for every table (uses more context)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "data_source_id": {
                    "type": "string",
                    "description": "The id of the bound data source to introspect.",
                },
                "table": {
                    "type": "string",
                    "description": "Optional. If set, return columns of just this table.",
                },
                "full": {
                    "type": "boolean",
                    "description": "If true, return columns for every table.",
                    "default": False,
                },
                "max_tables": {
                    "type": "integer",
                    "description": "Cap on number of tables when full=true (default 30).",
                    "default": 30,
                },
            },
            "required": ["data_source_id"],
        },
    },
}

EXECUTE_QUERY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "execute_query",
        "description": (
            "Run a SQL statement against a bound data source and return the rows. "
            "The query is read-only from this tool's perspective; the LLM is "
            "trusted to write appropriate SQL. Use `describe_schema` first to "
            "discover table/column names."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "data_source_id": {
                    "type": "string",
                    "description": "The id of the bound data source to query.",
                },
                "sql": {
                    "type": "string",
                    "description": "The SQL statement to execute (a single statement).",
                },
                "max_rows": {
                    "type": "integer",
                    "description": "Maximum rows to return (default 1000).",
                    "default": 1000,
                },
                "timeout_s": {
                    "type": "integer",
                    "description": "Statement timeout in seconds (default 10).",
                    "default": 10,
                },
            },
            "required": ["data_source_id", "sql"],
        },
    },
}

ANSWER_FROM_DATABASE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "answer_from_database",
        "description": (
            "End-to-end natural-language database answer. Pass a question, "
            "get back a structured payload: a natural-language 'answer', "
            "the 'rows' used to answer, the 'sql' that was run, the "
            "'source_id' / 'source_name' it came from, and 'citations' "
            "listing the tables/columns referenced. "
            "Use this for simple questions; for complex multi-step work, "
            "call describe_schema + execute_query yourself."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "data_source_id": {
                    "type": "string",
                    "description": "The id of the bound data source to answer from.",
                },
                "question": {
                    "type": "string",
                    "description": "The natural-language question to answer.",
                },
            },
            "required": ["data_source_id", "question"],
        },
    },
}

SEARCH_DOCUMENTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_documents",
        "description": (
            "Semantic search over a bound document data source "
            "(source_kind='file'). Returns the top-k matching passages "
            "with scores and file metadata. Use this for granular "
            "retrieval; use answer_from_documents for a one-shot prose answer."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "data_source_id": {
                    "type": "string",
                    "description": "The id of the bound document data source.",
                },
                "query": {
                    "type": "string",
                    "description": "The natural-language search query.",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Max passages to return (default 5, max 20).",
                    "default": 5,
                },
            },
            "required": ["data_source_id", "query"],
        },
    },
}

ANSWER_FROM_DOCUMENTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "answer_from_documents",
        "description": (
            "End-to-end document answer. Pass a question, get back a "
            "prose 'answer' grounded in the top passages of the bound "
            "document data source, plus 'chunks' and 'citations' "
            "(file_name, chunk_index, score). Use this for simple "
            "questions; for multi-step reasoning use search_documents."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "data_source_id": {
                    "type": "string",
                    "description": "The id of the bound document data source.",
                },
                "question": {
                    "type": "string",
                    "description": "The natural-language question to answer.",
                },
            },
            "required": ["data_source_id", "question"],
        },
    },
}

# Register all six tools. The `data_agent` subagent is the only caller —
# the runtime never auto-injects these onto the user-facing agent.
# `enabled_by_default=False` is set so the registry's default-enable
# logic does not surface them in places that don't explicitly opt in.
for _name, _schema, _handler, _desc in (
    ("list_data_sources", LIST_DATA_SOURCES_SCHEMA, _list_data_sources,
     "List data sources bound to this agent."),
    ("describe_schema", DESCRIBE_SCHEMA_SCHEMA, _describe_schema,
     "Introspect the schema of a bound data source."),
    ("execute_query", EXECUTE_QUERY_SCHEMA, _execute_query,
     "Run a SQL statement against a bound data source."),
    ("answer_from_database", ANSWER_FROM_DATABASE_SCHEMA, _answer_from_database,
     "End-to-end NL2SQL answer from a bound data source."),
    ("search_documents", SEARCH_DOCUMENTS_SCHEMA, _search_documents,
     "Semantic search over a bound document data source."),
    ("answer_from_documents", ANSWER_FROM_DOCUMENTS_SCHEMA, _answer_from_documents,
     "End-to-end answer from a bound document data source."),
):
    registry.register(
        name=_name,
        schema=_schema,
        handler=_handler,
        category="database",
        enabled_by_default=False,  # subagent-only
        description=_desc,
    )
