"""LLM-driven smart retry for empty query results.

When ``ask_data_agent`` returns 0 rows for a file-format request
(PPT / DOCX / dashboard), this module asks the LLM to inspect the failed
query + the source context and propose a revised, broader question, then
re-runs the data agent with it. Strict budget: max ``max_attempts``
retries (default 2) so a pathological schema can never loop forever.

Designed to be called from the v3 SSE stream in ``agents.py``; all I/O is
injected via ``call_llm_fn`` / ``execute_ask_data_fn`` so it stays fully
unit-testable without touching the network or the DB.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

_RETRY_SYSTEM_PROMPT = """\
You are a query-revision assistant. A business-data agent ran a query for \
the user and it returned ZERO rows. Your job is to propose a REVISED question \
that is more likely to return data.

The most common causes of zero-row results are:
1. WRONG TABLE: The agent selected the wrong table (e.g. chose a "sentiment" \
table instead of a "sales" table). Try explicitly mentioning the correct table \
type in your revised question (e.g. "Show me sales/order/transaction data for...").
2. TOO NARROW: The date range or filters are too restrictive. Widen the date \
range, drop optional filters (customer/product/region/category).
3. WRONG COLUMN NAMES: The SQL may have used column names that don't exist \
in the actual table. Keep the intent clear but be less prescriptive about \
specific column names.

HERMES RE-PLAN RULES (applies when a warehouse catalog is provided below):
- You are given the FULL list of tables in the warehouse with row counts and \
coverage dates. Pick a DIFFERENT table than the one the failed SQL used — \
the previous table either had no matching rows or was stale.
- Prefer tables with MORE rows and MORE recent coverage dates. A table with \
14275 rows updated last week is far better than one with 0 rows updated in \
2023.
- If the failed SQL used a CJK table name, prefer another table from the \
catalog over an English-named view — business data may live in CJK-named \
tables in this warehouse.

