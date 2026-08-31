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

    async def answer(
        self,
        kb_id: str,
        question: str,
        resolved_policy=None,
        project_id: str | None = None,
        endpoint: LLMEndpoint | None = None,
        skip_narrate: bool = False,
        agent_name: str | None = None,
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
                    format_toc_text,
                    get_selected_tables_ddl,
                )
                toc = build_full_toc(self._db, [kb_id])
                if toc:
                    toc_text = format_toc_text(toc, token_budget=1200)
                    # Ask the LLM which tables are relevant
                    selected_names = await self._select_tables_from_toc(
                        question, toc_text, len(toc), endpoint=endpoint,
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
                from app.services.knowledge_graph.schema_linker import link_schema
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

        # 2. Text → SQL
        try:
            sql = await self._text_to_sql(
                question, schema_info, schema_text=schema_text, project_id=project_id,
                endpoint=endpoint,
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

        # 3. Execute
        try:
            exec_result = await asyncio.to_thread(query_svc.execute, kb_id, sql)
        except Exception as e:
            logger.warning("NLAnswer: execute failed: %s", e)
            _shadow_validate_and_log(
                kb_id, question, sql, live_success=False, live_error=str(e)
            )
            return {
                "success": False,
                "error": f"Query failed: {e}",
                "sql": sql,
                "source_id": kb_id,
            }

        _shadow_validate_and_log(kb_id, question, sql, live_success=True)

        # 3b. Zero-row / degenerate auto-correction: if the SQL returned 0
        # rows (or degenerate data — metadata-only probe rows / all-null
        # values, C3 2026-08-26), try once more with the full TOC approach
        # (or a re-selection if we already used TOC). The most common cause
        # is the schema linker selecting the wrong table; the TOC phase fixes
        # it. C2 adds a live COUNT(*) probe so the re-selection LLM can avoid
        # provably-empty tables.
        exec_rows = exec_result.get("rows", [])
        _degenerate = _is_degenerate_result(exec_rows)
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
                    format_toc_text,
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
                        _probe_counts = await _probe_table_counts(
                            self._db, kb_id, _probe_names[:5],
                        )
                        if _probe_counts:
                            _probe_note = (
                                " Table coverage probe (live COUNT(*)): "
                                + ", ".join(
                                    f"{t}={c}" for t, c in _probe_counts.items()
                                )
                                + ". Prefer tables with non-zero counts."
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
                    toc_text = format_toc_text(toc, token_budget=1200)
                    selected_names = await self._select_tables_from_toc(
                        question, toc_text, len(toc),
                        error_context=failed_context,
                        endpoint=endpoint,
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
                                if sql2:
                                    try:
                                        exec_result = await asyncio.to_thread(
                                            query_svc.execute, kb_id, sql2
                                        )
                                        exec_rows = exec_result.get("rows", [])
                                        if exec_rows:
                                            logger.info(
                                                "NLAnswer: zero-row correction "
                                                "SUCCEEDED — got %d rows",
                                                len(exec_rows),
                                            )
                                            sql = sql2
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

        # 4. Narrate
        # PERF 2026-08-24: skip_narrate lets deliverable-turn callers bypass
        # this LLM call — the main agent's post-loop synthesize_report writes
        # the final CEO narrative from ALL merged datasets (more data than
        # narrate ever sees), so this call is pure duplication on those turns.
        if skip_narrate:
            logger.info("NLAnswer: skipping narrate LLM call (caller synthesizes)")
            answer_text = _fallback_narrative(question, exec_result)
        else:
            try:
                answer_text = await self._narrate(question, exec_result, endpoint=endpoint)
            except Exception as e:
                logger.debug("NLAnswer: narrate failed (non-fatal): %s", e)
                # If narration fails, fall back to a tiny template so the
                # caller still gets a useful string.
                answer_text = _fallback_narrative(question, exec_result)

        _na_rows = exec_result.get("rows", [])
        if _na_rows and isinstance(_na_rows[0], dict):
            _na_cols = list(_na_rows[0].keys())
        else:
            _na_cols = []

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
    ) -> str:
        """Ask the LLM to produce a single SQL statement.

        ``schema_text`` is the pre-rendered prompt context (e.g. the schema
        linker's curated slice); when None it is rendered from schema_info.

        ``project_id`` (optional) enables Business Semantic Layer injection:
        matched approved ``project_metric`` definitions + coverage annotations
        are appended to the prompt (progressive disclosure, token-capped).
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
    ) -> list[str]:
        """Phase A of two-phase NL2SQL: ask the LLM to pick relevant tables
        from the full table-of-contents.

        Returns a list of table names, or [] on failure.
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
                # Validate: table names must exist in the TOC
                known_names = set()
                for line in toc_text.split("\n"):
                    line = line.strip()
                    if line and not line.startswith("--"):
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
        self, question: str, exec_result: dict, endpoint: LLMEndpoint | None = None
    ) -> str:
        """Ask the LLM to narrate the rows into a natural-language answer."""
        rows = exec_result.get("rows", [])
        sql = exec_result.get("sql", "")
        # Keep the prompt small: cap rows sent to the LLM.
        preview = rows[:50]
        messages = [
            {"role": "system", "content": _NARRATE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Question: {question}\n"
                    f"SQL: {sql}\n"
                    f"Rows ({len(rows)} total, showing up to 50):\n"
                    f"{json.dumps(preview, default=str, ensure_ascii=False)}\n\n"
                    "Answer in the user's language. Cite column names where helpful."
                ),
            },
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

        adapter = build_adapter(kb)
        try:
            # Build M-Schema text (with value sampling)
            schema_text = await asyncio.to_thread(
                render_m_schema, adapter, sample_rows=3
            )

            # Build schema_info from adapter's refresh_schema (for db_type + citations)
            schema_info = await asyncio.to_thread(
                _schema_info_from_adapter, kb, adapter
            )

            # Cache both
            _cache_put(cache_key, {
                "schema_info": schema_info,
                "schema_text": schema_text,
            })
            logger.info(
                "NLAnswer: M-Schema built for %s (%d tables)",
                kb_id, len(schema_info.get("tables", [])),
            )
            return schema_info, schema_text
        finally:
            adapter.close()


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


async def _probe_table_counts(db: Session, kb_id: str, table_names: list[str], timeout_s: int = 3) -> dict:
    """Run ``SELECT COUNT(*)`` per table (safe identifiers only).

    Returns ``{table: count_str}``; empty dict on any failure. Used by the
    zero-row / degenerate correction so the re-selection LLM can AVOID
    tables that are provably empty.
    """
    counts: dict[str, str] = {}
    from app.services.db.query_service import QueryService

    qsvc = QueryService(db)
    for name in table_names:
        if not _SAFE_IDENT_RE.match(name):
            continue
        try:
            res = await asyncio.wait_for(
                asyncio.to_thread(
                    qsvc.execute, kb_id, f"SELECT COUNT(*) AS cnt FROM `{name}`"
                ),
                timeout=timeout_s,
            )
            rows = (res or {}).get("rows") or []
            if rows and isinstance(rows[0], dict):
                counts[name] = str(rows[0].get("cnt", "?"))
        except Exception as e:
            logger.debug("NLAnswer: probe COUNT(%s) failed (non-fatal): %s", name, e)
    return counts


def _is_degenerate_result(rows: list) -> bool:
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
        if is_metadata_only_rows(rows):
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


def _fallback_narrative(question: str, exec_result: dict) -> str:
    rows = exec_result.get("rows", [])
    if not rows:
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
    return f"Found {len(rows)} row(s). First: {head}{more}."


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
