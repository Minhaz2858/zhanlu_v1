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
import re
import time
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.services.db import NLAnswerService, QueryService, SchemaService
from app.services.db.connector_factory import DriverUnavailable
from app.services.db.schema_graph import SchemaGraph
from app.services.db.schema_service import connection_fingerprint
from app.services import access_policy_service
from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema cache (TTL)
#
# describe_schema is one of the most expensive tools in the data-agent loop:
# it may run the semantic schema linker, build a SchemaGraph, or introspect
# the live DB — each takes 5-20s. Within a single conversation turn the
# schema does NOT change, so repeated describe_schema calls with the same
# (kb_id, table, full) shape can be served from a TTL cache, saving 15-20s
# per repeated call and cutting multi-query reports from ~317s to ~80s.
# ---------------------------------------------------------------------------

_SCHEMA_CACHE: dict[str, tuple[float, dict]] = {}
_SCHEMA_CACHE_TTL_SECONDS = 3600  # 1 hour — schema rarely changes mid-session


def _schema_cache_key(
    kb_id: str, table: str | None, full: bool, max_tables: int, fingerprint: str = ""
) -> str:
    # fingerprint = connection identity (host/port/db/dialect hash) so that
    # re-pointing a KB at a DIFFERENT database is an automatic cache miss.
    return f"{kb_id}|{fingerprint}|{table or ''}|{int(full)}|{max_tables}"


def _schema_cache_get(
    kb_id: str, table: str | None, full: bool, max_tables: int, fingerprint: str = ""
) -> dict | None:
    key = _schema_cache_key(kb_id, table, full, max_tables, fingerprint)
    hit = _SCHEMA_CACHE.get(key)
    if not hit:
        return None
    cached_at, payload = hit
    if time.monotonic() - cached_at > _SCHEMA_CACHE_TTL_SECONDS:
        _SCHEMA_CACHE.pop(key, None)
        return None
    logger.debug("describe_schema cache HIT for %s", key)
    return payload


def _schema_cache_put(
    kb_id: str, table: str | None, full: bool, max_tables: int, payload: dict, fingerprint: str = ""
) -> None:
    key = _schema_cache_key(kb_id, table, full, max_tables, fingerprint)
    _SCHEMA_CACHE[key] = (time.monotonic(), payload)
    # Opportunistic eviction: keep the cache bounded (~256 entries).
    if len(_SCHEMA_CACHE) > 256:
        now = time.monotonic()
        expired = [k for k, (t, _) in _SCHEMA_CACHE.items() if now - t > _SCHEMA_CACHE_TTL_SECONDS]
        for k in expired:
            _SCHEMA_CACHE.pop(k, None)
        if len(_SCHEMA_CACHE) > 256:
            # Fall back to dropping the oldest 64 entries.
            oldest = sorted(_SCHEMA_CACHE.items(), key=lambda kv: kv[1][0])[:64]
            for k, _ in oldest:
                _SCHEMA_CACHE.pop(k, None)


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


def _resolve_user_policy(
    db: Session,
    user_id: str | None,
    context: dict | None,
) -> "access_policy_service.ResolvedPolicy":
    """Resolve the current user's data-access policy from tool context.

    Reads ``resource_type`` / ``resource_id`` (injected by
    ``prepare_data_source_runtime``) and the bound KB set.  When the context
    lacks resource scoping (e.g. the builtin Data Agent), returns a full-access
    policy — policies only apply to shared users of a project/agent.
    """
    if not context or not user_id:
        return access_policy_service.ResolvedPolicy(has_policies=False)
    resource_type = context.get("resource_type")
    resource_id = context.get("resource_id")
    if not resource_type or not resource_id:
        return access_policy_service.ResolvedPolicy(has_policies=False)
    return access_policy_service.resolve_user_policies(
        db,
        user_id=user_id,
        resource_type=resource_type,
        resource_id=resource_id,
        bound_kb_ids=_resolve_bound_kb_ids(context),
    )


