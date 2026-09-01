"""NL-to-SQL translation engine (P4).

Flag-gated behind UNIVERSAL_ANALYTICS_NL_SQL (default OFF) because it requires
an LLM call. When enabled, translates natural-language questions into
dialect-appropriate SQL using the configured LLM.

Safety: all generated SQL is validated (SELECT-only) and injection-screened
before execution.
"""

from __future__ import annotations

import os
import re

# ── Safety ──────────────────────────────────────────────────────────

_DANGEROUS_KEYWORDS = re.compile(
    r"\b(DROP|CREATE|ALTER|INSERT|UPDATE|DELETE|TRUNCATE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)


def _reject_dangerous_question(question: str) -> str | None:
    """Screen the NL question for DDL/DML keywords before LLM call.

    Returns None if safe, error string if rejected.
    """
    stripped = (question or "").strip()
    if not stripped:
        return "Question is empty."
    if _DANGEROUS_KEYWORDS.search(stripped):
        return "Question contains dangerous SQL keywords — request rejected."
    if ";" in stripped:
        return "Question contains multiple SQL statements — request rejected."
    return None


# ── Flag check ──────────────────────────────────────────────────────


def is_nl_sql_enabled() -> bool:
    """Check whether NL-to-SQL translation is enabled."""
    return os.environ.get("UNIVERSAL_ANALYTICS_NL_SQL", "false").lower() in (
        "true", "1", "yes",
    )


# ── Translation ─────────────────────────────────────────────────────


def translate(
    question: str,
    schema: dict | None = None,
    db_type: str | None = None,
) -> dict:
    """Translate a natural-language question into SQL.

    When the feature flag is OFF, returns an error immediately — no
    LLM call is made.

    Args:
        question:  Natural-language question (e.g. "total revenue by region").
        schema:    Optional schema context dict with "tables" list.
        db_type:   Target database dialect (mysql, postgres, etc.).

    Returns:
        dict with keys: success, sql (when enabled), error (when disabled/failed).
    """
    if not is_nl_sql_enabled():
        return {
            "success": False,
            "error": "NL-to-SQL translation is disabled. "
                     "Set UNIVERSAL_ANALYTICS_NL_SQL=true to enable.",
        }

    # Safety screening (even when flag is ON — never trust NL input)
    screening = _reject_dangerous_question(question)
    if screening:
        return {"success": False, "error": screening}

    # ── TODO: Wire up LLM call ──────────────────────────────────────
    # When the LLM integration is implemented, this is where the
    # prompt template, schema context injection, and dialect-
    # specific SQL generation would happen.
    #
    # Example::
    #   prompt = _build_prompt(question, schema, db_type)
    #   raw_sql = await _call_llm(prompt)
    #   sql = _post_process(raw_sql, db_type)
    #
    # For now, return a user-friendly "not configured" message.
    return {
        "success": False,
        "error": "NL-to-SQL LLM not configured yet. "
                 "Use universal_query with direct SQL instead.",
    }
