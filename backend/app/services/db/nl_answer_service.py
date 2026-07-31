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

import json
import logging
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.services.db.query_service import QueryService
from app.services.db.schema_service import SchemaService
from app.services.llm_service import llm_headers, llm_url, get_model

logger = logging.getLogger(__name__)


class NLAnswerService:
    """Orchestrate text→SQL→execute→narrate for a KnowledgeBase."""

    def __init__(self, db: Session):
        self._db = db

    async def answer(self, kb_id: str, question: str) -> dict:
        """Return a structured + prose answer for a user question.

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

        schema_svc = SchemaService(self._db)
        query_svc = QueryService(self._db)

        # 1. Pull schema
        try:
            schema_info = schema_svc.describe_all(kb_id, max_tables=30)
        except Exception as e:
            logger.warning("NLAnswer: schema introspection failed: %s", e)
            return {
                "success": False,
                "error": f"Failed to read schema: {e}",
                "source_id": kb_id,
            }

        # 2. Text → SQL
        try:
            sql = await self._text_to_sql(question, schema_info)
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

        # 3. Execute
        try:
            exec_result = query_svc.execute(kb_id, sql)
        except Exception as e:
            logger.warning("NLAnswer: execute failed: %s", e)
            return {
                "success": False,
                "error": f"Query failed: {e}",
                "sql": sql,
                "source_id": kb_id,
            }

        # 4. Narrate
        try:
            answer_text = await self._narrate(question, exec_result)
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

    async def _text_to_sql(self, question: str, schema_info: dict) -> str:
        """Ask the LLM to produce a single SQL statement."""
        schema_text = _format_schema_for_prompt(schema_info)
        messages = [
            {"role": "system", "content": _SQL_GEN_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Database: {schema_info['source'].get('db_type')}\n"
                    f"Schema:\n{schema_text}\n\n"
                    f"Question: {question}\n\n"
                    "Return only the SQL — no explanation, no markdown."
                ),
            },
        ]
        text = await _chat(messages, temperature=0.0)
        return _strip_sql_fences(text)

    async def _narrate(self, question: str, exec_result: dict) -> str:
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
        return await _chat(messages, temperature=0.2)


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------

_SQL_GEN_SYSTEM_PROMPT = (
    "You are a SQL generator. Given a database schema and a natural-language "
    "question, output exactly one SQL statement that answers the question. "
    "Use only tables and columns present in the schema. Do not invent columns. "
    "Prefer explicit JOINs. Use the SQL dialect implied by the db_type. "
    "Output the raw SQL only — no markdown, no explanation, no leading prose."
)

_NARRATE_SYSTEM_PROMPT = (
    "You are a data analyst. Given a user's question, the SQL that was run, "
    "and the resulting rows, write a concise natural-language answer. "
    "Mention totals, top values, and column names where they add clarity. "
    "If the rows are empty, say so explicitly. Match the user's language."
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


async def _chat(messages: list[dict], temperature: float = 0.0) -> str:
    """Single-turn chat call to the configured LLM."""
    payload = {
        "model": get_model(),
        "messages": messages,
        "temperature": temperature,
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(llm_url(), headers=llm_headers(), json=payload)
        resp.raise_for_status()
    data = resp.json()
    return (data["choices"][0]["message"].get("content") or "").strip()