def _apply_schema_policy(
    result: dict,
    policy: "access_policy_service.ResolvedPolicy",
    kb_id: str,
    requested_table: str | None,
) -> dict:
    """Filter a SchemaService result to the user's policy for *kb_id*.

    Handles the three SchemaService shapes:
      - list_tables:    {"tables": ["a", "b"]}
      - describe_table: {"table": "a", "columns": [{name: ...}, ...]}
      - describe_all:   {"tables": [{"table": "a", "columns": [...]}, ...]}
    """
    if not policy.has_policies and not policy.blocked_kb_ids:
        return result

    blocked = {t.lower() for t in policy.blocked_tables_for_kb(kb_id)}
    allowed = policy.allowed_tables_for_kb(kb_id)  # None or list of allowed tables
    allowed_set = {t.lower() for t in allowed} if allowed is not None else None

    def _table_visible(name: str | None) -> bool:
        n = (name or "").lower()
        if n in blocked:
            return False
        if allowed_set is not None and n not in allowed_set:
            return False
        return True

    def _filter_columns(table_name: str | None, columns: list) -> list:
        allowlist = policy.allowed_columns_for(kb_id, table_name)
        if allowlist is None:
            return columns
        allow_lower = {c.lower() for c in allowlist}
        return [c for c in columns if (c.get("name") or "").lower() in allow_lower]

    # describe_table shape: {"table": ..., "columns": [...]}
    if "table" in result and isinstance(result.get("columns"), list):
        tname = result.get("table")
        if not _table_visible(tname):
            raise ValueError("Access to this table is restricted.")
        result["columns"] = _filter_columns(tname, result.get("columns", []))
        return result

    # list_tables (list of strings) or describe_all (list of dicts)
    tables = result.get("tables", [])
    if not tables:
        return result

    if isinstance(tables[0], dict):
        filtered = []
        for entry in tables:
            tname = entry.get("table")
            if not _table_visible(tname):
                continue
            entry["columns"] = _filter_columns(tname, entry.get("columns", []))
            filtered.append(entry)
        result["tables"] = filtered
        # all_table_names (describe_all full name list) must be filtered too —
        # otherwise restricted users see blocked table names.
        if isinstance(result.get("all_table_names"), list):
            result["all_table_names"] = [
                t for t in result["all_table_names"] if _table_visible(t)
            ]
        return result

    result["tables"] = [t for t in tables if _table_visible(t)]
    return result


