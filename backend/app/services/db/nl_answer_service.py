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

        # 1. Pull schema — try semantic catalog first, fall back to M-Schema.
        # `schema_info` keeps the describe_all-shaped dict (used for db_type
        # and citations); `schema_text` is the pre-rendered prompt context
        # (the linker slice when available, else M-Schema with value sampling).
        schema_info: dict | None = None
        schema_text: str | None = None
        if settings.SCHEMA_LINKING_ENABLED and settings.SEMANTIC_CATALOG_ENABLED:
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

        # 4. Narrate
        try:
            answer_text = await self._narrate(question, exec_result, endpoint=endpoint)
        except Exception as e:
            logger.debug("NLAnswer: narrate failed (non-fatal): %s", e)
            # If narration fails, fall back to a tiny template so the
            # caller still gets a useful string.
            answer_text = _fallback_narrative(question, exec_result)

        return {
            "success": True,
            "answer": answer_text,
            "rows": exec_result.get("rows", []),
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
        text = await _chat(messages, temperature=0.0, endpoint=endpoint)
        return _strip_sql_fences(text)

    async def _text_to_sql_with_correction(
        self,
        question: str,
        schema_info: dict,
        schema_text: str | None = None,
        project_id: str | None = None,
        errors: list[str] | None = None,
        available_columns: dict | None = None,
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
        return await _chat(messages, temperature=0.2, endpoint=endpoint)

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
    cols = list(rows[0].keys())
    head = ", ".join(f"{c}={rows[0][c]!r}" for c in cols[:5])
    more = f" (+{len(rows) - 1} more rows)" if len(rows) > 1 else ""
    return f"Found {len(rows)} row(s). First: {head}{more}."


async def _chat(
    messages: list[dict],
    temperature: float = 0.0,
    endpoint: LLMEndpoint | None = None,
) -> str:
    """Single-turn chat call to the configured LLM.

    When ``endpoint`` is provided (project binding), targets the endpoint's
    base_url / api_key / model_id. Otherwise falls back to the legacy global
    provider (get_model / llm_url / llm_headers).
    """
    if endpoint is not None:
        _model = endpoint.model_id
        _url = endpoint.base_url.rstrip("/") + "/chat/completions"
        _headers = {"Authorization": f"Bearer {endpoint.api_key}", "Content-Type": "application/json"}
    else:
        _model = get_model()
        _url = llm_url()
        _headers = llm_headers()

    payload = {
        "model": _model,
        "messages": messages,
        "temperature": temperature,
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(_url, headers=_headers, json=payload)
        resp.raise_for_status()
    data = resp.json()
    return (data["choices"][0]["message"].get("content") or "").strip()