Rules:
- Output ONLY the revised question. No preamble, no markdown fences, no quotes.
- Keep the user's core intent (which metric / which entity) unchanged.
- If the original question mentioned a specific domain (sales, inventory, \
finance), EXPLICITLY mention that domain type in the revised question so the \
agent picks the right table (e.g. "sales order data", "inventory data").
- Relax the most restrictive parts: widen the date range, drop optional \
filters, loosen exact-match conditions.
- Prefer a concrete, SQL-able question with an explicit, wider time window.
- If the request is already maximally broad AND you've tried a different \
table on a prior attempt, reply exactly: NO_RETRY
"""


def _extract_question(text: str) -> Optional[str]:
    """Pull the revised question out of an LLM reply.

    Tolerates markdown fences, JSON wrapping, surrounding prose and the
    ``NO_RETRY`` sentinel.
    """
    if not text:
        return None
    t = str(text).strip()
    # Markdown fences.
    t = re.sub(r"```(?:json|text|sql)?\s*", "", t).strip()
    t = t.strip("`").strip()
    if not t or t.upper() == "NO_RETRY":
        return None
    # JSON-wrapped replies (checked after fence stripping).
    if t.startswith("{") or t.startswith("["):
        try:
            parsed = json.loads(t)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            t = str(
                parsed.get("question")
                or parsed.get("revised_question")
                or parsed.get("query")
                or ""
            )
        elif isinstance(parsed, list):
            picked = None
            for item in parsed:
                if isinstance(item, dict) and item.get("question"):
                    picked = str(item["question"])
                    break
            t = picked or ""
        else:
            t = ""
        t = t.strip()
        if not t:
            return None
    # Keep just the first non-empty line if the model rambled.
    first_line = t.splitlines()[0].strip() if t.splitlines() else ""
    return first_line if first_line else None


def _describe_failure(failed_result: dict) -> str:
    """Build a compact failure summary for the retry prompt."""
    lines = [
        "CONTEXT FROM THE FAILED RUN:",
        f"- user request: {failed_result.get('question') or '(n/a)'}",
        (
            f"- source: {failed_result.get('source_name') or failed_result.get('source') or '(n/a)'}"
        ),
        f"- SQL that returned 0 rows: {failed_result.get('sql') or '(n/a)'}",
    ]
    cols = failed_result.get("columns") or failed_result.get("column_names")
    if cols:
        lines.append(f"- available columns: {', '.join(str(c) for c in cols)}")
    err = failed_result.get("error")
    if err:
        lines.append(f"- error/notes: {err}")
    return "\n".join(lines)


async def llm_driven_retry_ask_data(
    *,
    question: str,
    failed_result: dict,
    call_llm_fn: Callable[[str, list], Awaitable[str]],
    execute_ask_data_fn: Callable[[str], Awaitable[dict]],
    max_attempts: int = 2,
    get_catalog_fn: Optional[Callable[[], Awaitable[str]]] = None,
) -> Optional[dict]:
    """Attempt to find data for ``question`` after an empty first result.

    Returns the first non-empty ``ask_data_agent`` result dict, or
    ``None`` if every attempt came back empty (or the LLM declined /
    errored). Never raises: failures are logged and swallowed so the
    caller's stream can never crash on a retry.

    Hermes-style re-plan: when ``get_catalog_fn`` is supplied, the full
    warehouse table-of-contents is fetched fresh before EACH retry attempt
    and injected into the LLM's revision prompt. This lets the LLM pick a
    DIFFERENT table than the one that produced zero rows — the most common
    cause of empty results. Without the catalog, the LLM only sees the
    failed query's columns and is more likely to propose the same wrong
    table again.
    """
    attempts = 0
    while attempts < max_attempts:
        attempts += 1
        context = _describe_failure(failed_result)

        # ── Hermes Step 1: re-read the catalog before each retry ──
        # The catalog lists ALL tables with row counts + coverage dates.
        # Injecting it lets the LLM pick a different table than the one
        # that just returned 0 rows. Failures here are non-fatal — the
        # retry proceeds with just the failure context (legacy behavior).
        _catalog_block = ""
        if get_catalog_fn is not None:
            try:
                _catalog_text = await get_catalog_fn()
                if _catalog_text and _catalog_text.strip():
                    _catalog_block = (
                        "\n\nWAREHOUSE TABLE CATALOG (re-read fresh for this "
                        "attempt — pick a DIFFERENT table than the failed "
                        "SQL above):\n"
                        + _catalog_text.strip()
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "smart-retry catalog fetch failed (attempt %d, non-fatal): %s",
                    attempts, exc,
                )

        system_prompt = _RETRY_SYSTEM_PROMPT + "\n\n" + context + _catalog_block
        try:
            reply = await call_llm_fn(
                system_prompt, [{"role": "user", "content": question}]
            )
        except Exception as exc:  # noqa: BLE001 — retry must never crash the stream
            logger.warning("smart-retry LLM call failed (attempt %d): %s", attempts, exc)
            return None
        revised = _extract_question(reply)
        if not revised:
            logger.info("smart-retry: LLM declined to broaden (attempt %d)", attempts)
            return None
        try:
            new_result = await execute_ask_data_fn(revised)
        except Exception as exc:  # noqa: BLE001
            logger.warning("smart-retry execute failed (attempt %d): %s", attempts, exc)
            return None
        if not isinstance(new_result, dict):
            return None
        rows = new_result.get("rows") or []
        # 2026-08-26: only treat as success if the rows are REAL business
        # data, not metadata-only probes. A 1-row `MIN/MAX/COUNT` summary
        # is technically "non-empty" but useless to the calling agent.
        try:
            from app.services.goal_contract import is_metadata_only_rows as _is_meta
            _is_metadata_only = bool(rows) and _is_meta(rows)
        except Exception:
            _is_metadata_only = False
        if rows and not _is_metadata_only:
            new_result["retried"] = True
            new_result["retried_question"] = revised
            new_result["retry_attempts"] = attempts
            new_result["hermes_catalog_used"] = bool(_catalog_block)
            logger.info(
                "smart-retry SUCCESS (attempt %d): found %d rows; sql=%s; "
                "catalog_used=%s; revised_q=%s",
                attempts,
                len(rows),
                str(new_result.get("sql") or "")[:200],
                bool(_catalog_block),
                revised[:120],
            )
            return new_result
        if _is_metadata_only:
            logger.info(
                "smart-retry attempt %d returned %d metadata-only row(s) — "
                "continuing retry to get real business data; sql=%s",
                attempts, len(rows), str(new_result.get("sql") or "")[:200],
            )
            # Treat as a failed attempt so the loop continues
            failed_result = new_result
            last_sql = new_result.get("sql") or last_sql
            last_error = "metadata-only rows returned (not real business data)"
            continue
        # Still empty — feed the failure back for the next round.
        failed_result = new_result
        logger.info(
            "smart-retry attempt %d still empty; sql=%s; catalog_used=%s",
            attempts,
            str(new_result.get("sql") or "")[:200],
            bool(_catalog_block),
        )
    return None


__all__ = ["llm_driven_retry_ask_data", "_extract_question"]