def _annotate_table_roles(result: dict, db, kb_id: str | None) -> dict:
    """Annotate describe_table / describe_all results with ``table_role``.

    Loads the cached structural role (entity_master / fact / dimension /
    bridge / unknown) from ``kb_table_meta.table_role`` so the LLM knows
    which tables to query FIRST (masters) when using the non-schema-graph
    fallback path. Flag-gated by ENTITY_MASTER_FILTER_ENABLED; best-effort
    (any failure leaves the result untouched).
    """
    if not getattr(settings, "ENTITY_MASTER_FILTER_ENABLED", False):
        return result
    try:
        from app.models.knowledge_catalog import KBTableMeta
        name_to_role: dict[str, str] = {}
        table_names: list[str] = []
        # describe_table shape
        if isinstance(result.get("table"), str):
            table_names.append(result["table"])
        # describe_all shape
        for entry in result.get("tables", []) or []:
            if isinstance(entry, dict) and isinstance(entry.get("table"), str):
                table_names.append(entry["table"])
        if not table_names:
            return result
        metas = (
            db.query(KBTableMeta)
            .filter(
                KBTableMeta.kb_id == kb_id,
                KBTableMeta.table_name.in_(table_names),
            )
            .all()
        )
        name_to_role = {m.table_name: m.table_role or "unknown" for m in metas}

        if isinstance(result.get("table"), str):
            result["table_role"] = name_to_role.get(result["table"], "unknown")
        for entry in result.get("tables", []) or []:
            if isinstance(entry, dict) and isinstance(entry.get("table"), str):
                entry["table_role"] = name_to_role.get(entry["table"], "unknown")
    except Exception as exc:
        logger.debug("_annotate_table_roles failed (non-fatal): %s", exc)
    return result


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

    # Per-user access policy: hide KBs the user is denied, and annotate
    # restricted KBs with their allowed/blocked tables so the LLM can avoid
    # querying forbidden data.
    policy = _resolve_user_policy(db, user_id, context)

    data_sources = []
    for kb in rows:
        if policy.is_kb_fully_denied(kb.id):
            logger.info(
                "list_data_sources: hiding KB %s (denied for user %s)", kb.id, user_id
            )
            continue
        entry = {
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
        if policy.is_kb_restricted(kb.id):
            entry["restricted"] = True
            allowed = policy.allowed_tables_for_kb(kb.id)
            if allowed is not None:
                entry["allowed_tables"] = allowed
            blocked = policy.blocked_tables_for_kb(kb.id)
            if blocked:
                entry["blocked_tables"] = blocked
        data_sources.append(entry)

    return {"success": True, "data_sources": data_sources}


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

    # Data Agent auto-escalation: when the caller has schema_validator_enabled
    # in context (the Data Agent sub-agent always does), default to returning
    # columns so the agent doesn't guess column names.  Without this, the
    # default path returns only table names, and the agent invents columns
    # like FDATE on the wrong table — causing 30+ second validator-rejection
    # loops before eventually falling back to stale tables.
    _ctx = context or {}
    if not full and not table and _ctx.get("schema_validator_enabled"):
        full = True
        if max_tables < 50:
            max_tables = 50

    policy = _resolve_user_policy(db, user_id, context)
    if policy.is_kb_fully_denied(kb_id):
        logger.warning("describe_schema: KB %s denied for user %s", kb_id, user_id)
        return {"success": False, "error": "Access to this data source is restricted."}

    # ── TTL schema cache: serve repeated describe_schema calls from cache ──
    # Within a single conversation turn the schema is immutable, and the
    # data-agent loop often calls describe_schema 2-5x per turn (once per
    # sub-agent iteration). Caching here cuts 15-20s per repeated call.
    # Policy-restricted KBs are excluded (their output depends on per-user
    # table allow/block lists).
    restricted = policy.is_kb_restricted(kb_id)
    # Connection identity — re-pointing the KB at a different database must
    # invalidate the schema cache automatically (see _schema_cache_key).
    _fp = connection_fingerprint(db, kb_id)
    if not restricted:
        cached = _schema_cache_get(kb_id, table, full, max_tables, _fp)
        if cached is not None:
            return cached

    # ── Schema linker path (flag-gated) — runs before _do_work ──
    # Skip when the KB is policy-restricted: the catalog slice is a free-text
    # blob we can't reliably filter, so fall back to the filtered describe path.
    # The Data Agent sub-context sets schema_linking_enabled / schema_graph_enabled
    # as agent-level opt-ins (in addition to global settings flags).
    _linker_enabled = (
        settings.SCHEMA_LINKING_ENABLED
        or _ctx.get("schema_linking_enabled")
    ) and settings.SEMANTIC_CATALOG_ENABLED
    _graph_enabled = (
        settings.SCHEMA_GRAPH_ENABLED
        or _ctx.get("schema_graph_enabled")
    )
    if not restricted and _linker_enabled:
        try:
            from app.services.knowledge_graph.schema_linker import link_schema
            question = args.get("question", "") or (context or {}).get("user_query", "") or ""
            linker_result = await link_schema(
                question=question,
                kb_ids=[kb_id],
                db=db,
                top_k=8,
            )
            candidate_tables = [
                t.get("table_name")
                for t in (linker_result or {}).get("tables", [])
                if t.get("table_name")
            ]

            # ── SchemaGraph path (flag-gated): structural view + join edges ──
            if _graph_enabled and candidate_tables:
                try:
                    from app.services.db.schema_graph import SchemaGraph
                    graph = await asyncio.to_thread(
                        lambda: SchemaGraph(db, kb_id).build(candidate_tables)
                    )
                    graph_text = graph.to_llm_context()
                    if graph_text.strip():
                        # Also include full TOC so LLM knows about ALL tables
                        _toc_section = ""
                        try:
                            from app.services.knowledge_graph.schema_linker import (
                                build_full_toc, format_toc_text,
                            )
                            toc = build_full_toc(db, [kb_id])
                            if toc and len(toc) > len(candidate_tables):
                                _toc_section = (
                                    "\n\n---\n"
                                    "FULL TABLE CATALOG (ALL tables in this database):\n"
                                    "If none of the tables above contain the data you need, "
                                    "check this catalog for alternatives.\n"
                                    + format_toc_text(toc, token_budget=1000)
                                )
                        except Exception:
                            pass
                        _result = {
                            "success": True,
                            "schema": graph_text + _toc_section,
                            "source": "schema_graph",
                        }
                        if not restricted:
                            _schema_cache_put(kb_id, table, full, max_tables, _result, _fp)
                        return _result
                except Exception as exc:
                    logger.warning("describe_schema: schema_graph failed: %s", exc)

            slice_text = (linker_result or {}).get("slice_text") or ""
            useful_lines = [
                line for line in slice_text.splitlines()
                if line.strip() and not line.strip().startswith("-- Semantic catalog")
            ]
            if useful_lines:
                # Also include full TOC so LLM knows about ALL tables
                _toc_section = ""
                try:
                    from app.services.knowledge_graph.schema_linker import (
                        build_full_toc, format_toc_text,
                    )
                    toc = build_full_toc(db, [kb_id])
                    if toc and len(toc) > 8:
                        _toc_section = (
                            "\n\n---\n"
                            "FULL TABLE CATALOG (ALL tables in this database):\n"
                            "If none of the tables above contain the data you need, "
                            "check this catalog for alternatives.\n"
                            + format_toc_text(toc, token_budget=1000)
                        )
                except Exception:
                    pass
                _result = {
                    "success": True,
                    "schema": slice_text + _toc_section,
                    "source": "catalog",
                }
                if not restricted:
                    _schema_cache_put(kb_id, table, full, max_tables, _result, _fp)
                return _result
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
        result = _apply_schema_policy(result, policy, kb_id, table)
        result = _annotate_table_roles(result, db, kb_id)
        _result = {"success": True, **result}
        if not restricted:
            _schema_cache_put(kb_id, table, full, max_tables, _result, _fp)
        return _result
    except DriverUnavailable as e:
        return {"success": False, "error": str(e), "error_kind": "driver_missing"}
    except ValueError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.warning("describe_schema failed: %s", e)
        return {"success": False, "error": f"describe_schema failed: {e}"}


# ---------------------------------------------------------------------------
# profile_data
# ---------------------------------------------------------------------------

def _profile_error_entry(table: str, exc: Exception) -> dict:
    """Per-table failure entry appended to profile_data results."""
    return {
        "table": table,
        "row_count": 0,
        "status": "error",
        "error_message": str(exc)[:200],
        "columns": [],
    }


async def _profile_data(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    """Profile real data (row counts, cardinality, null ratio, shapes) of a bound data source."""
    from app.services.dashboard_profiler import profile_kb

    kb_id, err = _require_kb_id(args, context)
    if err:
        return err

    policy = _resolve_user_policy(db, user_id, context)
    if policy.is_kb_fully_denied(kb_id):
        logger.warning("profile_data: KB %s denied for user %s", kb_id, user_id)
        return {"success": False, "error": "Access to this data source is restricted."}

    # Table/column-level policy filtering — same semantics as _apply_schema_policy
    # so blocked tables / whitelist-mode KBs / column allowlists are enforced
    # before any sampled data is returned.
    blocked = {t.lower() for t in policy.blocked_tables_for_kb(kb_id)}
    allowed = policy.allowed_tables_for_kb(kb_id)  # None or list of allowed tables
    allowed_set = {t.lower() for t in allowed} if allowed is not None else None

    def _table_visible(name: str | None) -> bool:
        n = (name or "").lower()
        if n in blocked:
            return False
        if allowed_set is not None and n not in allowed_set:
            return False
        return True

    def _filter_columns(table_name: str | None, cols: list[str]) -> list[str]:
        allowlist = policy.allowed_columns_for(kb_id, table_name)
        if allowlist is None:
            return cols
        allow_lower = {c.lower() for c in allowlist}
        return [c for c in cols if c.lower() in allow_lower]

    table = args.get("table")
    raw_columns = args.get("columns") or []
    # Guard against the LLM sending a JSON string instead of an array —
    # `list("a,b")` would char-split it into single letters.
    columns = raw_columns if isinstance(raw_columns, list) else []
    columns = [c for c in columns if isinstance(c, str) and c]
    try:
        max_tables = min(max(int(args.get("max_tables", 8)), 1), 8)
    except (TypeError, ValueError):
        max_tables = 8

    if table:
        if not _table_visible(table):
            logger.warning(
                "profile_data: table %s denied for user %s on KB %s",
                table, user_id, kb_id,
            )
            return {"success": False, "error": "Access to this table is restricted."}
        tables = [table]
    else:
        try:
            listing = await asyncio.to_thread(SchemaService(db).list_tables, kb_id)
        except DriverUnavailable as e:
            return {"success": False, "error": str(e), "error_kind": "driver_missing"}
        except ValueError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            logger.warning("profile_data: list_tables failed on KB %s: %s", kb_id, e)
            return {"success": False, "error": f"profile_data failed: {e}"}
        tables = [t for t in (listing.get("tables") or []) if _table_visible(t)][:max_tables]

    results: list[dict] = []
    for t in tables:
        cols = _filter_columns(t, columns) if columns else []
        if not cols:
            try:
                info = await asyncio.to_thread(
                    SchemaService(db).describe_table, kb_id, t
                )
                cols = [
                    c.get("name") or c.get("column_name") or ""
                    for c in (info.get("columns") or [])
                ]
                cols = [c for c in cols if c][:20]
                cols = _filter_columns(t, cols)
            except Exception as exc:
                results.append(_profile_error_entry(t, exc))
                continue
        try:
            profiled = await asyncio.to_thread(profile_kb, db, kb_id, t, cols)
            results.append(profiled)
        except Exception as exc:
            results.append(_profile_error_entry(t, exc))

    return {"success": True, "tables": results}


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

    # Read-only gate: this KB never accepts write/DDL statements. Runs before
    # the data-access-policy check so the rejection reason is unambiguous.
    from app.services.nl2sql.schema_validator import check_read_only_sql

    ro_error = check_read_only_sql(sql)
    if ro_error:
        return {"success": False, "error": ro_error, "sql": sql}

    # Per-user access policy: reject queries against denied tables BEFORE
    # execution (closes the gap where QueryService.execute had no validation).
    policy = _resolve_user_policy(db, user_id, context)
    if policy.is_kb_fully_denied(kb_id):
        logger.warning("execute_query: KB %s denied for user %s", kb_id, user_id)
        return {"success": False, "error": "Access to this data source is restricted.", "sql": sql}
    if policy.has_policies:
        vr = access_policy_service.validate_sql_against_policy(sql, policy, kb_id)
        if vr and not getattr(vr, "is_valid", True):
            errors = getattr(vr, "errors", []) or ["query references forbidden tables"]
            logger.warning(
                "execute_query: blocked SQL for user %s on KB %s: %s",
                user_id, kb_id, "; ".join(str(e) for e in errors),
            )
            return {
                "success": False,
                "error": "Query blocked by data access policy: " + "; ".join(str(e) for e in errors),
                "sql": sql,
            }

    # ── Structural schema validation (flag-gated): feedback, not a gate ──
    # Returns available-columns so the LLM self-corrects in the agent loop.
    # Enabled globally via SCHEMA_GRAPH_SQL_VALIDATOR_ENABLED OR per-agent via
    # context["schema_validator_enabled"] (the ask_data_agent sub-agent opts
    # in so its queries get early did-you-mean / FK-master hints).
    _ctx = context or {}
    _schema_validation_enabled = bool(
        settings.SCHEMA_GRAPH_SQL_VALIDATOR_ENABLED
        or _ctx.get("schema_validator_enabled")
    )
    _did_you_mean_enabled = bool(
        settings.SCHEMA_VALIDATOR_DID_YOU_MEAN_ENABLED
        or _ctx.get("did_you_mean_enabled")
    )
    if _schema_validation_enabled:
        try:
            from app.services.nl2sql.schema_validator import validate_against_schema
            vres = validate_against_schema(
                sql, kb_id, db, did_you_mean=_did_you_mean_enabled
            )
            if not vres.get("is_valid", True):
                errors = vres.get("errors") or ["query references unknown tables/columns"]
                logger.warning(
                    "execute_query: schema validation failed on KB %s: %s",
                    kb_id, "; ".join(str(e) for e in errors),
                )
                # Build human-readable fix guidance so the LLM can self-correct.
                # Without this, the agent sees raw available_columns dict and
                # ignores it, retrying with the same wrong column names.
                available = vres.get("available_columns", {})
                suggestions = vres.get("available_suggestions", [])
                fix_guidance = ""
                if available:
                    fix_guidance += "\n\nAVAILABLE COLUMNS on referenced tables:\n"
                    for tbl, cols in available.items():
                        shown = cols[:25] if isinstance(cols, list) else []
                        fix_guidance += f"  - {tbl}: {', '.join(str(c) for c in shown)}"
                        if isinstance(cols, list) and len(cols) > 25:
                            fix_guidance += f" (+{len(cols)-25} more)"
                        fix_guidance += "\n"
                if suggestions:
                    fix_guidance += "\nFIX SUGGESTIONS:\n"
                    for s in suggestions[:8]:
                        fix_guidance += f"  - {s}\n"
                return {
                    "success": False,
                    "error": (
                        "Query references unknown tables/columns: "
                        + "; ".join(str(e) for e in errors)
                        + fix_guidance
                    ),
                    "available_columns": available,
                    "available_suggestions": suggestions,
                    "sql": sql,
                }
        except Exception as exc:
            logger.debug("execute_query: schema validator error: %s", exc)

    def _do_work() -> dict:
        svc = QueryService(db)
        return svc.execute(kb_id, sql, max_rows=max_rows, timeout_s=timeout_s)

    try:
        result = await asyncio.to_thread(_do_work)
        hints = _build_validation_hints(db, kb_id, result)
        if hints:
            result["validation_hints"] = hints
        return {"success": True, **result}
    except DriverUnavailable as e:
        return {"success": False, "error": str(e), "error_kind": "driver_missing", "sql": sql}
    except ValueError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.warning("execute_query failed: %s", e)
        return {"success": False, "error": f"Query failed: {e}", "sql": sql}


# Name-role / id-role structural detection — mirrors answer_verification +
# catalog_indexer conventions (boundary + suffix-anchored, zero hardcoded names).
_NAME_ROLE_RE = re.compile(
    r"(^|_)(name|fname|title|label)(_|$)|(name|title|label)$",
    re.IGNORECASE,
)
_ID_ROLE_RE = re.compile(
    r"(^|_)(id|code|no|number)(_|$)|(id|code|no|number)$",
    re.IGNORECASE,
)
_FROM_TABLE_RE = re.compile(r"\bFROM\s+([`\"\[]?[\w.]+[`\"\]]?)", re.IGNORECASE)


def _parse_from_table(sql: str) -> str | None:
    """Best-effort parse of the first FROM table from generated SQL."""
    if not sql:
        return None
    m = _FROM_TABLE_RE.search(sql)
    if not m:
        return None
    return m.group(1).strip("`\"[]") or None


def _is_blank_dim_value(value: Any) -> bool:
    """Blank = None | '' | whitespace-only string. Numeric zero is NOT blank."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def _relation_partners(db: Session, kb_id: str, table: str) -> list[str]:
    """Direct relation partners of `table` from cached catalog metadata
    (KBTableRelation) — no connector, no introspection."""
    from app.models.knowledge_catalog import KBTableMeta, KBTableRelation

    meta = (
        db.query(KBTableMeta)
        .filter(KBTableMeta.kb_id == kb_id, KBTableMeta.table_name == table)
        .first()
    )
    if not meta:
        return []
    rels = (
        db.query(KBTableRelation)
        .filter(
            KBTableRelation.kb_id == kb_id,
            or_(
                KBTableRelation.source_table_meta_id == meta.id,
                KBTableRelation.target_table_meta_id == meta.id,
            ),
        )
        .all()
    )
    ids: set[int] = set()
    for r in rels:
        ids.add(r.source_table_meta_id)
        ids.add(r.target_table_meta_id)
    if not ids:
        return []
    names = dict(
        db.query(KBTableMeta.id, KBTableMeta.table_name)
        .filter(KBTableMeta.kb_id == kb_id, KBTableMeta.id.in_(ids))
        .all()
    )
    return [n for i in ids if (n := names.get(i)) and n != table]


def _find_master_for_fk(
    db: Session, kb_id: str, table: str, fk_col: str
) -> tuple[str, str, str] | None:
    """Focused schema-graph query: build over `table` + its relation partners
    and ask find_master_for_fk. Returns (master, join_col, fk_col) | None."""
    try:
        partners = _relation_partners(db, kb_id, table)
        graph = SchemaGraph(db, kb_id).build([table] + partners)
        return graph.find_master_for_fk(table, fk_col)
    except Exception as exc:  # never block the query result
        logger.debug("find_master_for_fk failed for %s.%s: %s", table, fk_col, exc)
        return None


def _blank_dimension_hint(db: Session, kb_id: str, result: dict) -> dict | None:
    """Detect a 100%-blank FIRST name-role column and suggest a master JOIN.

    Structural only — the primary name-role column must be blank (None | ''
    | whitespace) across ALL returned rows. Numeric-only or empty results
    never fire. The master lookup reuses SchemaGraph.find_master_for_fk.
    """
    rows = result.get("rows") or []
    if not rows or not isinstance(rows[0], dict):
        return None
    columns = list(rows[0].keys())
    name_cols = [c for c in columns if _NAME_ROLE_RE.search(str(c))]
    if not name_cols:
        return None
    primary = name_cols[0]
    if not all(_is_blank_dim_value(r.get(primary)) for r in rows):
        return None

    table = _parse_from_table(result.get("sql") or "")
    master_table = None
    join_desc = None
    if table:
        for col in columns:
            if not _ID_ROLE_RE.search(str(col)):
                continue
            found = _find_master_for_fk(db, kb_id, table, str(col))
            if found:
                master_table, master_col, fk_col = found
                join_desc = f"{fk_col} -> {master_col}"
                break
    if master_table:
        message = (
            f"Column '{primary}' is 100% blank in all returned rows. JOIN the "
            f"entity master table '{master_table}' via '{join_desc}' and re-query "
            f"using its name column so the answer shows real names. This fix is "
            f"deterministic from the schema graph — do not ask the user for permission."
        )
    else:
        message = (
            f"Column '{primary}' is 100% blank in all returned rows. JOIN the "
            f"entity master table connected via this table's FK/id column in the "
            f"schema graph and re-query using its name column so the answer shows "
            f"real names."
        )
    return {
        "kind": "blank_dimension",
        "column": primary,
        "master_table": master_table,
        "join": join_desc,
        "message": message,
    }


def _build_validation_hints(db: Session, kb_id: str, result: dict) -> list[dict]:
    """Advisory validation hints attached to a successful ``execute_query``.

    Computed from already-returned rows + cached coverage metadata (O(rows),
    no extra queries). The agent reads these and may retry (≤2). Never blocks.

    Hints produced:
    - ``blank_dimension`` — first name-role column is 100% blank; JOIN the
      entity master via the schema graph. Gated by SCHEMA_GRAPH_ENABLED
      (independent of KG_BUSINESS_CONTEXT_ENABLED so it can't be silently off).
    - ``empty_set`` — result has no rows; distinguish "genuinely no data"
      (coverage max_date is recent) from "no data in window / stale".
    - ``null_rate`` — % of NULL values in the first numeric-ish columns.
    - ``date_range`` — whether the result's temporal span matches coverage.
    """
    hints: list[dict] = []
    rows = result.get("rows") or []

    # blank_dimension is computed BEFORE the KG flag early-return.
    if settings.SCHEMA_GRAPH_ENABLED:
        try:
            blank = _blank_dimension_hint(db, kb_id, result)
            if blank:
                hints.append(blank)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("blank_dimension hint failed (non-fatal): %s", exc)

    if not settings.KG_BUSINESS_CONTEXT_ENABLED:
        return hints
    try:
        from app.services.knowledge_graph.business_context import (
            _coverage_max_dates,
        )
    except Exception:  # pragma: no cover - import guard
        return hints

    coverage = _coverage_max_dates(db, kb_id)
    cov_by_table = {c["table_name"]: c for c in coverage}

    # 1. Empty-set vs coverage.
    if not rows:
        if coverage:
            latest = max(
                (c["max_date"] for c in coverage if c.get("max_date")),
                default=None,
            )
            if latest:
                hints.append({
                    "kind": "empty_set",
                    "message": (
                        f"The query returned no rows. The source's latest data is "
                        f"{latest.isoformat()} — if the question asked for a period "
                        f"after that date, the data is stale (check the ERP/warehouse "
                        f"sync). Otherwise the filter may be too narrow."
                    ),
                })
        return hints

    # 2. NULL rate on leading columns.
    if rows and isinstance(rows[0], dict):
        first_row = rows[0]
        for col in list(first_row.keys())[:6]:
            total = len(rows)
            nulls = sum(1 for r in rows if r.get(col) is None)
            if total and nulls / total > 0.5:
                hints.append({
                    "kind": "null_rate",
                    "column": col,
                    "null_ratio": round(nulls / total, 2),
                    "message": (
                        f"Column '{col}' is NULL in {nulls}/{total} rows — the metric "
                        f"may be missing data or the join is wrong."
                    ),
                })
                break  # one representative column is enough

    # 3. Date-range vs coverage (best-effort: first temporal column present).
    if rows and isinstance(rows[0], dict):
        for cov in coverage:
            dc = cov.get("date_column")
            if not dc or dc not in rows[0]:
                continue
            md = cov.get("max_date")
            if md:
                hints.append({
                    "kind": "date_range",
                    "date_column": dc,
                    "coverage_max": md.isoformat(),
                    "message": (
                        f"The source's data for {dc} ends at {md.isoformat()}. "
                        f"If the question asks for a more recent period, the answer "
                        f"is 'no data after {md.isoformat()}'."
                    ),
                })
                break
    return hints


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

    policy = _resolve_user_policy(db, user_id, context)

    try:
        svc = NLAnswerService(db)
        result = await svc.answer(kb_id, question, resolved_policy=policy)
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

PROFILE_DATA_SCHEMA = {
    "type": "function",
    "function": {
        "name": "profile_data",
        "description": (
            "Profile real data of a bound data source before designing dashboards. "
            "Returns per-table row counts and per-column stats: cardinality, "
            "null ratio, min/max, top sample values, and a shape hint "
            "(time_series / category / continuous / sparse / empty), plus an "
            "ok/empty/error status per table. Call with the `table` you intend "
            "to query, and optionally `columns`; omit `columns` to profile the "
            "first 20 columns. Do NOT build a dashboard on tables whose profile "
            "status is empty or error."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "data_source_id": {
                    "type": "string",
                    "description": "The id of the bound data source to profile.",
                },
                "table": {
                    "type": "string",
                    "description": "Optional. If set, profile just this table.",
                },
                "columns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional. Columns to profile; omit to profile the "
                        "first 20 columns of the table."
                    ),
                },
                "max_tables": {
                    "type": "integer",
                    "description": (
                        "Cap on number of tables profiled when `table` is "
                        "omitted (default 8)."
                    ),
                    "default": 8,
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

# Register all seven tools. The `data_agent` subagent is the only caller —
# the runtime never auto-injects these onto the user-facing agent.
# `enabled_by_default=False` is set so the registry's default-enable
# logic does not surface them in places that don't explicitly opt in.
for _name, _schema, _handler, _desc in (
    ("list_data_sources", LIST_DATA_SOURCES_SCHEMA, _list_data_sources,
     "List data sources bound to this agent."),
    ("describe_schema", DESCRIBE_SCHEMA_SCHEMA, _describe_schema,
     "Introspect the schema of a bound data source."),
    ("profile_data", PROFILE_DATA_SCHEMA, _profile_data,
     "Profile real data (row counts, cardinality, null ratio, shapes) of a bound data source."),
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
