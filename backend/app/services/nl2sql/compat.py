"""Compatibility adapter for the legacy ``nl2sql.ask()`` entry point.

``nl2sql.ask()`` predates the grounded data-agent path (``db/nl_answer_service``
+ ``db/execution_gate``). It resolved a question against an
``AgentDataBinding``'s raw datasource config, generated SQL, validated it with
the legacy validator, and executed it THROUGH ITS OWN ADAPTER — bypassing the
source readiness gate, the execution boundary, the grounded plan and the
canonical result set. Two pipelines meant two answers for the same question, and
the legacy one was the weaker.

This module makes the grounded path the one implementation and turns the legacy
caller into a thin adapter over it. What the adapter is allowed to do:

* resolve WHICH source the legacy inputs name (never guess, never default),
* convert a binding's / policy-config's table, column and row restrictions into
  the ``ResolvedPolicy`` shape the canonical path already enforces,
* run the canonical answer,
* map its result onto the legacy ``NL2SQLResult`` keys — carrying ``plan``,
  ``result_set``, ``unsupported_slots``, ``disclosure`` and ``provenance``
  through, never dropping them.

What it must never do: author or execute SQL itself, open a connector, or reach
the database except through ``QueryService.execute`` (the execution boundary).
There is no code path in here that touches a driver.

Nothing in this module names a database, schema, table, dialect or customer: the
source id is whatever the caller's own data says it is, and an unresolvable
source is a refusal, not a fallback to "the first database".
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


#: Business-language refusal for a question whose source could not be
#: identified. Plumbing (which input was missing) stays in ``detail``.
_UNRESOLVED_SOURCE = (
    "This question could not be answered: no data source could be identified "
    "for it. No figure is reported."
)


@dataclass
class SourceResolution:
    """Which source a legacy call actually names — or why none could be named."""

    kb_id: str = ""
    kb: Any = None
    binding: Any = None
    binding_id: str = ""
    error: str = ""
    error_kind: str = ""
    detail: str = ""
    candidates: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.kb_id) and not self.error


# ── source resolution ──────────────────────────────────────────────────


def _load_kb(db: Session, kb_id: str) -> Any:
    """A database-kind KnowledgeBase by id, or ``None``."""
    if not kb_id:
        return None
    try:
        from app.models.knowledge_base import KnowledgeBase

        kb = (
            db.query(KnowledgeBase)
            .filter(KnowledgeBase.id == kb_id, KnowledgeBase.is_deleted == False)  # noqa: E712
            .first()
        )
    except Exception as e:
        logger.debug("nl2sql compat: source lookup failed for %s: %s", kb_id, e)
        return None
    if kb is None:
        return None
    if (kb.source_kind or "").lower() != "database":
        logger.debug(
            "nl2sql compat: %s is not a database source (source_kind=%r)",
            kb_id, kb.source_kind,
        )
        return None
    return kb


def _norm_dialect(value: Any) -> str:
    """Canonical dialect family for a raw string — never assumes one."""
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    try:
        from app.services.db.dialect import profile_for

        return profile_for(raw).name
    except Exception:
        return raw


def _match_kb_for_connection(db: Session, config: dict[str, Any]) -> tuple[Any, list[str]]:
    """The KB whose own connection identity equals this datasource config.

    Deterministic equality on dialect + host + port + database only. A config
    that matches nothing — or more than one source — resolves to nothing: two
    candidates is not a licence to pick one.
    """
    if not config:
        return None, []
    dialect = _norm_dialect(config.get("dialect") or config.get("engine"))
    host = str(config.get("host") or "").strip().lower()
    try:
        port = int(config.get("port") or 0)
    except Exception:
        port = 0
    database = str(
        config.get("database") or config.get("dbname") or config.get("database_name") or ""
    ).strip().lower()
    if not dialect and not host and not database:
        return None, []

    try:
        from app.models.knowledge_base import KnowledgeBase

        rows = (
            db.query(KnowledgeBase)
            .filter(
                KnowledgeBase.is_deleted == False,  # noqa: E712
                KnowledgeBase.source_kind == "database",
            )
            .all()
        )
    except Exception as e:
        logger.debug("nl2sql compat: connection match unavailable: %s", e)
        return None, []

    matches: list[Any] = []
    for kb in rows:
        if dialect and _norm_dialect(kb.db_type) != dialect:
            continue
        if host and str(kb.host or "").strip().lower() != host:
            continue
        if port and int(kb.port or 0) != port:
            continue
        if database and str(kb.database_name or "").strip().lower() != database:
            continue
        matches.append(kb)
    if len(matches) == 1:
        return matches[0], [str(matches[0].id)]
    return None, [str(m.id) for m in matches]


def _load_binding(db: Session, binding_id: str) -> Any:
    """An ``AgentDataBinding`` row by id, or ``None`` (a synthetic id has no row)."""
    if not binding_id:
        return None
    try:
        from app.models.agent_data_binding import AgentDataBinding

        return (
            db.query(AgentDataBinding)
            .filter(
                AgentDataBinding.id == binding_id,
                AgentDataBinding.is_deleted == False,  # noqa: E712
            )
            .first()
        )
    except Exception as e:
        logger.debug("nl2sql compat: binding lookup failed for %s: %s", binding_id, e)
        return None


def resolve_source(
    db: Session,
    *,
    kb_id: str | None = None,
    binding_id: str | None = None,
    datasource_config: dict[str, Any] | None = None,
) -> SourceResolution:
    """Identify the source a legacy call names, without guessing.

    Resolution order, each step an EXPLICIT statement of identity:

    1. ``kb_id`` — the caller already knows the source.
    2. ``datasource_config["kb"]`` / ``["kb_id"]`` — the caller handed over the
       source object itself (how the workflow runtime calls in).
    3. ``binding_id`` naming a KnowledgeBase — the id space agents are bound to
       (``data_source_id`` in tool args is a KB id).
    4. ``binding_id`` naming an ``AgentDataBinding`` → the KB its datasource
       is (by id, else by connection identity).
    5. a bare ``datasource_config`` connection dict → the KB with that same
       connection identity.

    Zero matches and multiple matches are both refusals. There is no
    "first database", no default schema, no query-all-sources behaviour.
    """
    res = SourceResolution(binding_id=str(binding_id or ""))

    # 1. explicit source
    kb = _load_kb(db, str(kb_id or ""))
    if kb is not None:
        res.kb, res.kb_id = kb, str(kb.id)
        res.binding = _load_binding(db, str(binding_id or ""))
        return res

    config = dict(datasource_config or {})

    # 2. the source object / id handed over in the config
    for key in ("kb", "kb_id", "knowledge_base_id", "data_source_id"):
        held = config.get(key)
        if held is None:
            continue
        cached = held if not isinstance(held, str) else _load_kb(db, held)
        if cached is not None:
            res.kb, res.kb_id = cached, str(getattr(cached, "id", "") or cached)
            res.binding = _load_binding(db, str(binding_id or ""))
            return res

    # 3/4. the binding id itself
    binding = _load_binding(db, res.binding_id)
    if binding is not None:
        res.binding = binding
    else:
        kb = _load_kb(db, res.binding_id)
        if kb is not None:
            res.kb, res.kb_id = kb, str(kb.id)
            return res

    if binding is not None:
        held = str(getattr(binding, "datasource_id", "") or "")
        kb = _load_kb(db, held)
        if kb is not None:
            res.kb, res.kb_id = kb, str(kb.id)
            return res
        if held:
            try:
                from app.models.datasource import Datasource

                ds = (
                    db.query(Datasource)
                    .filter(Datasource.id == held, Datasource.is_deleted == False)  # noqa: E712
                    .first()
                )
            except Exception as e:
                logger.debug("nl2sql compat: datasource lookup failed for %s: %s", held, e)
                ds = None
            if ds is not None:
                cfg = dict(ds.connection_config or {})
                cfg.setdefault("dialect", ds.engine)
                matched, candidates = _match_kb_for_connection(db, cfg)
                res.candidates = candidates
                if matched is not None:
                    res.kb, res.kb_id = matched, str(matched.id)
                    return res

    # 5. a bare connection dict
    if config and not res.candidates:
        matched, candidates = _match_kb_for_connection(db, config)
        res.candidates = candidates
        if matched is not None:
            res.kb, res.kb_id = matched, str(matched.id)
            return res

    res.error = _UNRESOLVED_SOURCE
    res.error_kind = "source_not_resolved"
    if len(res.candidates) > 1:
        res.detail = (
            f"{len(res.candidates)} sources match this connection "
            f"({', '.join(res.candidates[:5])}) — the caller must name one"
        )
    elif binding_id and binding is None:
        res.detail = (
            f"binding_id {binding_id!r} is neither a data source nor a data "
            f"binding on this platform"
        )
    else:
        res.detail = "no data source could be identified from the call's inputs"
    logger.warning("nl2sql compat: %s (binding_id=%r)", res.detail, binding_id)
    return res


# ── policy: the legacy restrictions, in the canonical shape ────────────


def _as_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if str(v)]
    if value:
        return [str(value)]
    return []


def _table_of(key: str) -> str:
    """``"orders.region"`` / ``"orders"`` → a bare table name, lower-cased."""
    return str(key or "").split(".")[-1].strip().lower()


def policy_from_binding(kb_id: str, binding: Any, policy_config: Any = None):
    """The legacy restrictions as a ``ResolvedPolicy``.

    Both legacy carriers are honoured: an ``AgentDataBinding`` row (allowed /
    blocked tables, column allowlists, row filters) and a legacy
    ``PolicyConfig``. The result is the shape the canonical path already
    enforces — schema filtering before SQL generation, and the execution
    boundary afterwards — so a restriction cannot be applied on one path and
    forgotten on the other.

    Returns ``None`` when nothing is configured, which the boundary records as
    ``not-in-scope`` rather than as a pass.
    """
    from app.services.access_policy_service import ResolvedPolicy

    allowed_tables: list[str] = []
    blocked_tables: list[str] = []
    column_allowlists: dict[tuple[str, str], list[str]] = {}
    row_filters: dict[tuple[str, str], dict] = {}
    allowed_columns: dict[str, Any] = {}

    if policy_config is not None:
        allowed_tables = _as_list(getattr(policy_config, "allowed_tables", None))
        blocked_tables = _as_list(
            getattr(policy_config, "block_tables", None)
            or getattr(policy_config, "blocked_tables", None)
        )
        cand = getattr(policy_config, "allowed_columns", None)
        allowed_columns = dict(cand) if isinstance(cand, dict) else {}

    if binding is not None:
        if not allowed_tables:
            allowed_tables = _as_list(getattr(binding, "allowed_tables", None))
        if not blocked_tables:
            blocked_tables = _as_list(
                getattr(binding, "blocked_tables", None)
                or getattr(binding, "block_tables", None)
            )
        cand = getattr(binding, "allowed_columns", None)
        if isinstance(cand, dict) and not allowed_columns:
            allowed_columns = dict(cand)
        raw_filters = getattr(binding, "row_filters", None)
        if isinstance(raw_filters, str):
            try:
                import json

                raw_filters = json.loads(raw_filters)
            except Exception:
                raw_filters = None
        if isinstance(raw_filters, dict):
            for key, value in raw_filters.items():
                if isinstance(value, dict):
                    row_filters[(kb_id, _table_of(key))] = dict(value)
                else:
                    row_filters[(kb_id, _table_of(key))] = {str(key): value}
        elif isinstance(raw_filters, (list, tuple)):
            for entry in raw_filters:
                if not isinstance(entry, dict):
                    continue
                table = _table_of(entry.get("table") or entry.get("table_name") or "")
                row_filters[(kb_id, table)] = dict(entry)

    if not (allowed_tables or blocked_tables or column_allowlists or row_filters or allowed_columns):
        return None

    policy = ResolvedPolicy(has_policies=True)
    # An allow-list is a whitelist: everything not named is refused. Blocked
    # tables stay refused either way.
    if allowed_tables:
        policy.allowlisted_kbs[kb_id] = {_table_of(t) for t in allowed_tables}
    for table in blocked_tables:
        policy.denied_tables[(kb_id, _table_of(table))] = "binding:blocked_tables"
    for table, columns in allowed_columns.items():
        cols = _as_list(columns)
        if cols:
            policy.column_allowlists[(kb_id, _table_of(table))] = cols
    policy.row_filters.update(row_filters)
    return policy


def row_filters_for(policy: Any, kb_id: str) -> list[dict]:
    """The policy's row filters, as the execution boundary takes them.

    Passed as enforcement CONTEXT, not as SQL the adapter authors: the boundary
    verifies the statement carries the filter, and refuses it when it does not.
    That is the difference between "we filtered" and "we meant to filter".
    """
    if policy is None:
        return []
    out: list[dict] = []
    for (kb, _table), spec in (getattr(policy, "row_filters", None) or {}).items():
        if kb != kb_id or not isinstance(spec, dict):
            continue
        out.append(dict(spec))
    return out


# ── running the canonical path from a synchronous legacy caller ────────


def run_canonical(
    db: Session,
    kb_id: str,
    question: str,
    *,
    resolved_policy: Any = None,
    row_filters: list[dict] | None = None,
    project_id: str | None = None,
    endpoint: Any = None,
    skip_narrate: bool = False,
    agent_name: str | None = None,
    conversation_id: str | None = None,
    turn_id: str | None = None,
) -> dict:
    """Run ``NLAnswerService.answer`` for a synchronous caller.

    The legacy entry point is synchronous (a FastAPI sync endpoint, a workflow
    node executor) while the canonical path is a coroutine. When a loop is
    already running, the coroutine is run on a worker thread rather than driving
    the running loop re-entrantly.

    ``turn_id``/``conversation_id`` (P0-3, 2026-10-30 audit D4): threaded to
    the answer so the per-turn SQL budget applies at its proper turn scope
    instead of degrading to conversation scope.
    """
    from app.services.db.nl_answer_service import NLAnswerService

    service = NLAnswerService(db)
    service.row_filters = list(row_filters or [])
    coro = service.answer(
        kb_id,
        question,
        resolved_policy=resolved_policy,
        project_id=project_id,
        endpoint=endpoint,
        skip_narrate=skip_narrate,
        agent_name=agent_name,
        conversation_id=conversation_id,
        turn_id=turn_id,
    )
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro)).result()


# ── the legacy result view ─────────────────────────────────────────────


def _query_result_from_rows(rows: list[dict], sql: str, elapsed_ms: int):
    """A ``QueryResult`` for legacy callers that read ``result.data``.

    Built from rows the canonical path already fetched — no execution, no
    connector. ``QueryResult.rows`` is a list of tuples in this codebase.
    """
    from app.services.datasources import QueryResult

    columns = list(rows[0].keys()) if rows and isinstance(rows[0], dict) else []
    tuples = [tuple(row.get(c) for c in columns) for row in rows if isinstance(row, dict)]
    return QueryResult(
        columns=columns,
        rows=tuples,
        row_count=len(tuples),
        duration_ms=elapsed_ms,
    )
