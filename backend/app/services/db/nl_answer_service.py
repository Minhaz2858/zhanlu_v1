"""NLAnswerService — end-to-end NL2SQL pipeline.

Pipeline:
  1. Introspect schema for the bound KB
  2. Ask the LLM to produce SQL (text → SQL), grounded in the schema
  3. Execute the SQL via QueryService
  4. Ask the LLM to narrate the answer in natural language

This is the "high-level" tool used by `answer_from_database` and by the
builtin Data Agent's main flow. It returns BOTH the structured payload
(rows, sql, source) AND a default prose narrative, so callers can
pick whichever fits.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.services.db.query_service import QueryService
from app.services.db.schema_service import SchemaService
from app.services.llm_router import LLMEndpoint
from app.services.llm_service import llm_headers, llm_url, get_model

logger = logging.getLogger(__name__)


def _connection_context(schema_svc: SchemaService, kb_id: str) -> str:
    """Return a secret-free connection description for a KB, for error hints.

    Includes only db_type/host/port/database_name/schema so the Data Agent LLM
    can see *why* a schema read failed without exposing credentials.
    """
    try:
        kb = schema_svc._load_kb(kb_id)
    except Exception:
        return "(data source context unavailable)"
    parts = [f"db_type={kb.db_type or 'unknown'}"]
    if getattr(kb, "host", None):
        parts.append(f"host={kb.host}")
    if getattr(kb, "port", None):
        parts.append(f"port={kb.port}")
    if getattr(kb, "database_name", None):
        parts.append(f"database={kb.database_name}")
    if getattr(kb, "schema", None):
        parts.append(f"schema={kb.schema}")
    return f"Data source ({', '.join(parts)})."


_MULTI_STMT_NL = re.compile(r";\s*(?=(?:SELECT|WITH)\b)", re.IGNORECASE)


_MISSING_IDENTIFIER_PATTERNS = (
    re.compile(r"Unknown column '([^']+)'", re.I),            # MySQL 1054
    re.compile(r"Unknown table '([^']+)'", re.I),             # MySQL 1109
    re.compile(r"Table '([^']+)' doesn't exist", re.I),       # MySQL 1146
    re.compile(r"no such column: ?([^\s'\"]+)", re.I),        # SQLite
    re.compile(r"no such table: ?([^\s'\"]+)", re.I),         # SQLite
    re.compile(r"column \"?([\w.]+)\"? does not exist", re.I),   # Postgres 42703
    re.compile(r"relation \"?([\w.]+)\"? does not exist", re.I),  # Postgres 42P01
    re.compile(r"Invalid column name '([^']+)'", re.I),       # MSSQL 207
    re.compile(r"ORA-00904[^:]*: ?\"?([\w$#]+)\"?", re.I),     # Oracle invalid identifier
)

# Malformed SQL is also the model's to fix, and the driver says so explicitly.
_SQL_SYNTAX_ERROR_RE = re.compile(
    r"\(1064,|You have an error in your SQL syntax|syntax error at or near|"
    r"ORA-00933|Incorrect syntax near",
    re.I,
)


def _apply_sql_rewrite_stage(
    sql: str,
    *,
    max_rows: int | None = None,
    unique_columns: dict[str, set[str]] | None = None,
):
    """OCI stage 5 — the deterministic SQL rewrite pass, flag-gated.

    Returns the ``RewriteResult`` (``None`` when the flag is off or the pass
    is unavailable), never raises. The rewritten statement still passes the
    unconditional execution gate inside ``QueryService.execute`` — this is an
    optimization between authoring and execution, never a second execution
    path. Used by the sealed NL path AND the ``execute_query`` tool.
    """
    if not getattr(settings, "NL2SQL_SQL_REWRITE_ENABLED", True):
        return None
    try:
        from app.services.db.sql_rewriter import rewrite_sql

        return rewrite_sql(sql, max_rows=max_rows, unique_columns=unique_columns)
    except Exception as e:  # a failed optimization must never break a query
        logger.debug("NLAnswer: sql rewrite stage unavailable: %s", e)
        return None


def _sanitize_llm_sql(sql: str) -> str:
    """Hygiene pass for LLM-generated SQL before execution (2026-08-28).

    Observed on the Sales Performance Dashboard build (conv 5f2c2c39):
    deepseek emitted ``DATE_FORMAT(PLANDATE, '%%Y-%%m')`` (Python-style
    doubled percents) and pasted TWO SELECTs in one response. Both produce
    MySQL 1064s that kill the whole data step. Single-pass cleanup:
    - collapse ``%%`` -> ``%`` (safe for DATE_FORMAT / LIKE in LLM SQL),
    - keep only the FIRST statement (MySQL multi-statements are disabled;
      the model usually pastes alternates after the primary query).
    """
    if not sql:
        return sql
    cleaned = sql.replace("%%", "%")
    cleaned = _MULTI_STMT_NL.split(cleaned, maxsplit=1)[0]
    return cleaned.strip()


class NLAnswerService:
    """Orchestrate text→SQL→execute→narrate for a KnowledgeBase."""

    def __init__(self, db: Session):
        self._db = db
        #: row filters this turn must be verified against (enforcement context —
        #: see db/execution_gate.py). Set by the caller that resolved a policy;
        #: empty means "nothing configured", which the boundary records as
        #: ``not-applicable`` rather than as a pass.
        self.row_filters: list[dict] = []

    async def answer(
        self,
        kb_id: str,
        question: str,
        resolved_policy=None,
        project_id: str | None = None,
        endpoint: LLMEndpoint | None = None,
        skip_narrate: bool = False,
        agent_name: str | None = None,
        conversation_id: str | None = None,
    ) -> dict:
        """Public entry point: gate + answer + readiness disclosure.

        Every caller goes through here, so the readiness gate cannot be
        bypassed by a second entry point (the previous design had one path in
        ``db_tools`` and another in the nl2sql router, only one of which asked).

        ``conversation_id`` (optional) enables the planner's structured
        multi-turn state: resolved entities/defaults carried across turns of
        one conversation (OCI P1-6). None = stateless single-shot, unchanged.
        """
        result = await self._answer_impl(
            kb_id,
            question,
            resolved_policy=resolved_policy,
            project_id=project_id,
            endpoint=endpoint,
            skip_narrate=skip_narrate,
            agent_name=agent_name,
            conversation_id=conversation_id,
        )
        return self._attach_readiness_directive(result, kb_id)

    def _attach_readiness_directive(self, result, kb_id: str):
        """Attach a limited-source disclosure to a successful answer."""
        try:
            from app.services.db import readiness as _src_readiness

            verdict = _src_readiness.evaluate_source(self._db, kb_id)
            if not verdict.limited or not verdict.disclosure:
                return result
            if isinstance(result, dict):
                result.setdefault("disclosure", verdict.disclosure)
                result.setdefault(
                    "source_readiness",
                    {
                        "state": verdict.state,
                        "code": verdict.code,
                        "limited": True,
                        "disclosure": verdict.disclosure,
                    },
                )
        except Exception:
            logger.debug("NLAnswer: readiness disclosure skipped", exc_info=True)
        return result

    async def _answer_impl(
        self,
        kb_id: str,
        question: str,
        resolved_policy=None,
        project_id: str | None = None,
        endpoint: LLMEndpoint | None = None,
        skip_narrate: bool = False,
        agent_name: str | None = None,
        conversation_id: str | None = None,
    ) -> dict:
        """Return a structured + prose answer for a user question.

        ``resolved_policy`` is an optional
        ``access_policy_service.ResolvedPolicy`` for the current user.  When
        provided, denied tables/columns are stripped from the schema context
        before text→SQL, and the generated SQL is validated against the policy
        before execution.

        Returns:
            {
                "success": True,
                "answer": "<prose>",
                "rows": [...],
                "sql": "...",
                "source_id": "...",
                "source_name": "...",
                "citations": ["table.col", ...],
                "iterations": N,
            }
        """
        if not question or not question.strip():
            return {"success": False, "error": "Question is required"}

        if resolved_policy is not None and resolved_policy.is_kb_fully_denied(kb_id):
            return {
                "success": False,
                "error": "Access to this data source is restricted.",
                "source_id": kb_id,
            }

        # 0-pre. Source readiness gate. This is the last gate before planning,
        # retrieval and SQL generation, so it is the one that matters: a source
        # whose structure was never catalogued, or whose preparation failed or
        # stalled, has no trustworthy table/column knowledge to plan against.
        # Refusing here (with a business-language disclosure the caller passes
        # through) replaces the old behaviour of answering from whatever the
        # live introspection happened to return.
        #
        # A partially prepared source is allowed through and its limitation is
        # attached to the result for the caller to disclose.
        from app.services.db import readiness as _src_readiness

        _readiness, _refusal = _src_readiness.check_source(self._db, kb_id)
        if _refusal:
            _refusal["source_id"] = kb_id
            return _refusal

        # Turn-scoped artifacts the grounded plan reads. Initialised here so the
        # plan can be built on EVERY path (TOC, linker, M-Schema) without a
        # branch-specific variable being undefined.
        _window: tuple | None = None
        _toc_snapshot: list | None = None
        _catalog_names: set[str] = set()

        # 0. Freshness short-circuit (deterministic, ~1s, no SQL): if the
        # question's relative window starts after every probed table's
        # max_date, answer immediately with a plain-language stale-data
        # statement instead of scanning the full table.
        if settings.KG_FRESHNESS_SHORTCIRCUIT_ENABLED:
            try:
                from app.services.knowledge_graph.business_context import (
                    parse_relative_window,
                    freshness_verdict,
                )
                window = parse_relative_window(question)
                # Share the parsed window with candidate retrieval and the
                # zero-row correction: the same "2026-09-01..2026-10-01" that
                # decides staleness is what marks a candidate
                # "NO DATA IN REQUESTED WINDOW" at table level. A source where
                # only SOME tables are stale must not be answered as a refusal —
                # that is the correction's job, not the short-circuit's.
                _window = window
                verdict = freshness_verdict(self._db, kb_id, window)
                if verdict and verdict.get("stale"):
                    logger.info(
                        "NLAnswer: freshness short-circuit hit for KB %s "
                        "(window=%s, max_date=%s)",
                        kb_id, window, verdict.get("max_date"),
                    )
                    return {
                        "success": True,
                        "answer": verdict["message"],
                        "rows": [],
                        "sql": None,
                        "source_id": kb_id,
                        "source_name": None,
                        "citations": [],
                        "iterations": 0,
                        "short_circuit": "freshness",
                    }
            except Exception as e:
                logger.warning("NLAnswer: freshness short-circuit skipped: %s", e)

        schema_svc = SchemaService(self._db)
        query_svc = QueryService(self._db)
        # Enforcement context (never a switch): the gate runs on every execution
        # regardless, and validates against the user's policy when there is one.
        query_svc.policy = resolved_policy
        # Row filters the policy resolved for this source are handed to the
        # boundary as context, so a filtered query is verified to actually be
        # filtered. Callers that already resolved their own (the legacy adapter)
        # keep theirs.
        if not getattr(self, "row_filters", None) and resolved_policy is not None:
            try:
                query_svc.row_filters = [
                    dict(spec)
                    for (kb, _table), spec in (getattr(resolved_policy, "row_filters", None) or {}).items()
                    if kb == kb_id and isinstance(spec, dict)
                ]
            except Exception as e:  # pragma: no cover - policy shapes vary
                logger.debug("NLAnswer: row-filter context unavailable: %s", e)
        else:
            query_svc.row_filters = list(getattr(self, "row_filters", None) or [])

        # ── C1 (2026-08-26): static intent resolver ──
        # Known business questions ("top customer for last month", "sales
        # report", ...) route DIRECTLY to the known-correct table, skipping
        # the vector schema linker — eliminating the "wrong table → 0 rows"
        # failure mode at its source. The NL model still writes the SQL; we
        # only anchor the table. Falls through to normal selection when the
        # route's table doesn't exist in the KB.
        _static_route: dict | None = None
        # NOTE: the setting may be None (unset) — treat None as enabled.
        if getattr(settings, "STATIC_QUERY_ROUTING_ENABLED", True) is not False:
            try:
                from app.services.db.query_router import resolve_static_route
                _static_route = resolve_static_route(question, agent_name=agent_name)
            except Exception as e:
                logger.warning(
                    "NLAnswer: static route resolver failed (non-fatal): %s", e,
                )
                _static_route = None

        # ── C1-pre (2026-09-22): OCI agentic planning stage ──
        # Resolve WHAT is being asked (entity / metric / time / slice) against
        # the semantic store's VERIFIED vocabulary BEFORE any retrieval or
        # generation. Two outcomes, both fail-soft:
        #   * proceed — the <resolved-intent> block steers SQL generation and
        #     the assumptions ride on the result payload;
        #   * clarify (flag-gated) — the question is genuinely ambiguous, so
        #     return structured options instead of guessing; the calling
        #     agent relays them through its existing clarify channel and the
        #     user's answer narrows the next turn.
        _planning: dict | None = None
        if getattr(settings, "NL2SQL_PLANNER_ENABLED", False):
            try:
                from app.services.db import nl2sql_planner

                _planning = await nl2sql_planner.plan_question(
                    self._db,
                    kb_id,
                    question,
                    endpoint=endpoint,
                    project_id=project_id,
                    conversation_id=conversation_id,
                )
            except Exception as e:
                logger.debug("NLAnswer: planner failed (non-fatal): %s", e)
                _planning = None
            if _planning is not None and _planning.get("needs_clarification"):
                _clar = _planning.get("clarification") or {}
                logger.info(
                    "NLAnswer: planner requested clarification for KB %s "
                    "(confidence=%.2f)",
                    kb_id, _planning.get("confidence", 0.0),
                )
                return {
                    "success": False,
                    "needs_clarification": True,
                    "answer": _clar.get("question", ""),
                    "clarification": _clar,
                    "source_id": kb_id,
                    "iterations": 0,
                    # Instruction for the CALLING agent LLM: it owns the user
                    # channel (the NL path does not) — it either relays the
                    # options or applies a known default itself.
                    "guidance": (
                        "The data question is ambiguous. Ask the user the "
                        "clarification question below (or resolve it from "
                        "earlier conversation context) BEFORE retrying "
                        "answer_from_database with the narrowed question."
                    ),
                }

        # 1. Pull schema — TWO-PHASE approach:
        #   Phase A: Show LLM a full table-of-contents (ALL tables) so it can
        #            choose the most relevant ones.  This fixes the #1 cause of
        #            empty-row results: the vector-based schema linker picking
        #            the wrong tables.
        #   Phase B: Feed the LLM full DDL for only the selected tables so it
        #            can generate accurate SQL.
        # Fallback: if Phase A fails, fall back to the original RRF top_k=8
        #           schema linker → M-Schema path.
        schema_info: dict | None = None
        schema_text: str | None = None
        _selected_via_toc = False  # track whether Phase A succeeded
        # The question's relative window ("this month"), parsed once and shared
        # by the freshness check, candidate coverage annotation and the
        # zero-row correction. None when the question names no period.
        _window: tuple | None = None
        # The catalog TOC and the candidate retrieval for this turn, kept so
        # the answer can carry provenance about which tables it drew on.
        _toc_snapshot: list | None = None
        _retrieval = None

        # ── C1b: static route → pin the table before ANY LLM selection ──
        # If the intent resolver hit, load DDL for the pinned table directly
        # (same machinery as TOC Phase B) and skip Phase A + the linker.
        if (
            _static_route
            and settings.SCHEMA_LINKING_ENABLED
            and settings.SEMANTIC_CATALOG_ENABLED
        ):
            try:
                from app.services.knowledge_graph.schema_linker import (
                    get_selected_tables_ddl,
                )
                _route_table = _static_route["table"]
                ddl_text = await asyncio.to_thread(
                    get_selected_tables_ddl,
                    self._db, [kb_id], [_route_table],
                )
                if ddl_text and ddl_text.strip():
                    schema_info = _schema_info_from_toc_selection(
                        self._db, kb_id, [_route_table], schema_svc,
                    )
                    schema_text = ddl_text
                    # Column/date hints make the pinned table usable on the
                    # first attempt (e.g. PLANDATE for date filters) instead
                    # of the model guessing a wrong date column → 0 rows.
                    _date_hint = _static_route.get("date_hint")
                    if _date_hint:
                        schema_text = ddl_text + "\n-- " + _date_hint + "\n"
                    _selected_via_toc = True
                    logger.info(
                        "NLAnswer: static route pinned table=%s (KB %s)",
                        _route_table, kb_id,
                    )
                else:
                    # Pinned table missing from KB → fall through to normal
                    # selection (Phase A / linker).
                    logger.warning(
                        "NLAnswer: static route table %s not found in KB %s "
                        "— falling back to normal selection",
                        _route_table, kb_id,
                    )
                    _static_route = None
            except Exception as e:
                logger.warning(
                    "NLAnswer: static route DDL failed (non-fatal): %s", e,
                )
                _static_route = None

        # ── Phase A: Full table-of-contents → LLM selects tables ──
        # Skipped when C1b already pinned a static-route table (the pin is the
        # authoritative selection; re-running Phase A here would let the model
        # clobber the pinned schema with its own TOC pick — observed: contract
        # route pinned erp_v_contract_execution but Phase A overwrote it with
        # erp_t_crm_contract).
        if (
            not _selected_via_toc
            and settings.SCHEMA_LINKING_ENABLED
            and settings.SEMANTIC_CATALOG_ENABLED
        ):
            try:
                from app.services.knowledge_graph.schema_linker import (
                    build_full_toc,
                    format_toc_for_selection,
                    get_selected_tables_ddl,
                )
                from app.services.db.table_retrieval import (
                    candidate_names,
                    format_candidates_block,
                    reroute_uncovered,
                    retrieve_for_question,
                )
                toc = build_full_toc(self._db, [kb_id])
                _toc_snapshot = toc
                if toc:
                    # The FULL catalog's table names. Selection is validated
                    # against this, never against a narrowed scope: a question
                    # can legitimately need a table the scope guessed away, and
                    # a name that is not in the catalog does not exist.
                    _catalog_names = {t["table_name"] for t in toc}

                    # ── Deterministic candidate retrieval (the step that was
                    # missing). Ranks the catalog for THIS question from the
                    # semantic foundation (names/aliases/domains), verified
                    # plans and the workload, and annotates each hit with its
                    # catalog data window. It ORDERS — it never filters, so a
                    # retrieval miss degrades to a lower rank, not to a lost
                    # table.
                    _window = None
                    try:
                        from app.services.knowledge_graph.business_context import (
                            parse_relative_window,
                        )
                        _window = parse_relative_window(question)
                    except Exception:
                        _window = None
                    _retrieval = await asyncio.to_thread(
                        lambda: retrieve_for_question(
                            self._db, [kb_id], question, toc,
                            project_id=project_id, window=_window,
                        )
                    )
                    logger.info(
                        "NLAnswer: candidate retrieval → %d candidates "
                        "(method=%s, window=%s, uncovered=%s) top=%s",
                        len(_retrieval.candidates), _retrieval.method,
                        _retrieval.window, _retrieval.uncovered,
                        _retrieval.names[:5],
                    )

                    # Candidate-first surface. The legacy renderer walked the
                    # catalog ALPHABETICALLY and stopped at a 1,200-token
                    # budget, so on a 143-table warehouse the model picked from
                    # 14 tables — all ``_``/``a``-prefixed, none of them
                    # business tables. Ranked candidates are now rendered in
                    # full before anything else, and the rest compactly.
                    toc_text = format_toc_for_selection(toc, _retrieval.names)

                    # Domain match: EVIDENCE, never an exclusion. It used to
                    # narrow ``toc`` without re-rendering ``toc_text`` (so the
                    # model never saw the narrowed set) and, measured live, it
                    # scoped a Chinese contract question into "User
                    # Authentication and Permissions". Retrieval already boosts
                    # matched domains; here it only labels.
                    if getattr(settings, "KG_DOMAIN_SCOPING_ENABLED", False):
                        try:
                            from app.services.knowledge_graph.domain_discovery import (
                                scoped_tables_for_question,
                            )

                            scoped, matched = await asyncio.to_thread(
                                scoped_tables_for_question,
                                self._db,
                                [kb_id],
                                question,
                            )
                            if scoped and matched:
                                # A domain is only ADVERTISED to the selector when
                                # the question's own vocabulary appears in it.
                                # Measured live: the domain ranker put a Chinese
                                # contract question in "User Authentication and
                                # Permissions", and this line then told the model
                                # that was the matched domain — which is how the
                                # eight auth_* tables got picked.
                                from app.services.db.table_retrieval import (
                                    domain_support as _domain_support,
                                )

                                scored = [
                                    d for d in matched if _domain_support(question, d)
                                ]
                                if scored:
                                    domains_label = ", ".join(
                                        f"{d.get('name_en')}"
                                        + (f" / {d['name_zh']}" if d.get("name_zh") else "")
                                        for d in scored
                                    )
                                    logger.info(
                                        "NLAnswer: domain annotation %s "
                                        "(%d/%d matched domains carry the question's "
                                        "vocabulary)",
                                        [d.get("name_en") for d in scored],
                                        len(scored), len(matched),
                                    )
                                    toc_text = (
                                        f"Matched business domains: {domains_label}\n"
                                        + toc_text
                                    )
                                else:
                                    logger.info(
                                        "NLAnswer: domain matches %s share no "
                                        "vocabulary with the question — not "
                                        "advertised to the selector",
                                        [d.get("name_en") for d in matched],
                                    )
                        except Exception as e:
                            logger.warning("NLAnswer: domain scoping skipped: %s", e)

                    # Retrieval evidence last so it sits adjacent to the
                    # question and cannot be pushed out of the prompt.
                    _block = format_candidates_block(_retrieval)
                    if _block:
                        toc_text = f"{_block}\n\n{toc_text}"

                    # Ask the LLM which tables are relevant
                    selected_names = await self._select_tables_from_toc(
                        question, toc_text, len(toc), endpoint=endpoint,
                        known_names=_catalog_names,
                    )
                    if not selected_names and _retrieval.candidates:
                        # The model named no table that exists. Use the
                        # deterministic candidate set instead of passing the
                        # invented names through — the passthrough is how
                        # "table 'contracts' doesn't exist" reached execution.
                        selected_names = candidate_names(_retrieval)
                        logger.info(
                            "NLAnswer: selection contained no existing table — "
                            "falling back to retrieval top-%d %s",
                            len(selected_names), selected_names,
                        )

                    # ── Coverage reroute: a selection the catalog's own window
                    # has already convicted never reaches SQL. Until now the
                    # per-candidate `covers_window` verdict was rendered into
                    # the prompt and the answer and ACTED ON by nothing, so a
                    # table whose data ends before the asked-for period was
                    # queried anyway, returned 0 rows, and the turn narrated the
                    # emptiness. Measured live (2026-09-20, 示例工厂本周销售情况):
                    # the selector picked forecast_accuracy_log (max_date
                    # 2026-09-05) over the rank-1 erp_v_sale_huizhou (max_date
                    # 2026-09-16) because the former is a REAL catalog table —
                    # which is all the old fallback checked. Swap the convicted
                    # table for the highest-ranked covering candidate instead.
                    if selected_names and _window is not None and _retrieval.candidates:
                        selected_names, _swaps = reroute_uncovered(
                            selected_names, toc, _retrieval, _window,
                        )
                        if _swaps:
                            logger.info(
                                "NLAnswer: coverage reroute (window %s) %s",
                                _window,
                                "; ".join(f"{d} → {a}" for d, a in _swaps),
                            )

                    # ── Join-partner guarantee (2026-09-22): when the
                    # catalog knows a relation for a selected table but its
                    # partner was NOT selected, pull the best partner in
                    # BEFORE the DDL slice is built. Without it a question
                    # like "sales by customer" selects only the sales table
                    # and the model invents a join to a table it never saw —
                    # the dominant JOIN failure on freshly onboarded
                    # databases. Only ADDS catalog-vouched tables.
                    if (
                        selected_names
                        and getattr(
                            settings, "NL2SQL_JOIN_PARTNER_GUARANTEE_ENABLED", True
                        )
                    ):
                        try:
                            from app.services.db.join_partner import (
                                ensure_join_partners,
                            )

                            selected_names, _partners = await asyncio.to_thread(
                                ensure_join_partners,
                                self._db, kb_id, selected_names,
                            )
                            if _partners:
                                logger.info(
                                    "NLAnswer: join-partner guarantee added %s",
                                    _partners,
                                )
                        except Exception as e:
                            logger.debug(
                                "NLAnswer: join-partner guarantee skipped: %s", e,
                            )

                    # ── Coverage expansion (2026-09-22): pull NON-selected
                    # catalog tables sharing an entity token with the
                    # selection into it (bounded). Fixes recall failures
                    # like "contracts expiring" selecting only the purchase
                    # table while the sales-contract table exists — the
                    # 25-vs-215 measured failure.
                    if (
                        selected_names
                        and getattr(
                            settings, "NL2SQL_COVERAGE_EXPANSION_ENABLED", True
                        )
                    ):
                        try:
                            from app.services.db.coverage import (
                                expand_entity_selection,
                            )

                            selected_names, _cov_added = await asyncio.to_thread(
                                expand_entity_selection,
                                self._db, kb_id, selected_names,
                            )
                            if _cov_added:
                                logger.info(
                                    "NLAnswer: coverage expansion added %s",
                                    _cov_added,
                                )
                        except Exception as e:
                            logger.debug(
                                "NLAnswer: coverage expansion skipped: %s", e,
                            )

                    if selected_names:
                        logger.info(
                            "NLAnswer: TOC phase selected %d tables: %s",
                            len(selected_names), selected_names,
                        )
                        # Phase B: get full DDL for selected tables only
                        ddl_text = await asyncio.to_thread(
                            get_selected_tables_ddl,
                            self._db, [kb_id], selected_names,
                        )
                        if ddl_text and ddl_text.strip():
                            schema_info = _schema_info_from_toc_selection(
                                self._db, kb_id, selected_names, schema_svc,
                            )
                            schema_text = ddl_text
                            _selected_via_toc = True
                            logger.debug(
                                "NLAnswer: using TOC-selected DDL (%d tables)",
                                len(selected_names),
                            )
            except Exception as e:
                logger.warning(
                    "NLAnswer: TOC phase failed (falling back to schema linker): %s", e,
                )

        # ── Fallback: original RRF-based schema linker ──
        if not _selected_via_toc and settings.SCHEMA_LINKING_ENABLED and settings.SEMANTIC_CATALOG_ENABLED:
            try:
                from app.services.knowledge_graph.schema_linker import (
                    get_selected_tables_ddl,
                    link_schema,
                )
                linker_result = await link_schema(
                    question=question,
                    kb_ids=[kb_id],
                    db=self._db,
                    top_k=8,
                )
                if linker_result and linker_result.get("slice_text"):
                    schema_info = _schema_info_from_linker(
                        linker_result, schema_svc, kb_id
                    )
                    # ── SchemaGraph path (flag-gated): structural view + edges ──
                    candidate_tables = [
                        t.get("table_name")
                        for t in linker_result.get("tables", [])
                        if t.get("table_name")
                    ]

                    # ── Join-partner guarantee on the linker path too ──
                    if (
                        candidate_tables
                        and getattr(
                            settings, "NL2SQL_JOIN_PARTNER_GUARANTEE_ENABLED", True
                        )
                    ):
                        try:
                            from app.services.db.join_partner import (
                                ensure_join_partners,
                            )

                            candidate_tables, _partners = await asyncio.to_thread(
                                ensure_join_partners,
                                self._db, kb_id, candidate_tables,
                            )
                            if _partners:
                                # The linker's slice_text only covers the
                                # ORIGINAL tables — rebuild the DDL for the
                                # expanded set so the partner's columns are
                                # actually in the prompt.
                                _linker_slice = await asyncio.to_thread(
                                    get_selected_tables_ddl,
                                    self._db, [kb_id], candidate_tables,
                                )
                                if _linker_slice and _linker_slice.strip():
                                    linker_result["slice_text"] = _linker_slice
                                logger.info(
                                    "NLAnswer: join-partner guarantee (linker) "
                                    "added %s",
                                    _partners,
                                )
                        except Exception as e:
                            logger.debug(
                                "NLAnswer: join-partner guarantee skipped: %s", e,
                            )

                    # ── Coverage expansion on the linker path too ──
                    if (
                        candidate_tables
                        and getattr(
                            settings, "NL2SQL_COVERAGE_EXPANSION_ENABLED", True
                        )
                    ):
                        try:
                            from app.services.db.coverage import (
                                expand_entity_selection,
                            )

                            candidate_tables, _cov_added = await asyncio.to_thread(
                                expand_entity_selection,
                                self._db, kb_id, candidate_tables,
                            )
                            if _cov_added:
                                _cov_slice = await asyncio.to_thread(
                                    get_selected_tables_ddl,
                                    self._db, [kb_id], candidate_tables,
                                )
                                if _cov_slice and _cov_slice.strip():
                                    linker_result["slice_text"] = _cov_slice
                                logger.info(
                                    "NLAnswer: coverage expansion (linker) "
                                    "added %s",
                                    _cov_added,
                                )
                        except Exception as e:
                            logger.debug(
                                "NLAnswer: coverage expansion skipped: %s", e,
                            )
                    if settings.SCHEMA_GRAPH_ENABLED and candidate_tables:
                        try:
                            from app.services.db.schema_graph import SchemaGraph
                            graph = await asyncio.to_thread(
                                lambda: SchemaGraph(self._db, kb_id).build(
                                    candidate_tables
                                )
                            )
                            graph_text = graph.to_llm_context()
                            if graph_text.strip():
                                schema_text = graph_text
                                logger.debug(
                                    "NLAnswer: using schema graph (%d tables)",
                                    len(candidate_tables),
                                )
                            else:
                                schema_text = linker_result["slice_text"]
                        except Exception as e:
                            logger.warning(
                                "NLAnswer: schema graph failed (falling back to "
                                "catalog slice): %s", e,
                            )
                            schema_text = linker_result["slice_text"]
                    else:
                        schema_text = linker_result["slice_text"]
                    logger.debug(
                        "NLAnswer: using catalog slice (%d tables in context)",
                        len(linker_result.get("tables", [])),
                    )
            except Exception as e:
                logger.warning(
                    "NLAnswer: schema linker failed (falling back to M-Schema): %s", e,
                )
                schema_info = None
                schema_text = None

        if schema_info is None:
            try:
                schema_info, schema_text = await self._build_m_schema_fallback(
                    kb_id, question
                )
            except Exception as e:
                logger.warning("NLAnswer: M-Schema fallback failed: %s", e)
                return {
                    "success": False,
                    "error": f"Failed to read schema: {e}. "
                    f"{_connection_context(schema_svc, kb_id)}",
                    "source_id": kb_id,
                }

        # 1b. Apply the user's access policy to the schema context: strip
        # denied tables and restrict columns, then re-render the prompt text so
        # the LLM never sees forbidden data.
        if resolved_policy is not None and resolved_policy.has_policies:
            schema_info = _filter_schema_info(schema_info, resolved_policy, kb_id)
            schema_text = _format_schema_for_prompt(schema_info)

        # 1b-wl (P0-1): mined workload hints — validated join paths among the
        # selected tables + the filters analysts actually apply. Appended to
        # the schema text so the SQL model reuses proven conventions instead
        # of inventing joins. Empty for fresh warehouses (nothing mined).
        try:
            if getattr(settings, "NL2SQL_WORKLOAD_HINTS_ENABLED", True):
                from app.services.db.semantic_store import get_semantic_store

                _wl_store = get_semantic_store(
                    self._db, [kb_id], project_id=project_id,
                )
                _wl_tables = [
                    t.get("table_name")
                    for t in (schema_info or {}).get("tables") or []
                    if t.get("table_name")
                ]
                if _wl_tables:
                    _wl_text = _wl_store.workload_hints_for_tables(_wl_tables)
                    if _wl_text:
                        schema_text = (schema_text or "") + "\n\n" + _wl_text
                        logger.info(
                            "NLAnswer: workload hints injected (%d chars, "
                            "tables=%d)",
                            len(_wl_text), len(_wl_tables),
                        )
        except Exception as _wl_err:
            logger.debug("NLAnswer: workload hints skipped: %s", _wl_err)

        # 1c. GROUNDED PLAN (P3). One record of what this turn may answer with,
        # assembled from artifacts already computed above (catalog, retrieval,
        # policy, dialect layer, capability profile). This is NOT a second
        # pipeline: no extra LLM call, no extra data query.
        #
        # Its job is `unsupported_slots` — the parts of the question the selected
        # tables cannot support. They are named to the SQL model BEFORE it writes
        # SQL (so it cannot invent a column for them), carried into the result
        # set, and disclosed to the reader. The measured failure this replaces: a
        # five-part question answered with four figures and a 0.0, where the 0.0
        # was an unresolved join presented as data.
        _grounded_plan = None
        _grounding = None
        try:
            from app.services.db import grounding as _grounding
            from app.services.db.plan_memory import question_signature as _q_sig

            _denied: list[str] = []
            if (
                resolved_policy is not None
                and getattr(resolved_policy, "has_policies", False)
                and _catalog_names
            ):
                _denied = sorted(
                    _catalog_names - {t.get("table_name") for t in (schema_info or {}).get("tables") or []}
                )
            _grounded_plan = _grounding.build_grounded_plan(
                db=self._db,
                kb=schema_svc._load_kb(kb_id),
                question=question,
                window=_window,
                schema_info=schema_info,
                catalog=_toc_snapshot,
                retrieval=_retrieval,
                authorized_tables=_catalog_names or None,
                denied_tables=_denied,
                intent_signature=_q_sig(question),
            )
            logger.info(
                "NLAnswer: grounded plan for KB %s — tables=%d dialect=%s "
                "slots=%d unsupported=%s",
                kb_id, len(_grounded_plan.tables), _grounded_plan.dialect,
                len(_grounded_plan.evidence), _grounded_plan.unsupported_labels(),
            )
        except Exception as e:
            logger.debug("NLAnswer: grounded plan unavailable: %s", e, exc_info=True)

        # 2. Text → SQL
        # A verified plan for this same question on this same KB is injected as
        # guidance: the tables, columns and grouping that answered it last time
        # stay fixed, while any relative period is re-resolved against today.
        # This is what stops one business question from answering with a
        # different column (and a different number) on each run — see
        # services/db/plan_memory.py.
        _plan_guidance = ""
        if getattr(settings, "NL2SQL_PLAN_MEMORY_ENABLED", True):
            try:
                from app.services.db.plan_memory import (
                    format_plan_guidance,
                    load_plan,
                    mark_plan_used,
                )

                _plan = load_plan(self._db, kb_id, question)
                if _plan:
                    _plan_guidance = format_plan_guidance(_plan)
                    mark_plan_used(self._db, _plan)
                    logger.info(
                        "NLAnswer: reusing verified plan for KB %s (tables=%s, hits=%s)",
                        kb_id, _plan.get("tables"), _plan.get("hits"),
                    )
            except Exception as e:
                logger.debug("NLAnswer: plan memory lookup skipped: %s", e)
        _unsupported_guidance = ""
        _ratio_guidance = ""
        if _grounded_plan is not None and _grounding is not None:
            _unsupported_guidance = _grounding.unsupported_directive(_grounded_plan)
            # Ratio metrics (validated again on the result — see
            # db/ratio_contract.py): the requirement is stated BEFORE the model
            # writes the arithmetic, because a grain mismatch is an arithmetic
            # choice, not a data problem.
            _ratio_guidance = _grounding.ratio_directive(_grounded_plan)
        # OCI online stage — query log retrieval: SIMILAR past successful
        # questions (not just the exact match plan memory owns) become few-shot
        # shape grounding. Bounded, kb-scoped, LEARNED_FROM_WORKLOAD tier.
        _log_guidance = ""
        if getattr(settings, "NL2SQL_QUERY_LOG_RETRIEVAL_ENABLED", True):
            try:
                from app.services.db.query_log_retrieval import (
                    build_query_log_guidance,
                )

                _log_guidance = build_query_log_guidance(self._db, kb_id, question)
            except Exception as e:
                logger.debug("NLAnswer: query-log guidance skipped: %s", e)
        _sql_guidance = "\n\n".join(
            part
            for part in (
                # Intent resolution first: the model must know WHAT to answer
                # before it sees HOW similar questions were shaped.
                (_planning or {}).get("guidance"),
                _plan_guidance,
                _log_guidance,
                _unsupported_guidance,
                _ratio_guidance,
            )
            if part
        )
        try:
            sql = await self._text_to_sql(
                question, schema_info, schema_text=schema_text, project_id=project_id,
                endpoint=endpoint, extra_guidance=_sql_guidance or None,
            )
        except Exception as e:
            logger.warning("NLAnswer: text→SQL failed: %s", e)
            return {
                "success": False,
                "error": f"Could not generate SQL: {e}",
                "source_id": kb_id,
            }

        if not sql:
            return {
                "success": False,
                "error": "Model did not produce a SQL statement.",
                "source_id": kb_id,
            }

        # 2b. Validate generated SQL against the user's policy (defense-in-depth:
        # even if the LLM referenced a denied table, refuse to execute it).
        if resolved_policy is not None and resolved_policy.has_policies:
            vr = _validate_against_policy(sql, resolved_policy, kb_id)
            if vr is not None and not vr.is_valid:
                errors = vr.errors or ["query references forbidden tables"]
                logger.warning(
                    "NLAnswer: blocked generated SQL for KB %s: %s",
                    kb_id, "; ".join(str(e) for e in errors),
                )
                return {
                    "success": False,
                    "error": "Query blocked by data access policy: "
                    + "; ".join(str(e) for e in errors),
                    "sql": sql,
                    "source_id": kb_id,
                }

        # 2c. Structural schema validation (flag-gated): one self-correct retry
        # on hard errors (always) OR fan-out warnings (Fix 3,
        # NL2SQL_FANOUT_GUARD_ENABLED).
        sql, _corrected = await self._correct_sql_with_validator_feedback(
            sql,
            question,
            kb_id,
            schema_info,
            schema_text=schema_text,
            project_id=project_id,
            endpoint=endpoint,
        )

        # 2d. LLM-output hygiene: models sometimes emit Python-format
        # artifacts (%% in DATE_FORMAT) or paste multiple statements — both
        # produce MySQL 1064s that kill the data step. Deterministic cleanup
        # BEFORE execution (see _sanitize_llm_sql).
        sql = _sanitize_llm_sql(sql)

        # 2e. OCI stage 5 — deterministic, semantics-preserving rewrite
        # (predicate push-down, join elimination with uniqueness evidence).
        # The canonical path passes NO row budget: narration consumes the
        # rows, so bounding them here would silently change the answer shape.
        # Rewrites are additive telemetry on the result either way.
        _rewrite = _apply_sql_rewrite_stage(sql)
        if _rewrite is not None and _rewrite.applied:
            logger.info(
                "NLAnswer: sql rewrite applied %s for KB %s",
                _rewrite.applied, kb_id,
            )
            sql = _rewrite.sql

        # 3. Execute — with a bounded REPAIR loop on naming/syntax failures.
        #
        # A driver error is the only ground truth we get about the real schema,
        # and "Unknown column 'x'" is the most common way a data answer dies:
        # the model invents a plausible column, the database rejects it, and
        # the turn used to end by asking the user to name the column. Instead:
        # hand the model the error plus the real column list, regenerate, and
        # re-run — the runtime reaction the request deserved. Bounded by
        # NL2SQL_DB_ERROR_REPAIR_MAX; failures that are not naming problems
        # (permissions, timeouts, connections) skip the loop entirely.
        exec_result: dict | None = None
        _exec_error: Exception | None = None
        _repaired_from: str | None = None
        _repair_budget = max(0, int(getattr(settings, "NL2SQL_DB_ERROR_REPAIR_MAX", 2) or 0))
        for _attempt in range(_repair_budget + 1):
            try:
                # The grounded plan is the context the execution gate validates
                # against (its ratio contracts in particular).
                query_svc.plan = _grounded_plan
                exec_result = await asyncio.to_thread(query_svc.execute, kb_id, sql)
                _exec_error = None
                break
            except Exception as e:  # noqa: BLE001
                _exec_error = e
                if _attempt >= _repair_budget or not _is_resolvable_db_error(e):
                    break
                repaired, why_not = await self._repair_sql_after_db_error(
                    sql=sql,
                    error=e,
                    question=question,
                    kb_id=kb_id,
                    schema_info=schema_info,
                    schema_text=schema_text,
                    project_id=project_id,
                    endpoint=endpoint,
                )
                if not repaired or repaired == sql:
                    logger.warning(
                        "NLAnswer: could not repair SQL for KB %s after %s (%s)",
                        kb_id, e, why_not or "model returned the same query",
                    )
                    break
                logger.info(
                    "NLAnswer: repaired SQL after DB error %s (attempt %d/%d)",
                    e, _attempt + 1, _repair_budget,
                )
                if _repaired_from is None:
                    _repaired_from = str(e)
                sql = repaired

        if _exec_error is not None:
            logger.warning("NLAnswer: execute failed: %s", _exec_error)
            _shadow_validate_and_log(
                kb_id, question, sql, live_success=False, live_error=str(_exec_error)
            )
            failure = {
                "success": False,
                "error": f"Query failed: {_exec_error}",
                "sql": sql,
                "source_id": kb_id,
                "sql_repaired": bool(_repaired_from),
            }
            # A statement refused at the execution boundary is a STRUCTURED
            # failure, not a string: the checks that ran, the SQL as authored, the
            # (empty) executed statement, and the same payload on the plan and the
            # result set — so a caller reads what was refused and why.
            payload = getattr(_exec_error, "payload", None)
            if isinstance(payload, dict):
                from app.services.db import execution_gate

                failure.update(
                    {
                        "error_kind": "execution_validation_failed",
                        "validation": payload,
                        "authored_sql": payload.get("authored_sql") or sql,
                        "executed_sql": payload.get("executed_sql") or "",
                    }
                )
                failure["result_set"] = execution_gate.failure_result_set(
                    _grounded_plan, payload
                ).to_dict()
                if _grounded_plan is not None:
                    try:
                        _grounded_plan.execution_validation = payload
                        failure["plan"] = _grounded_plan.to_dict()
                    except Exception as e:  # pragma: no cover - plan is a dataclass
                        logger.debug("NLAnswer: could not stamp the plan: %s", e)
            return failure

        if _repaired_from:
            logger.info(
                "NLAnswer: served a REPAIRED query for KB %s (original failure: %s)",
                kb_id, _repaired_from,
            )
            if isinstance(exec_result, dict):
                exec_result["repaired_from_error"] = _repaired_from

        if exec_result is None:  # unreachable: the loop either sets it or records an error
            exec_result = {"rows": [], "sql": sql, "source_id": kb_id}

        _shadow_validate_and_log(kb_id, question, sql, live_success=True)

        # 3b. Zero-row / degenerate auto-correction: if the SQL returned 0
        # rows (or degenerate data — metadata-only probe rows / all-null
        # values, C3 2026-08-26), try once more with the full TOC approach
        # (or a re-selection if we already used TOC). The most common cause
        # is the schema linker selecting the wrong table; the TOC phase fixes
        # it. C2 adds a live COUNT(*) probe so the re-selection LLM can avoid
        # provably-empty tables.
        exec_rows = exec_result.get("rows", [])
        _degenerate = _is_degenerate_result(exec_rows, question=question)
        if (
            (not exec_rows or _degenerate)
            and settings.SCHEMA_LINKING_ENABLED
            and settings.SEMANTIC_CATALOG_ENABLED
        ):
            _why = "0 rows" if not exec_rows else "degenerate data (metadata-only or all-null)"
            logger.info(
                "NLAnswer: SQL returned %s — attempting table re-selection "
                "with full TOC for KB %s", _why, kb_id,
            )
            try:
                from app.services.knowledge_graph.schema_linker import (
                    build_full_toc,
                    format_toc_for_selection,
                    get_selected_tables_ddl,
                )
                # C2 (2026-08-26): probe table coverage so the re-selection
                # LLM can AVOID provably-empty tables.
                _probe_names = _extract_table_names(sql)
                if _static_route:
                    for _fb in (_static_route.get("fallback_tables") or []):
                        if _fb not in _probe_names:
                            _probe_names.append(_fb)
                _probe_note = ""
                if _probe_names:
                    try:
                        _probe_cov = await _probe_table_coverage(
                            self._db, kb_id, _probe_names[:5], window=_window,
                        )
                        if _probe_cov:
                            _probe_note = (
                                " Live coverage probe (bounded existence test, "
                                "not a COUNT — safe on views): "
                                + ", ".join(
                                    f"{t}: {c}" for t, c in _probe_cov.items()
                                )
                                + ". Use a table reported as empty or without "
                                "data in the window only if no alternative exists."
                            )
                    except Exception:
                        _probe_note = ""
                # Build error context for the re-selection prompt
                failed_context = (
                    f"Previous SQL attempt returned {_why}:\n"
                    f"```sql\n{sql}\n```\n"
                    f"Please choose DIFFERENT tables that are more likely to "
                    f"contain the data for: {question}"
                    f"{_probe_note}"
                )
                toc = build_full_toc(self._db, [kb_id])
                if toc:
                    # Same retrieval stage as the first pass. The correction
                    # round is where a ranked candidate set matters most: the
                    # failed SQL already names tables to avoid, so the model
                    # needs a bounded, evidence-carrying alternative surface
                    # rather than the alphabetical head of the catalog.
                    from app.services.db.table_retrieval import (
                        candidate_names as _cand_names,
                        format_candidates_block as _fmt_block,
                        retrieve_for_question as _retrieve,
                    )

                    _r2 = await asyncio.to_thread(
                        lambda: _retrieve(self._db, [kb_id], question, toc,
                                          window=_window)
                    )
                    toc_text = format_toc_for_selection(toc, _r2.names)
                    _b2 = _fmt_block(_r2)
                    if _b2:
                        toc_text = f"{_b2}\n\n{toc_text}"
                    selected_names = await self._select_tables_from_toc(
                        question, toc_text, len(toc),
                        error_context=failed_context,
                        endpoint=endpoint,
                        known_names={t["table_name"] for t in toc},
                    )
                    if not selected_names and _r2.candidates:
                        selected_names = [
                            n for n in _cand_names(_r2)
                            if n not in set(_extract_table_names(sql))
                        ] or _cand_names(_r2)
                        logger.info(
                            "NLAnswer: zero-row correction named no existing "
                            "table — using retrieval top-%d %s",
                            len(selected_names), selected_names,
                        )
                    if selected_names:
                        logger.info(
                            "NLAnswer: zero-row correction selected %d tables: %s",
                            len(selected_names), selected_names,
                        )
                        ddl_text = await asyncio.to_thread(
                            get_selected_tables_ddl,
                            self._db, [kb_id], selected_names,
                        )
                        if ddl_text and ddl_text.strip():
                            schema_info = _schema_info_from_toc_selection(
                                self._db, kb_id, selected_names, schema_svc,
                            )
                            schema_text = ddl_text
                            # Re-generate SQL with new tables
                            sql2 = await self._text_to_sql(
                                question, schema_info, schema_text=schema_text,
                                project_id=project_id, endpoint=endpoint,
                            )
                            if sql2:
                                sql2, _ = await self._correct_sql_with_validator_feedback(
                                    sql2, question, kb_id, schema_info,
                                    schema_text=schema_text, project_id=project_id,
                                    endpoint=endpoint,
                                )
                                _rw2 = _apply_sql_rewrite_stage(sql2 or "")
                                if _rw2 is not None and _rw2.applied:
                                    sql2 = _rw2.sql
                                if sql2:
                                    try:
                                        exec_result = await asyncio.to_thread(
                                            query_svc.execute, kb_id, sql2
                                        )
                                        exec_rows = exec_result.get("rows", [])
                                        # Accept the correction only when it
                                        # produced REAL data: an all-null /
                                        # all-zero result is the same empty
                                        # signal the correction was escaping.
                                        # Adopting it replaces a failed attempt
                                        # with a confidently wrong answer —
                                        # measured 2026-09-22: "total number of
                                        # inventory lots" corrected onto
                                        # COUNT(DISTINCT FMTONO) over a table
                                        # where FMTONO is NULL on all 52,647
                                        # rows, then narrated "No inventory
                                        # lots are currently recorded".
                                        if exec_rows and not _is_degenerate_result(exec_rows, question=question):
                                            logger.info(
                                                "NLAnswer: zero-row correction "
                                                "SUCCEEDED — got %d rows",
                                                len(exec_rows),
                                            )
                                            sql = sql2
                                        elif exec_rows:
                                            logger.warning(
                                                "NLAnswer: zero-row correction "
                                                "returned %d DEGENERATE rows "
                                                "(all-null/all-zero) — not "
                                                "adopting; surfacing the "
                                                "empty-result disclosure",
                                                len(exec_rows),
                                            )
                                        else:
                                            logger.info(
                                                "NLAnswer: zero-row correction "
                                                "still returned 0 rows"
                                            )
                                    except Exception as e2:
                                        logger.warning(
                                            "NLAnswer: zero-row correction "
                                            "execute failed: %s", e2,
                                        )
            except Exception as e:
                logger.warning(
                    "NLAnswer: zero-row correction failed (non-fatal): %s", e,
                )

        # The zero-row correction above may have replaced `exec_rows` with good
        # data; recompute degeneracy so the plan-memory / example-promotion
        # gates below reflect what is actually served, not the discarded first
        # attempt (which left `_degenerate` stale).
        _degenerate = _is_degenerate_result(exec_rows, question=question)

        # ── 3c. Answer contract (deterministic, no LLM) ──────────────────
        # Every guard above reacts to a FAILURE: 0 rows, degenerate rows, a
        # driver error. Nothing checked the case that actually erodes trust —
        # a query that succeeds and returns rows from the wrong period, or one
        # scalar where the question asked for a trend, narrated as if it
        # answered. See app/services/db/answer_contract.py.
        from app.services.db.answer_contract import (
            check_answer_contract,
            dimension_candidates,
        )

        _contract = check_answer_contract(
            question, sql, exec_rows, truncated=bool(exec_result.get("truncated")),
        )
        _dim_candidates = dimension_candidates(schema_info)
        if not _contract.ok:
            logger.info(
                "NLAnswer: answer contract violated %s — asking the model to fix "
                "the SQL for KB %s", _contract.violations, kb_id,
            )
            try:
                # The fix instruction NAMES the dimensions to group by, and the
                # verified plan (if any) is repeated so the retry cannot drift
                # back to a different column.
                _fix_guidance = f"[Constraint] {_contract.feedback(_dim_candidates)}"
                if _plan_guidance:
                    _fix_guidance += f"\n\n{_plan_guidance}"
                if _unsupported_guidance:
                    _fix_guidance += f"\n\n{_unsupported_guidance}"
                sql_c = await self._text_to_sql(
                    question, schema_info, schema_text=schema_text,
                    project_id=project_id, endpoint=endpoint,
                    extra_guidance=_fix_guidance,
                )
                _rw_c = _apply_sql_rewrite_stage(sql_c or "")
                if _rw_c is not None and _rw_c.applied:
                    sql_c = _rw_c.sql
                if sql_c and sql_c.strip() and sql_c.strip() != sql.strip():
                    res_c = await asyncio.to_thread(query_svc.execute, kb_id, sql_c)
                    rows_c = (res_c or {}).get("rows") or []
                    if rows_c:
                        _contract2 = check_answer_contract(
                            question, sql_c, rows_c,
                            truncated=bool((res_c or {}).get("truncated")),
                        )
                        # Only adopt the rewrite when it is strictly closer to
                        # the question — never trade a real answer for a
                        # differently-wrong one.
                        if len(_contract2.violations) < len(_contract.violations):
                            logger.info(
                                "NLAnswer: contract retry improved (%s → %s); using new SQL",
                                _contract.violations, _contract2.violations,
                            )
                            exec_result, exec_rows, sql, _contract = res_c, rows_c, sql_c, _contract2
                        else:
                            logger.info(
                                "NLAnswer: contract retry did not improve (%s); "
                                "keeping the original rows", _contract2.violations,
                            )
            except Exception as e:
                logger.warning(
                    "NLAnswer: answer-contract retry failed (non-fatal): %s", e,
                )

        # ── 3c-bis. CANONICAL RESULT SET (P3) ────────────────────────────
        # The plan is completed from the SQL that will actually run (joins,
        # filters, grain, ordering, top-N), then the executor's result is
        # wrapped into the shape narration consumes. Slots the query could not
        # support are re-checked here against the RESULT — a constant zero whose
        # value crosses a join the catalog cannot vouch for becomes a disclosed
        # unsupported slot instead of a reported 0.0.
        _crs = None
        _crv = _build_provenance(
            kb_id=kb_id,
            source_name=exec_result.get("source", {}).get("name"),
            sql=exec_result.get("sql"),
            executed_sql=exec_result.get("executed_sql"),
            tables=_extract_table_names(exec_result.get("sql") or ""),
            window=_window,
            catalog=_toc_snapshot,
            retrieval=None if _retrieval is None else _retrieval.as_dict(),
        )
        if _grounded_plan is not None and _grounding is not None:
            try:
                _grounding.enrich_plan_from_sql(_grounded_plan, sql, db=self._db)
                _crs = _grounding.build_canonical_result_set(
                    plan=_grounded_plan,
                    exec_result=exec_result,
                    sql=sql,
                    schema_info=schema_info,
                    catalog=_toc_snapshot,
                    contract=_contract,
                    provenance=_crv,
                )
                # What the boundary verified before the statement was sent belongs
                # on the record for a statement that RAN, not only for one that was
                # refused (see the failure branch above): otherwise a success is
                # less auditable than a failure.
                _exec_validation = (
                    exec_result.get("validation")
                    if isinstance(exec_result, dict)
                    else None
                )
                if isinstance(_exec_validation, dict) and _exec_validation:
                    if _grounded_plan.execution_validation is None:
                        _grounded_plan.execution_validation = dict(_exec_validation)
                    if _crs.execution_validation is None:
                        _crs.execution_validation = dict(_exec_validation)
                _crs_violations = _crs.validate()
                if _crs_violations:
                    logger.warning(
                        "NLAnswer: canonical result set inconsistent: %s", _crs_violations
                    )
                logger.info(
                    "NLAnswer: canonical result set — rows=%d complete=%s freshness=%s "
                    "coverage=%s unsupported=%s",
                    _crs.row_count, _crs.complete, _crs.freshness.get("state"),
                    {t: c.get("state") for t, c in _crs.available_coverage.items()},
                    _crs.unsupported_labels(),
                )
            except Exception as e:
                logger.warning("NLAnswer: canonical result set failed (non-fatal): %s", e)

        # ── 3c-ter. Coverage self-check (2026-09-22): the 25-vs-215 fix ──
        # Measured live: "How many contracts expire this month?" answered 25
        # (purchase contracts) while 190 sales contracts sat in a sibling
        # table the SQL never touched. When the catalog knows entity-token
        # siblings of the touched tables that the SQL did NOT touch, run
        # bounded supplementary queries and attach them to the payload as
        # ``supplementary_snapshots`` — the calling agent sees the complete
        # entity set instead of a confident partial one.
        _supplementary: list[dict] = []
        # Reaching this point means the execution succeeded (execution
        # errors returned above) — the flag is the only gate.
        if getattr(settings, "NL2SQL_COVERAGE_SELF_CHECK_ENABLED", True):
            logger.info(
                "NLAnswer: coverage self-check ENTER (flag=%s sql=%r)",
                getattr(settings, "NL2SQL_COVERAGE_SELF_CHECK_ENABLED", True),
                (sql or "")[:80],
            )
            try:
                from app.services.db.coverage import find_uncovered_siblings
                from app.services.knowledge_graph.schema_linker import (
                    get_selected_tables_ddl as _sib_ddl_fn,
                )

                _touched = _extract_table_names(sql) or []
                _siblings = await asyncio.to_thread(
                    find_uncovered_siblings, self._db, kb_id, _touched,
                )
                if _siblings:
                    logger.info(
                        "NLAnswer: coverage self-check — entity siblings the "
                        "SQL never touched: %s", _siblings,
                    )
                    for _sib in _siblings[:2]:
                        try:
                            _sib_ddl = await asyncio.to_thread(
                                _sib_ddl_fn, self._db, [kb_id], [_sib],
                            )
                            if not (_sib_ddl and _sib_ddl.strip()):
                                continue
                            _sib_info = _schema_info_from_toc_selection(
                                self._db, kb_id, [_sib], schema_svc,
                            )
                            _sib_sql = await self._text_to_sql(
                                f"{question} — query ONLY the table {_sib} "
                                f"for the same request",
                                _sib_info, schema_text=_sib_ddl,
                                project_id=project_id, endpoint=endpoint,
                            )
                            if not _sib_sql:
                                continue
                            _sib_sql, _ = await self._correct_sql_with_validator_feedback(
                                _sib_sql, question, kb_id, _sib_info,
                                schema_text=_sib_ddl, project_id=project_id,
                                endpoint=endpoint,
                            )
                            if not _sib_sql:
                                continue
                            _sib_res = await asyncio.to_thread(
                                query_svc.execute, kb_id, _sib_sql,
                            )
                            # NOTE: QueryService.execute has NO 'success'
                            # key — gating on it silently skipped every
                            # supplementary query (measured 2026-09-22).
                            # Success here = rows came back at all.
                            _sib_rows = (_sib_res or {}).get("rows") or []
                            if not _sib_rows:
                                logger.info(
                                    "NLAnswer: supplementary query for %s "
                                    "returned 0 rows — not attaching",
                                    _sib,
                                )
                                continue
                            _supplementary.append({
                                "table": _sib,
                                "sql": _sib_res.get("sql") or _sib_sql,
                                "row_count": len(_sib_rows),
                                "rows": _sib_rows[:50],
                                "note": (
                                    f"entity sibling of {_touched} — the "
                                    f"primary query did not include this table"
                                ),
                            })
                            logger.info(
                                "NLAnswer: supplementary snapshot for %s — "
                                "%d rows", _sib, len(_sib_rows),
                            )
                        except Exception as _sib_err:
                            logger.debug(
                                "NLAnswer: supplementary query for %s failed "
                                "(non-fatal): %s", _sib, _sib_err,
                            )
            except Exception as e:
                logger.debug("NLAnswer: coverage self-check skipped: %s", e)

        # The narrator and every downstream consumer read exec_result — the
        # snapshots must be ON it, not only on the final payload.
        if _supplementary:
            exec_result["supplementary_snapshots"] = _supplementary

        # ── 3d. Remember the plan that worked ────────────────────────────
        # Only a VERIFIED answer becomes a plan. Learning from an unchecked one
        # would make the platform consistently wrong instead of occasionally
        # wrong. What is stored is the tables + SQL shape, never the rows.
        if (
            getattr(settings, "NL2SQL_PLAN_MEMORY_ENABLED", True)
            and _contract.ok
            and exec_rows
            and not _degenerate
        ):
            try:
                import re as _re

                from app.services.db.plan_memory import save_plan

                _gm = _re.search(
                    r"\bgroup\s+by\s+(.+?)(?:\s+(?:having|order\s+by|limit)\b|$)",
                    sql or "", _re.I | _re.S,
                )
                _dims_used = [d.strip() for d in _gm.group(1).split(",")] if _gm else []
                _tables_used = _extract_table_names(sql) or []
                if save_plan(
                    self._db, kb_id, question,
                    tables=_tables_used, sql=sql, dimensions=_dims_used,
                ):
                    logger.info(
                        "NLAnswer: saved verified plan for KB %s (tables=%s, dims=%s)",
                        kb_id, _tables_used, _dims_used,
                    )
            except Exception as e:
                logger.debug("NLAnswer: plan memory save skipped: %s", e)

        # ── 3d-bis. Promote the VERIFIED Q→SQL pair into the few-shot corpus
        # (OCI feedback loop, P0-3). Same eligibility as plan memory —
        # execution OK, contract OK, non-empty rows, not degenerate — so the
        # corpus only ever learns answers that passed the deterministic
        # checks. Deduped by sql_hash, capped per datasource, flag-gated.
        if (
            getattr(settings, "NL2SQL_EXAMPLE_AUTO_PROMOTION_ENABLED", False)
            and _contract.ok
            and exec_rows
            and not _degenerate
        ):
            try:
                from app.services.q_sql_examples.service import QSqlExampleService

                _promoted = QSqlExampleService.maybe_auto_promote(
                    self._db,
                    question=question,
                    sql=sql,
                    datasource_id=kb_id,
                )
                if _promoted:
                    logger.info(
                        "NLAnswer: promoted verified Q→SQL example for KB %s",
                        kb_id,
                    )
            except Exception as e:
                logger.debug("NLAnswer: example auto-promotion skipped: %s", e)

        # 4. Narrate
        # PERF 2026-08-24: skip_narrate lets deliverable-turn callers bypass
        # this LLM call — the main agent's post-loop synthesize_report writes
        # the final CEO narrative from ALL merged datasets (more data than
        # narrate ever sees), so this call is pure duplication on those turns.
        if skip_narrate:
            logger.info("NLAnswer: skipping narrate LLM call (caller synthesizes)")
            answer_text = _fallback_narrative(question, exec_result, crs=_crs)
        else:
            try:
                answer_text = await self._narrate(
                    question, exec_result, endpoint=endpoint, crs=_crs
                )
            except Exception as e:
                logger.debug("NLAnswer: narrate failed (non-fatal): %s", e)
                # If narration fails, fall back to a tiny template so the
                # caller still gets a useful string.
                answer_text = _fallback_narrative(question, exec_result, crs=_crs)

        # 4b. The prose is checked against the result set it was given. Two
        # failure modes are caught here, both deterministic:
        #   * a figure stated for a slot the result does not support (an
        #     unsupported slot answered with 0 or with a number from nowhere);
        #   * a figure the rows never contained (the existing grounding rule —
        #     see app/services/db/answer_grounding.py, applied by the caller's
        #     guardrail for the agent's own final text).
        if _crs is not None and _crs.unsupported_slots:
            try:
                from app.services.db import grounding as _grounding2

                _claims = _grounding2.check_narrative_against_result(answer_text, _crs)
                if _claims:
                    logger.info(
                        "NLAnswer: narration claimed unsupported data %s — rewriting", _claims
                    )
                    try:
                        answer_text = await self._narrate(
                            question,
                            exec_result,
                            endpoint=endpoint,
                            crs=_crs,
                            correction=_grounding2.rewrite_instruction(_claims, _crs),
                        )
                    except Exception as e:
                        logger.debug("NLAnswer: corrective narrate failed: %s", e)
                    _claims2 = _grounding2.check_narrative_against_result(answer_text, _crs)
                    if _claims2:
                        # Still claiming it: state the limitation ourselves rather
                        # than publish a figure the data does not support.
                        logger.warning(
                            "NLAnswer: narration still claimed unsupported data "
                            "after the rewrite — appending the disclosure"
                        )
                        answer_text = f"{answer_text}\n\n{_crs.disclosure}"
            except Exception as e:
                logger.debug("NLAnswer: narrative check skipped: %s", e)

        _na_rows = exec_result.get("rows", [])
        if _na_rows and isinstance(_na_rows[0], dict):
            _na_cols = list(_na_rows[0].keys())
        else:
            _na_cols = []

        # ── 4c. Visualization hint (OCI stage 8, P2-7). Deterministic shape
        # inference — no LLM call. Advisory: renderers may ignore it.
        _visualization = None
        if (
            getattr(settings, "NL2SQL_CHART_SUGGESTION_ENABLED", False)
            and _na_rows
        ):
            try:
                from app.services.db.chart_suggestor import suggest_chart

                _visualization = suggest_chart(_na_rows, _na_cols)
            except Exception as e:
                logger.debug("NLAnswer: chart suggestion skipped: %s", e)

        # ── 4d. Persist the planner's structured turn state (OCI P1-6): the
        # next turn in this conversation starts from what was resolved here
        # instead of re-deriving entity/metric/time from scratch.
        if _planning is not None and conversation_id:
            try:
                from app.services.db import nl2sql_planner as _planner

                if not _planning.get("needs_clarification"):
                    _resolved = {
                        k: _planning.get(k)
                        for k in ("metric", "metric_definition", "entity", "intent")
                        if _planning.get(k)
                    }
                    _defaults = None
                    if _planning.get("time_range") or _planning.get("slices"):
                        _defaults = {
                            "time_range": _planning.get("time_range") or {},
                            "slices": _planning.get("slices") or [],
                        }
                    _planner.save_turn_state(
                        self._db,
                        conversation_id,
                        kb_id,
                        resolved=_resolved or None,
                        defaults=_defaults,
                    )
            except Exception as e:
                logger.debug("NLAnswer: turn-state save skipped: %s", e)

        return {
            "success": True,
            "answer": answer_text,
            "rows": _na_rows,
            "columns": _na_cols,
            "sql": exec_result.get("sql"),
            "source_id": kb_id,
            "source_name": exec_result.get("source", {}).get("name"),
            "citations": _extract_citations(schema_info, exec_result),
            "iterations": 1,
            # Present when the first query died and the model rebuilt it from
            # the driver's error — callers surface it as "recovered" evidence.
            "repaired_from_error": _repaired_from,
            # OCI stage 5 telemetry: which deterministic rewrites changed the
            # statement that actually ran (empty when the flag is off or the
            # SQL was already clean).
            **(
                {"rewrites_applied": list(_rewrite.applied)}
                if _rewrite is not None and _rewrite.applied
                else {}
            ),
            # Present only when the SQL that ran contradicted the question
            # (wrong period / single scalar for a trend request). Callers can
            # qualify the answer instead of presenting it as authoritative.
            **({"contract_violations": _contract.violations}
               if not _contract.ok else {}),
            # Provenance: which source, which SQL (fingerprinted), which tables
            # it actually touched, the period asked for and the freshest date
            # the catalog holds for those tables. Enough to audit an answer
            # after the fact without re-running it — and enough to tell a
            # "genuinely zero" answer apart from an "answered from the wrong
            # window" one.
            "provenance": _crv,
            # Coverage self-check (2026-09-22): snapshots from entity-sibling
            # tables the primary SQL never touched. Present only when the
            # self-check ran and produced them; callers (agent narration,
            # report synthesis) MUST fold these in before claiming a
            # complete entity-level answer.
            **(
                {"supplementary_snapshots": _supplementary}
                if _supplementary else {}
            ),
            # OCI stage 8 (P2-7): deterministic chart-type suggestion from the
            # result shape. Present only when NL2SQL_CHART_SUGGESTION_ENABLED
            # and the result is chartable; renderers may ignore it.
            **({"visualization": _visualization} if _visualization is not None else {}),
            # ── P3 contracts (added 2026-09-20) ───────────────────────────
            # `plan` is what the platform decided it could answer, including the
            # slots it could not support. `result_set` is the canonical shape
            # narration consumed. Both are additive: every key above is
            # unchanged, so existing callers keep working (see
            # _legacy_result_view for the adapter contract).
            **(
                {
                    "plan": _grounded_plan.to_dict(),
                    "result_set": _crs.to_dict(),
                    "unsupported_slots": list(_crs.unsupported_slots),
                }
                if (_grounded_plan is not None and _crs is not None)
                else {}
            ),
            **({"disclosure": _crs.disclosure} if (_crs is not None and _crs.disclosure) else {}),
        }

    # ------------------------------------------------------------------
    # LLM steps
    # ------------------------------------------------------------------

    async def _text_to_sql(
        self,
        question: str,
        schema_info: dict,
        schema_text: str | None = None,
        project_id: str | None = None,
        endpoint: LLMEndpoint | None = None,
        extra_guidance: str | None = None,
    ) -> str:
        """Ask the LLM to produce a single SQL statement.

        ``schema_text`` is the pre-rendered prompt context (e.g. the schema
        linker's curated slice); when None it is rendered from schema_info.

        ``project_id`` (optional) enables Business Semantic Layer injection:
        matched approved ``project_metric`` definitions + coverage annotations
        are appended to the prompt (progressive disclosure, token-capped).

        ``extra_guidance`` (optional) is turn-specific steering appended just
        before the question — the answer contract's fix instruction, and the
        verified plan for a repeat question (see services/db/plan_memory.py).
        """
        if schema_text is None:
            schema_text = _format_schema_for_prompt(schema_info)

        # Business context injection (Approach A). Only when a project_id is
        # known; flag-gated and token-capped inside build_business_context.
        business_context = ""
        if project_id and settings.KG_BUSINESS_CONTEXT_ENABLED:
            try:
                from app.services.knowledge_graph.business_context import (
                    build_business_context,
                )
                business_context = build_business_context(
                    self._db, project_id, schema_info["source"].get("id"), question
                )
            except Exception as e:
                logger.warning("NLAnswer: business context injection skipped: %s", e)

        user_content = (
            f"Database: {schema_info['source'].get('db_type')}\n"
            f"Schema:\n{schema_text}\n\n"
        )
        if business_context:
            user_content += f"{business_context}\n\n"
        if extra_guidance:
            user_content += f"{extra_guidance}\n\n"
        user_content += (
            f"Question: {question}\n\n"
            "Return only the SQL — no explanation, no markdown."
        )

        messages = [
            {"role": "system", "content": _SQL_GEN_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
        text = await _chat(messages, temperature=0.0, endpoint=endpoint, max_tokens=3072)
        return _strip_sql_fences(text)

    async def _select_tables_from_toc(
        self,
        question: str,
        toc_text: str,
        total_tables: int,
        error_context: str | None = None,
        endpoint: LLMEndpoint | None = None,
        known_names: set[str] | None = None,
    ) -> list[str]:
        """Phase A of two-phase NL2SQL: ask the LLM to pick relevant tables
        from the full table-of-contents.

        Returns a list of table names that EXIST, or [] when the model named
        nothing real. Every returned name is validated against ``known_names``
        (the catalog) when the caller supplies it.

        The empty return is load-bearing. This used to fall back to returning
        the model's raw list when none of its names matched the catalog
        ("LLM may use full qualified names"), which is exactly how
        ``['contract_execution', 'contract', 'contract_execution_detail']``
        — three names that do not exist — reached SQL generation on a live
        warehouse and produced "table 'contracts' doesn't exist". A selection
        that names no existing table is an EMPTY selection; the caller decides
        what to do about it.
        """
        system_msg = (
            "You are a database expert. The user will ask a data question. "
            "You will see a catalog of ALL tables in the database. "
            "Your job is to choose the 1-4 most relevant tables for answering "
            "the question. Return ONLY a JSON array of table names, nothing else.\n\n"
            "Rules:\n"
            "- Choose tables that actually contain the data the user is asking about.\n"
            "- If the user mentions 'sales', look for tables with names or descriptions "
            "related to sales, orders, transactions, or revenue.\n"
            "- If the user mentions specific metrics (volume, revenue, margin), "
            "choose fact/transaction tables that typically contain such data.\n"
            "- Prefer fact tables over dimension tables for metrics.\n"
            "- Include dimension tables if the user asks for breakdowns by "
            "product, region, partner, etc.\n"
            "- Do NOT choose more than 4 tables.\n"
            "- Return the table names EXACTLY as they appear in the catalog.\n\n"
            "Example response: [\"sales_table\", \"partner_table\"]"
        )
        user_msg = f"{toc_text}\n\nQuestion: {question}"
        if error_context:
            user_msg += f"\n\n{error_context}"

        messages = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ]
        try:
            text = await _chat(messages, temperature=0.0, endpoint=endpoint, max_tokens=2048)
            # Parse JSON array from LLM response
            text = text.strip()
            # Strip markdown fences if present
            if text.startswith("```"):
                lines = text.split("\n")
                lines = [l for l in lines if not l.startswith("```")]
                text = "\n".join(lines).strip()
            selected = json.loads(text)
            if isinstance(selected, list) and all(isinstance(s, str) for s in selected):
                if known_names is not None:
                    # Validate against the CATALOG, not against the rendered
                    # text: a compact/truncated render must never be the reason
                    # a real table is rejected, and a name absent from the
                    # catalog is not queryable however plausible it sounds.
                    validated = [s for s in selected if s in known_names]
                    if not validated:
                        logger.warning(
                            "NLAnswer: table selection named no existing table "
                            "(%s) — treating as empty selection",
                            selected[:4],
                        )
                    return validated[:4]
                # Validation: table names must exist in the TOC
                known_names = set()
                for line in toc_text.split("\n"):
                    line = line.strip()
                    if line and not line.startswith(("--", "#", "Matched", "##")):
                        # First word (before comma or parenthesis) is the table name
                        name = line.split(",")[0].split("(")[0].strip()
                        if name:
                            known_names.add(name)
                validated = [s for s in selected if s in known_names]
                if validated:
                    return validated[:4]
                # If no exact match, return the raw list (LLM may use full qualified names)
                return selected[:4]
            return []
        except (json.JSONDecodeError, Exception) as e:
            logger.warning("NLAnswer: TOC table selection failed: %s", e)
            return []

    async def _text_to_sql_with_correction(
        self,
        question: str,
        schema_info: dict,
        schema_text: str | None = None,
        project_id: str | None = None,
        errors: list[str] | None = None,
        available_columns: dict | None = None,
        available_suggestions: list[str] | None = None,
        endpoint: LLMEndpoint | None = None,
    ) -> str | None:
        """One self-correct retry: re-generate SQL with validator feedback.

        Appends the structural validator's error list + the real available
        columns to the schema context so the LLM can fix hallucinated
        table/column references in a single follow-up call.
        """
        correction = ["The previous SQL was rejected for these reasons:"]
        for e in errors or []:
            correction.append(f"- {e}")
        if available_columns:
            correction.append("Available columns (use only these):")
            for table, cols in available_columns.items():
                correction.append(f"- {table}: {', '.join(cols)}")
        if available_suggestions:
            correction.append("Validator suggestions:")
            for s in available_suggestions:
                correction.append(f"- {s}")
        note = "\n".join(correction)
        augmented = f"{schema_text}\n\n{note}" if schema_text else note
        try:
            return await self._text_to_sql(
                question, schema_info, schema_text=augmented, project_id=project_id,
                endpoint=endpoint,
            )
        except Exception as e:
            logger.debug("NLAnswer: correction retry failed: %s", e)
            return None

    async def _repair_sql_after_db_error(
        self,
        *,
        sql: str,
        error,
        question: str,
        kb_id: str,
        schema_info: dict,
        schema_text: str | None,
        project_id: str | None,
        endpoint: LLMEndpoint | None,
    ) -> tuple[str | None, str | None]:
        """Re-generate SQL after the DATABASE rejected it.

        This is the post-execution counterpart to
        ``_correct_sql_with_validator_feedback``: that one validates SQL
        *before* it runs, so it can only catch what the (flag-gated) validator
        happens to know. A driver error is ground truth — the database itself
        names the column or table that does not exist here.

        Returns ``(repaired_sql, reason_if_unrepaired)``. Never raises: a
        failed repair must leave the caller holding its original error.
        """
        driver_error = str(error or "").strip()
        identifier = _missing_identifier_from_error(driver_error)

        # Real columns for the tables this SQL touched. Reuse the schema we
        # already fetched; introspect the live KB for anything missing.
        columns = _columns_by_table(schema_info)
        for table in _referenced_tables(sql):
            match = next(
                (known for known in columns if known.lower() == table.lower()), None
            )
            if match:
                if match != table:
                    columns[table] = columns.pop(match)
                continue
            try:
                described = await asyncio.to_thread(
                    SchemaService(self._db).describe_table, kb_id, table,
                )
                cols: list[str] = []
                if isinstance(described, dict):
                    for c in (described.get("columns") or []):
                        if isinstance(c, dict) and c.get("name"):
                            cols.append(str(c["name"]))
                if cols:
                    columns[table] = cols
            except Exception as exc:  # noqa: BLE001 — context is best-effort
                logger.debug("NLAnswer: describe_table(%s) failed: %s", table, exc)

        errors = [f"The database rejected the previous query: {driver_error}"]
        if identifier:
            errors.append(
                f"`{identifier}` does not exist in this database — it is not a "
                f"column or table here, so never reference it again."
            )
        errors.append(
            "Rewrite the query using ONLY the tables and columns listed below, "
            "keeping the user's original intent and time window."
        )
        if not columns:
            errors.append(
                "No column list could be loaded for the referenced tables — "
                "re-derive them from the schema above."
            )

        corrected = await self._text_to_sql_with_correction(
            question,
            schema_info,
            schema_text=schema_text,
            project_id=project_id,
            errors=errors,
            available_columns=columns or None,
            endpoint=endpoint,
        )
        if not corrected:
            return None, "the model produced no replacement SQL"
        return _sanitize_llm_sql(corrected), None

    async def _correct_sql_with_validator_feedback(
        self,
        sql: str,
        question: str,
        kb_id: str,
        schema_info: dict,
        schema_text: str | None,
        project_id: str | None,
        endpoint: LLMEndpoint | None,
    ) -> tuple[str, bool]:
        """Structural schema validation with one self-correct retry (Fix 3).

        Triggers the correction on hard errors (always) OR fan-out warnings
        (only when ``NL2SQL_FANOUT_GUARD_ENABLED``). Returns
        ``(maybe-corrected_sql, corrected_bool)``; identical output is not
        applied.
        """
        if not settings.SCHEMA_GRAPH_SQL_VALIDATOR_ENABLED:
            return sql, False
        try:
            from app.services.nl2sql.schema_validator import validate_against_schema
            vres = validate_against_schema(sql, kb_id, self._db)
        except Exception as e:
            logger.debug("NLAnswer: schema validator error: %s", e)
            return sql, False
        problems = list(vres.get("errors", []))
        warnings = list(vres.get("warnings", []))
        should_correct = bool(problems) or (
            bool(warnings) and getattr(settings, "NL2SQL_FANOUT_GUARD_ENABLED", False)
        )
        if not should_correct:
            return sql, False
        logger.warning(
            "NLAnswer: generated SQL needs correction for KB %s "
            "(errors=%d warnings=%d): %s",
            kb_id, len(problems), len(warnings),
            "; ".join(str(e) for e in (problems or warnings)),
        )
        corrected = await self._text_to_sql_with_correction(
            question,
            schema_info,
            schema_text=schema_text,
            project_id=project_id,
            errors=problems or warnings,
            available_columns=vres.get("available_columns", {}),
            available_suggestions=vres.get("available_suggestions", []),
            endpoint=endpoint,
        )
        if corrected and corrected != sql:
            return corrected, True
        return sql, False

    async def _narrate(
        self,
        question: str,
        exec_result: dict,
        endpoint: LLMEndpoint | None = None,
        crs=None,
        correction: str | None = None,
    ) -> str:
        """Ask the LLM to narrate the result into a natural-language answer.

        When ``crs`` (a CanonicalResultSet) is supplied, narration consumes the
        canonical shape ONLY: its rows, its units, its coverage and freshness,
        its warnings, and an explicit statement of which parts of the question
        the data cannot support. The model is not given the chance to invent a
        column for an unsupported slot, and it is told never to write 0 for one.

        ``correction`` is set on a rewrite after the prose claimed a figure the
        result set does not support (see ``grounding.check_narrative_against_result``).
        """
        rows = (crs.rows if crs is not None else exec_result.get("rows", [])) or []
        sql = (exec_result.get("sql") if crs is None else exec_result.get("sql")) or ""
        # Keep the prompt small: cap rows sent to the LLM.
        preview = rows[:50]

        # Coverage self-check (2026-09-22): supplementary snapshots from
        # entity-sibling tables the primary SQL never touched MUST reach the
        # narrator — otherwise the answer reports a partial entity set as if
        # it were complete (the measured 25-vs-215 failure).
        _supp_block = ""
        _supp = exec_result.get("supplementary_snapshots") or []
        if _supp:
            lines = [
                "RELATED TABLES the query above did NOT include (same entity, "
                "other tables of this database). The rows below are VERIFIED "
                "query results — you MUST cite their figures in the answer "
                "alongside the primary rows, labeling each by table, so the "
                "answer covers the COMPLETE entity set:"
            ]
            for snap in _supp[:3]:
                lines.append(
                    f"- Table {snap.get('table')}: {snap.get('row_count')} rows "
                    f"(SQL: {(snap.get('sql') or '')[:160]})\n"
                    f"  Rows: {json.dumps((snap.get('rows') or [])[:10], default=str, ensure_ascii=False)}"
                )
            _supp_block = "\n".join(lines) + "\n\n"

        if crs is None:
            body = (
                f"Question: {question}\n"
                f"SQL: {sql}\n"
                f"Rows ({len(rows)} total, showing up to 50):\n"
                f"{json.dumps(preview, default=str, ensure_ascii=False)}\n\n"
                "Answer in the user's language. Cite column names where helpful."
            )
            body = f"{_supp_block}{body}" if _supp_block else body
        else:
            from app.services.db import grounding as _grounding

            scope = crs.requested_coverage or {}
            coverage = "; ".join(
                f"{table}: {cov.get('min') or '?'}..{cov.get('max') or '?'} ({cov.get('state')})"
                for table, cov in list(crs.available_coverage.items())[:6]
            )
            body = (
                "Question: " + question + "\n"
                "SQL: " + sql + "\n"
                "Columns: " + json.dumps(crs.columns, ensure_ascii=False) + "\n"
                + (f"Units: {json.dumps(crs.units, ensure_ascii=False)}\n" if crs.units else "")
                + (
                    "Period asked about: " + json.dumps(scope, ensure_ascii=False) + "\n"
                    if scope else ""
                )
                + (f"Data actually available: {coverage}\n" if coverage else "")
                + f"Freshness: {crs.freshness.get('state')}"
                + (f" (as of {crs.freshness.get('as_of')})" if crs.freshness.get("as_of") else "")
                + "\n"
                + f"Rows ({crs.row_count} total, showing up to 50):\n"
                + json.dumps(preview, default=str, ensure_ascii=False) + "\n\n"
                + _supp_block
                + _grounding.narrate_directive(
                    crs, supplementary=exec_result.get("supplementary_snapshots")
                )
                + "\n\nAnswer in the user's language. Cite column names where helpful."
            )
        if correction:
            body = f"{correction}\n\n{body}"
        messages = [
            {"role": "system", "content": _NARRATE_SYSTEM_PROMPT},
            {"role": "user", "content": body},
        ]
        return await _chat(messages, temperature=0.2, endpoint=endpoint, max_tokens=4096)

    # ------------------------------------------------------------------
    # M-Schema fallback (replaces describe_all)
    # ------------------------------------------------------------------

    async def _build_m_schema_fallback(
        self, kb_id: str, question: str
    ) -> tuple[dict, str]:
        """Build schema_info + schema_text via M-Schema (cached).

        When the schema linker is unavailable (e.g. catalog still indexing),
        this replaces the old describe_all(30) fallback with the richer
        M-Schema format that includes value sampling for TEXT-like columns.

        Returns:
            (schema_info, schema_text) — schema_info is the describe_all-
            shaped dict used by downstream code; schema_text is the M-Schema
            rendered string with value examples.
        """
        from app.services.datasources import build_adapter
        from app.services.datasources.m_schema import render_m_schema
        from app.services.db.schema_service import _cache_get, _cache_put

        # Check cache first
        cache_key = (kb_id, "m_schema")
        cached = _cache_get(cache_key)
        if cached is not None:
            logger.debug("NLAnswer: M-Schema cache hit for %s", kb_id)
            return cached["schema_info"], cached["schema_text"]

        # Load KB and build adapter
        schema_svc = SchemaService(self._db)
        kb = schema_svc._load_kb(kb_id)

        def _build() -> tuple[dict, str]:
            """Build the M-Schema on ONE worker thread, start to finish.

            The adapter is a driver connection, and some drivers are
            thread-affine (sqlite3 refuses to be closed from the thread that did
            not open it). The previous shape opened it in one ``to_thread`` call,
            used it in another and closed it on the event-loop thread — which for
            a SQLite source raised ``ProgrammingError`` out of the ``finally`` and
            surfaced as "Failed to read schema", refusing a source that was
            perfectly readable.
            """
            adapter = build_adapter(kb)
            try:
                schema_text = render_m_schema(adapter, sample_rows=3)
                # Same adapter, same thread: describe_all-shaped dict for db_type
                # and citation extraction.
                schema_info = _schema_info_from_adapter(kb, adapter)
                return schema_info, schema_text
            finally:
                adapter.close()

        schema_info, schema_text = await asyncio.to_thread(_build)
        _cache_put(cache_key, {
            "schema_info": schema_info,
            "schema_text": schema_text,
        })
        logger.info(
            "NLAnswer: M-Schema built for %s (%d tables)",
            kb_id, len(schema_info.get("tables", [])),
        )
        return schema_info, schema_text


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------

def _schema_info_from_linker(
    linker_result: dict, schema_svc: SchemaService, kb_id: str
) -> dict:
    """Adapt a schema-linker result to the describe_all-shaped dict.

    Downstream helpers (db_type lookup in _text_to_sql, _extract_citations)
    expect the describe_all shape, while the linker returns enriched table
    dicts. The pre-rendered DDL slice is passed separately as schema_text.
    """
    kb = schema_svc._load_kb(kb_id)
    tables = []
    for t in linker_result.get("tables", []):
        tables.append({
            "table": t.get("table_name"),
            "columns": [
                {
                    "name": c.get("name"),
                    "type": c.get("data_type") or "",
                    "nullable": c.get("is_nullable"),
                    "pk": bool(c.get("is_primary_key")),
                    "default": None,
                }
                for c in t.get("columns", [])
            ],
        })
    return {
        "source": {"id": kb.id, "name": kb.name, "db_type": kb.db_type},
        "tables": tables,
    }


# ── C2/C3 helpers (2026-08-26): table-coverage probe + degenerate check ──
_SAFE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _missing_identifier_from_error(error: str) -> str | None:
    """Extract the unknown column/table name from a driver error, if any.

    Covers the dialects the KB adapters can be bound to. The identifier is the
    whole point of the repair: it names what the model invented, so the next
    attempt can be told what actually exists.
    """
    if not error:
        return None
    for pattern in _MISSING_IDENTIFIER_PATTERNS:
        m = pattern.search(error)
        if m and m.groups():
            return (m.group(1) or "").strip() or None
    return None


def _is_resolvable_db_error(error) -> bool:
    """True when a failed query failed over a NAME the model can fix.

    Only naming/syntax failures qualify — the model can repair those once it
    sees the real schema. Permission denials, timeouts, deadlocks and
    connection errors are NOT naming problems: retrying them just burns
    budget, so they keep their existing failure path.
    """
    text = str(error or "")
    if not text:
        return False
    for pattern in _MISSING_IDENTIFIER_PATTERNS:
        if pattern.search(text):
            return True
    return bool(_SQL_SYNTAX_ERROR_RE.search(text))


def _columns_by_table(schema_info: dict | None) -> dict[str, list[str]]:
    """Real column names per table from a describe_all-shaped schema dict."""
    out: dict[str, list[str]] = {}
    for entry in (schema_info or {}).get("tables", []) or []:
        table = entry.get("table")
        if not table:
            continue
        cols: list[str] = []
        for c in (entry.get("columns") or []):
            if isinstance(c, dict) and c.get("name"):
                cols.append(str(c["name"]))
        if cols:
            out[table] = cols
    return out


def _referenced_tables(sql: str) -> list[str]:
    """Table names the failing SQL mentions (best effort, never raises)."""
    try:
        names = _extract_table_names(sql)
    except Exception:  # noqa: BLE001 — repair context is best-effort
        return []
    out: list[str] = []
    for n in names or []:
        base = str(n).strip().strip("`\"")
        if "." in base:
            base = base.split(".")[-1].strip("`\"")
        if base and base not in out:
            out.append(base)
    return out


def _extract_table_names(sql: str) -> list[str]:
    """Best-effort table names from a SQL string (FROM/JOIN clauses)."""
    out: list[str] = []
    if not sql:
        return out
    for m in re.finditer(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_.]*)", sql, re.IGNORECASE):
        name = m.group(1).split(".")[-1].strip("`\"[]")
        if _SAFE_IDENT_RE.match(name) and name not in out:
            out.append(name)
    return out


def _build_provenance(
    *,
    kb_id: str,
    source_name: str | None,
    sql: str | None,
    executed_sql: str | None = None,
    tables: list[str],
    window: tuple | None,
    catalog: list | None,
    retrieval: dict | None,
) -> dict:
    """Audit trail for one answer: source, SQL fingerprint, tables, coverage.

    Deliberately records the CATALOG window of the tables the SQL actually
    touched next to the window the question asked for. That pairing is what
    lets a reader tell "the warehouse genuinely has no rows for this period"
    apart from "we answered out of a table whose data stops a year earlier" —
    the distinction the live failure turned on, and which neither the SQL nor
    the prose carried.
    """
    import hashlib

    normalised = " ".join((sql or "").split())
    fingerprint = (
        hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:16]
        if normalised
        else ""
    )
    # The statement the DATABASE ran is not always the authored text: MySQL
    # prepends `/*+ MAX_EXECUTION_TIME */`, Oracle wraps the query in ROWNUM.
    # A record that kept only the fingerprint of the authored SQL could not show
    # what executed, so `COUNT(*)` read as a question with no answer next to a
    # disclosure about the same question. Falls back to the authored SQL when the
    # executor did not report a rewrite.
    executed_text = " ".join((executed_sql or sql or "").split())
    executed_fingerprint = (
        hashlib.sha256(executed_text.encode("utf-8")).hexdigest()[:16]
        if executed_text
        else ""
    )
    cov: dict[str, dict] = {}
    for t in tables:
        entry = next((c for c in (catalog or []) if c.get("table_name") == t), None)
        if entry is not None:
            cov[t] = {
                "min": entry.get("min_date") or "",
                "max": entry.get("max_date") or "",
                "date_column": entry.get("date_column") or "",
            }
    return {
        "source_id": kb_id,
        "source_name": source_name,
        "sql_fingerprint": fingerprint,
        #: the executable statement, verbatim and unmangled, so an auditor can
        #: see the count/question form that actually ran
        "executed_sql": executed_text,
        "executed_sql_fingerprint": executed_fingerprint,
        "tables": list(tables),
        "requested_window": [str(window[0]), str(window[1])] if window else None,
        "table_coverage": cov,
        "retrieval_method": (retrieval or {}).get("method"),
        "candidates_considered": (retrieval or {}).get("candidate_count"),
    }


async def _probe_table_coverage(
    db: Session,
    kb_id: str,
    table_names: list[str],
    window: tuple | None = None,
    timeout_s: int = 8,
) -> dict:
    """Cheap, dialect-correct coverage probe for the re-selection step.

    Two defects this replaces, both measured on the live stack:

    1. **It was MySQL-only.** The old probe built ``SELECT COUNT(*) AS cnt FROM
       `t```  — backticks hardcoded. Probed against every database KB in the
       stack it returned {} for all five PostgreSQL sources (the server reports
       a syntax error at the identifier) and worked only for MySQL/MariaDB, so
       the "+1 zero-row correction" that depends on this evidence was dead on
       Postgres, MSSQL, Oracle and SQLite. Identifiers now go through
       ``quote_ident(name, dialect)``, the same helper every connector uses.
    2. **It asked the most expensive question available.** ``COUNT(*)`` on a
       view measured 17.7 s (``erp_v_contract``) against a 3 s budget, and
       ``erp_v_contract_execution`` needed 150 s and lost the connection —
       i.e. the probe reliably timed out exactly where the correction was most
       needed. Coverage is now narrowed to a bounded existence test:
       ``SELECT 1 … WHERE <date> >= <start> AND <date> < <end>`` capped at one
       row in the SOURCE'S pagination syntax when the catalog knows a date
       column, else the same probe without a window. The row cap matters:
       on the same view measured 0.42 s.

    Returns ``{table: verdict}`` with verdicts the model can act on directly:
    ``"empty"`` / ``"has rows"`` / ``"no data in window"`` / ``"data in window"``.
    """
    out: dict[str, str] = {}
    if not table_names:
        return out

    from app.models.knowledge_base import KnowledgeBase
    from app.models.knowledge_catalog import KBTableMeta
    from app.services.db.capability_probe import profile_for_kb
    from app.services.db.query_service import QueryService

    kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).first()
    # The source's OWN capability profile decides the dialect. This used to be
    # ``(kb.db_type or "mysql")`` — a silent default that made an unidentifiable
    # source get MySQL syntax. No profile ⇒ skip the probe rather than author SQL
    # for a dialect we have not identified.
    profile = profile_for_kb(db, kb) if kb is not None else None
    if profile is None:
        logger.debug(
            "NLAnswer: table-coverage probe skipped — no dialect profile for source %s",
            kb_id,
        )
        return out

    # Date column per table, from the catalog — no extra introspection query
    # against the user's warehouse.
    date_cols: dict[str, str] = {}
    try:
        for row in (
            db.query(KBTableMeta.table_name, KBTableMeta.coverage_json)
            .filter(KBTableMeta.kb_id == kb_id)
            .all()
        ):
            cov = row[1] or {}
            if cov.get("date_column"):
                date_cols[row[0]] = cov["date_column"]
    except Exception as e:
        logger.debug("NLAnswer: coverage date columns unavailable: %s", e)

    qsvc = QueryService(db)
    for name in table_names:
        if not _SAFE_IDENT_RE.match(name):
            continue
        try:
            quoted = profile.quote_ident(name)
        except ValueError:
            continue
        where = ""
        if window:
            dcol = date_cols.get(name)
            if dcol and _SAFE_IDENT_RE.match(dcol):
                try:
                    qd = profile.quote_ident(dcol)
                except ValueError:
                    qd = None
                if qd:
                    start, end = window
                    where = f" WHERE {qd} >= '{start}' AND {qd} < '{end}'"
        # Dialect pagination: LIMIT is a syntax error on SQL Server and Oracle.
        stmt = profile.probe_sql(name, where, 1)
        try:
            res = await asyncio.wait_for(
                asyncio.to_thread(qsvc.execute, kb_id, stmt),
                timeout=timeout_s,
            )
        except Exception as e:
            logger.debug("NLAnswer: coverage probe failed (non-fatal) %s: %s", name, e)
            continue
        # QueryService.execute has NO "success" key (see the note on the
        # sibling supplementary-query path above) — gating on it skipped
        # every probe. A raised error is already handled by the except
        # above, so a returned dict means the probe actually ran.
        rows = (res or {}).get("rows") or []
        if rows:
            out[name] = "data in window" if where else "has rows"
        else:
            out[name] = "no data in window" if where else "empty"
    return out


def _is_degenerate_result(rows: list, question: str | None = None) -> bool:
    """True when the result is degenerate business data (not a real answer).

    Conservative on purpose: a legit 1-row COUNT(*) summary (e.g. total
    orders = 11779) is NOT degenerate. Flags:
      - metadata-only probe rows (MIN_DATE/MAX_DATE/ENTRY_COUNT),
      - rows where every value is null/empty/zero (all-null measures).
    """
    if not isinstance(rows, list) or not rows:
        return False
    try:
        from app.services.goal_contract import is_metadata_only_rows
        if is_metadata_only_rows(rows, question=question):
            return True
    except Exception:
        pass
    # all-null / all-zero check across every column
    for row in rows:
        if not isinstance(row, dict):
            return False
        vals = [v for k, v in row.items() if k and v is not None and v != "" and v != 0]
        if vals:
            return False
    return True


def _schema_info_from_toc_selection(
    db: Session, kb_id: str, selected_table_names: list[str],
    schema_svc: SchemaService,
) -> dict:
    """Build a describe_all-shaped dict from TOC-selected table names.
    Similar to _schema_info_from_linker but queries the DB for full
    column metadata for the selected tables only.
    """
    from app.models.knowledge_catalog import KBTableMeta, KBColumnMeta
    kb = schema_svc._load_kb(kb_id)
    tables = []
    for name in selected_table_names:
        meta = (
            db.query(KBTableMeta)
            .filter(
                KBTableMeta.kb_id == kb_id,
                KBTableMeta.table_name == name,
            )
            .first()
        )
        if not meta:
            continue
        cols = (
            db.query(KBColumnMeta)
            .filter(KBColumnMeta.table_meta_id == meta.id)
            .order_by(KBColumnMeta.ordinal)
            .all()
        )
        tables.append({
            "table": meta.table_name,
            "columns": [
                {
                    "name": c.column_name,
                    "type": c.data_type or "",
                    "nullable": c.is_nullable,
                    "pk": bool(c.is_primary_key),
                    "default": None,
                }
                for c in cols
            ],
        })
    return {
        "source": {"id": kb.id, "name": kb.name, "db_type": kb.db_type},
        "tables": tables,
    }


def _schema_info_from_adapter(kb: Any, adapter: Any) -> dict:
    """Build a describe_all-shaped dict from an adapter's refresh_schema().

    This produces the same structure as SchemaService.describe_all() so
    that downstream code (db_type lookup, _extract_citations) works
    unchanged.
    """
    schema = adapter.refresh_schema()
    tables = []
    for table_name, columns in schema.items():
        cols = []
        for c in columns:
            cols.append({
                "name": c.name,
                "type": c.dtype,
                "nullable": c.nullable,
                "pk": c.is_pk,
                "default": c.default,
            })
        tables.append({"table": table_name, "columns": cols})
    return {
        "source": {
            "id": str(kb.id),
            "name": kb.name,
            "db_type": kb.db_type,
        },
        "tables": tables,
    }


_SQL_GEN_SYSTEM_PROMPT = (
    "You are a SQL generator. Given a database schema and a natural-language "
    "question, output exactly one SQL statement that answers the question. "
    "Use only tables and columns present in the schema. Do not invent columns. "
    "Prefer explicit JOINs. Use the SQL dialect implied by the db_type. "
    "Output the raw SQL only — no markdown, no explanation, no leading prose.\n"
    "Cardinality rules (fan-out guard):\n"
    "- Never SUM a measure across a row-multiplying join (a one-to-many or "
    "many-to-many join inflates the measure before aggregation).\n"
    "- When a fact table joins a snapshot/variant-bearing or dimension table, "
    "pre-aggregate each side in a CTE or subquery first, then join the "
    "aggregated results.\n"
    "- If a SUM is needed across joined tables, compute it over a "
    "pre-aggregated subquery, never over the raw join product."
)

_NARRATE_SYSTEM_PROMPT = (
    "You are an executive analyst writing for a CEO. Given the user's question, "
    "the SQL that was run, and the resulting rows, produce a business-focused "
    "answer in exactly this structure:\n"
    "1. HEADLINE — one actionable business insight (what changed / what matters).\n"
    "2. KEY FIGURES — 2 to 4 supporting numbers, each with its unit and the "
    "metric it represents.\n"
    "3. CAVEAT — exactly one line about data coverage, freshness, or confidence.\n"
    "4. NEXT STEP — one recommended follow-up action.\n"
    "Hard rules:\n"
    "- Never use meta-language such as \"snapshot\", \"payload\", \"as of the data\", "
    "or \"the following\".\n"
    "- Never present a raw record count (e.g. \"1,387 rows\") as the answer or as "
    "a headline; counts are only supporting detail, never the insight itself.\n"
    "- Every figure must be traceable to the SQL evidence; do not invent numbers.\n"
    "- If the data is stale (max date older than the requested window), say so "
    "explicitly in the CAVEAT.\n"
    "- If the rows are empty, state plainly that no data was found and why.\n"
    "- Match the user's language."
)


def _format_schema_for_prompt(schema_info: dict) -> str:
    """Render a compact DDL-ish description of every table."""
    parts: list[str] = []
    for entry in schema_info.get("tables", []):
        t = entry.get("table")
        if "error" in entry:
            parts.append(f"-- {t}: (introspection error: {entry['error']})")
            continue
        cols = entry.get("columns", [])
        col_lines = []
        for c in cols:
            nullable = "" if c.get("nullable") else " NOT NULL"
            default = f" DEFAULT {c['default']}" if c.get("default") is not None else ""
            pk = " [PK]" if c.get("pk") else ""
            col_lines.append(f"  {c['name']} {c['type']}{nullable}{default}{pk}")
        parts.append(f"TABLE {t} (\n" + ",\n".join(col_lines) + "\n)")
    return "\n\n".join(parts) or "(no tables found)"


def _filter_schema_info(schema_info: dict, resolved_policy, kb_id: str) -> dict:
    """Strip denied tables and restrict columns from a describe_all-shaped dict.

    Returns a shallow copy with ``tables`` filtered so the LLM never sees
    forbidden tables or columns.
    """
    blocked = {t.lower() for t in resolved_policy.blocked_tables_for_kb(kb_id)}
    allowed = resolved_policy.allowed_tables_for_kb(kb_id)
    allowed_set = {t.lower() for t in allowed} if allowed is not None else None

    filtered_tables: list[dict] = []
    for entry in schema_info.get("tables", []):
        tname = (entry.get("table") or "")
        tname_l = tname.lower()
        if tname_l in blocked:
            continue
        if allowed_set is not None and tname_l not in allowed_set:
            continue

        columns = entry.get("columns", [])
        allowlist = resolved_policy.allowed_columns_for(kb_id, tname)
        if allowlist is not None:
            allow_lower = {c.lower() for c in allowlist}
            columns = [
                c for c in columns if (c.get("name") or "").lower() in allow_lower
            ]
        filtered_tables.append({**entry, "columns": columns})

    return {**schema_info, "tables": filtered_tables}


def _validate_against_policy(sql: str, resolved_policy, kb_id: str):
    """Validate *sql* against a resolved policy using the nl2sql validator."""
    from app.services.nl2sql.validator import validate

    blocked = resolved_policy.blocked_tables_for_kb(kb_id)
    allowed = resolved_policy.allowed_tables_for_kb(kb_id)
    return validate(sql, allowed_tables=allowed, block_tables=blocked)


def _shadow_validate_and_log(
    kb_id: str,
    question: str,
    sql: str,
    *,
    live_success: bool,
    live_error: str = "",
) -> None:
    """NL2SQL shadow mode: validate the generated SQL with the governed
    nl2sql pipeline and log divergence to ``nl2sql_query_logs``.

    NEVER alters the served result — this is observe-only. Runs after the
    live path produced its outcome so divergence can be classified:
      agree_pass            — shadow valid, live succeeded
      shadow_pass_live_fail — shadow valid, live failed
      shadow_fail_live_pass — shadow rejected, live succeeded (over-rejection risk)
      agree_fail            — both failed
    """
    # Observability only (P5-A): this switch decides whether the divergence is
    # LOGGED. Enforcement does not live here — it lives at the execution boundary
    # (services/db/execution_gate.py), which runs on every execution regardless of
    # this flag, the caller, or the context.
    if not getattr(settings, "NL2SQL_SHADOW_MODE_ENABLED", False):
        return
    try:
        from app.services.nl2sql import validate_sql

        res = validate_sql(sql)
    except Exception as e:
        logger.debug("nl2sql shadow validation failed (non-fatal): %s", e)
        return

    try:
        import uuid as _uuid

        from app.database import SessionLocal
        from app.models.nl2sql_query_log import Nl2sqlQueryLog

        shadow_ok = bool(res.get("is_valid")) and bool(res.get("policy_allowed", True))
        if shadow_ok and live_success:
            cls = "agree_pass"
        elif shadow_ok:
            cls = "shadow_pass_live_fail"
        elif live_success:
            cls = "shadow_fail_live_pass"
        else:
            cls = "agree_fail"

        session = SessionLocal()
        try:
            session.add(
                Nl2sqlQueryLog(
                    id=str(_uuid.uuid4()),
                    datasource_id=kb_id,
                    question=question,
                    generated_sql=sql or None,
                    sql_hash=res.get("sql_hash") or None,
                    validation_errors=(
                        {"errors": res.get("errors")} if res.get("errors") else None
                    ),
                    policy_decision=(
                        "allowed" if res.get("policy_allowed", True) else "denied"
                    ),
                    outcome="shadow",
                    explanation=(
                        f"shadow_class={cls}; live_success={live_success}; "
                        f"live_error={live_error[:200]}"
                    ),
                )
            )
            session.commit()
        finally:
            session.close()
    except Exception as e:
        logger.debug("nl2sql shadow log failed (non-fatal): %s", e)


#: Keys added by the P3 grounding contracts. The live result dict is additive:
#: every pre-P3 key keeps its old meaning, so a caller that tolerates extra keys
#: needs no change at all.
P3_RESULT_KEYS = ("plan", "result_set", "unsupported_slots", "disclosure", "source_readiness")


def legacy_result_view(result: dict) -> dict:
    """The pre-P3 result shape, for a caller that declares its response strictly.

    This is an ADAPTER, not a second pipeline: same answer, same rows, same SQL —
    only the P3 keys removed, for a consumer whose contract would otherwise
    break on them (a Pydantic response model, a stored snapshot, a webhook).

    It must never be used to hide a disclosure from a user-facing path: the
    caller that strips ``unsupported_slots`` is also dropping the sentence that
    says a slot could not be answered. ``db_tools`` therefore keeps them (see
    ``_attach_grounding_envelope``).
    """
    if not isinstance(result, dict):
        return result
    return {k: v for k, v in result.items() if k not in P3_RESULT_KEYS}


def _strip_sql_fences(text: str) -> str:
    """Strip ```sql ... ``` fences if the model included them anyway."""
    t = text.strip()
    if t.startswith("```"):
        # Remove first fence
        first_nl = t.find("\n")
        t = t[first_nl + 1 :] if first_nl != -1 else ""
        if t.endswith("```"):
            t = t[:-3]
    return t.strip().rstrip(";").strip()


def _extract_citations(schema_info: dict, exec_result: dict) -> list[str]:
    """Best-effort: list the tables referenced by the executed SQL."""
    sql = (exec_result.get("sql") or "").lower()
    cited: list[str] = []
    for entry in schema_info.get("tables", []):
        t = entry.get("table")
        if t and t.lower() in sql:
            for c in entry.get("columns", []):
                cited.append(f"{t}.{c['name']}")
    return cited[:50]  # cap


def _fallback_narrative(question: str, exec_result: dict, crs=None) -> str:
    """Deterministic template used when narration is skipped or fails.

    When a CanonicalResultSet is available its disclosure is appended, so the
    no-LLM path cannot drop an unsupported slot either.
    """
    rows = exec_result.get("rows", [])
    disclosure = ""
    if crs is not None:
        rows = list(crs.rows) or rows
        disclosure = crs.disclosure or ""
    if not rows:
        # An EMPTY result that could not support part of the question must say
        # so — "no rows" alone reads like a zero.
        if disclosure:
            return f"The query returned no rows. {disclosure}"
        return "The query returned no rows."
    # 2026-08-26: business-aware deterministic report FIRST — for known
    # intents (contract performance) build the real business report from
    # the rows. This text is what the main agent forwards to the docx /
    # sandbox generator, so the deliverable gets a real report instead of
    # "Found 307 row(s). First: ...".
    try:
        from app.services.db.business_reports import try_build_business_report

        src = exec_result.get("source_name") or "the data source"
        biz_report = try_build_business_report(question, rows, src)
        if biz_report:
            return biz_report
    except Exception:
        pass
    cols = list(rows[0].keys())
    head = ", ".join(f"{c}={rows[0][c]!r}" for c in cols[:5])
    more = f" (+{len(rows) - 1} more rows)" if len(rows) > 1 else ""
    tail = f"\n\n{disclosure}" if disclosure else ""
    return f"Found {len(rows)} row(s). First: {head}{more}.{tail}"


async def _chat(
    messages: list[dict],
    temperature: float = 0.0,
    endpoint: LLMEndpoint | None = None,
    max_tokens: int | None = None,
) -> str:
    """Single-turn chat call to the configured LLM.

    When ``endpoint`` is provided (project binding), targets the endpoint's
    base_url / api_key / model_id. Otherwise falls back to the legacy global
    provider (get_model / llm_url / llm_headers).
    """
    if endpoint is not None:
        _model = endpoint.model_id
        _url = endpoint.base_url.rstrip("/") + "/chat/completions"
        _api_key = endpoint.api_key or ""
        _headers = {
            "Authorization": f"Bearer {_api_key}" if _api_key else "",
            "Content-Type": "application/json",
        }
    else:
        _model = get_model()
        _url = llm_url()
        _headers = llm_headers()

    payload = {
        "model": _model,
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(_url, headers=_headers, json=payload)
        resp.raise_for_status()
    data = resp.json()
    return (data["choices"][0]["message"].get("content") or "").strip()
