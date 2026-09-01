"""One-shot migration: detect agents whose name or description implies a
data-oriented role (sales, analyst, finance, BI, etc.) and rewrite their
``prompt_tools`` (L4) column to explicitly include the canonical
``ask_data_agent`` template — even when no KnowledgeBase is bound.

Background
----------
``fix_db_bound_agent_prompts.py`` handles agents that already have a
``knowledge_bases`` row, but many user-created agents (e.g. a "Sales Agent"
with no bound KB yet) fall through the gap and never get ``ask_data_agent``
into their L4 layer.  Because the LLM sees no ``ask_data_agent`` in the
tool list, it falls back to ``execute_code`` → ImportError on ``pymysql`` →
asks the user to install packages / share schemas / export CSVs.

This script closes that gap by detecting agents via a name+description regex
(``data|sales|analyst|query|metric|revenue|finance|customer|order|product|
inventory|report|forecast|chart|dashboard|kpi|bi``) and patching their
``prompt_tools`` even when no KB is bound.

Idempotency
-----------
If the agent's ``prompt_tools`` already contains the literal string
``ask_data_agent(``, it is skipped.  Re-running is safe.

Usage
-----
    cd /root/zhanlu/backend
    PYTHONPATH=. ./venv/bin/python scripts/inject_data_tools_for_named_agents.py

    # Dry-run (show what would change, no DB writes):
    PYTHONPATH=. ./venv/bin/python scripts/inject_data_tools_for_named_agents.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import re
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("inject_data_tools_for_named_agents")

# ---------------------------------------------------------------------------
# Detection regex — matches agents whose name OR description implies
# they are data-oriented and should have ask_data_agent.
# ---------------------------------------------------------------------------

_DATA_AGENT_PATTERN = re.compile(
    r"\b("
    r"data|sales|analyst|analytic|query|metric|revenue|finance|"
    r"customer|order|product|inventory|report|forecast|chart|"
    r"dashboard|kpi|bi\b|database|sql|table|warehouse|etl|"
    r"pipeline|insight|trend|statistic"
    r")\b",
    re.IGNORECASE,
)


def _is_data_agent(name: str, description: str) -> bool:
    """Return True if the agent name or description matches a data keyword."""
    text = f"{name or ''} {description or ''}"
    return bool(_DATA_AGENT_PATTERN.search(text))


# ---------------------------------------------------------------------------
# Canonical L4 template (same as fix_db_bound_agent_prompts.py)
# ---------------------------------------------------------------------------

NEW_PROMPT_TOOLS_TEMPLATE = """Tool selection: when the user asks about data, you MUST call the tool whose function name is exactly `ask_data_agent` (case-sensitive). This is the only way to reach the bound database data sources. The display name "Database Query" maps to the same tool, but the function-calling name the LLM must use in the tool_call is `ask_data_agent`.

For the bound database data sources, use `ask_data_agent` for SQL queries and any data retrieval. Use Report Generator for structured sales reports and weekly summaries. Use Chart Generator for KPI visualizations. Use analyze for deeper data analysis and trend detection. Use forecast for pipeline forecasting and commit/upside breakdowns. Use PDF Generator when a downloadable report is needed.

Function signature (use the exact `name` field when calling):
```
ask_data_agent(
    question: str,                # required — natural-language question
    data_source_id: str = None,   # optional — id of a bound source
    max_iterations: int = 6,      # optional — cap on subagent rounds (max 10)
)
```

Parameters: always validate that the question is clear; pass `data_source_id` when you know which source to query. Sequencing: for a weekly report, first call `ask_data_agent` to fetch data, then analyze, generate charts, and compile the report. Retries: if `ask_data_agent` returns an error, retry up to 3 times with a refined question before falling back. Verification: cross-reference the returned `rows` against known totals; flag anomalies. Graceful degradation: if the database is unavailable, the returned payload will say so — surface that to the user verbatim and offer to work from uploaded data files.

Anti-patterns to avoid:
- Do NOT pretend to query the database, describe steps you intend to take, or narrate a workflow without actually invoking `ask_data_agent`. Reasoning traces that list steps like "Query schema", "Run SQL", "Present results" are hallucinations when no tool call was emitted.
- Do NOT call `list_data_sources`, `describe_schema`, `execute_query`, or `answer_from_database` — those are internal to the Data Agent and are not on your tool list.
- Do NOT generate SQL in your reply text; the SQL lives inside the `ask_data_agent` payload under the `sql` field.
- NEVER ask the user to install packages, share credentials, export CSVs, or run SQL manually. Those are YOUR job."""


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def main(dry_run: bool = False) -> int:
    # Local imports so the script is self-contained.
    from app.database import SessionLocal
    from app.models.agent_app import AgentApp

    db = SessionLocal()
    try:
        agents = (
            db.query(AgentApp)
            .filter(AgentApp.is_deleted == False)  # noqa: E712
            .all()
        )
        patched: list[tuple[str, str, int, bool]] = []  # (id, name, before_len, had_kbs)
        skipped: list[tuple[str, str]] = []              # (id, name, reason)

        for agent in agents:
            name = agent.name or ""
            desc = agent.description or ""

            # Skip agents that are not data-named (no regex match on name+desc)
            if not _is_data_agent(name, desc):
                skipped.append((agent.id, name, "not a data-named agent"))
                continue

            # Idempotency — skip if prompt_tools already has ask_data_agent
            current = agent.prompt_tools or ""
            if "ask_data_agent(" in current:
                skipped.append((agent.id, name, "already has ask_data_agent"))
                continue

            # Determine if this agent also has bound KBs (for reporting)
            kbs = agent.knowledge_bases or []
            has_kbs = isinstance(kbs, list) and len(kbs) > 0

            patched.append((agent.id, name, len(current), has_kbs))
            if not dry_run:
                agent.prompt_tools = NEW_PROMPT_TOOLS_TEMPLATE
                db.add(agent)

        if not dry_run and patched:
            db.commit()

        # Report
        if patched:
            logger.info(
                "%s %d data-named agent(s):",
                "Would patch" if dry_run else "Patched",
                len(patched),
            )
            for agent_id, name, before_len, has_kbs in patched:
                kb_flag = " (has bound KBs)" if has_kbs else " (no bound KBs)"
                logger.info(
                    "  - %s  (id=%s, old prompt_tools len=%d → new=%d)%s",
                    name, agent_id, before_len, len(NEW_PROMPT_TOOLS_TEMPLATE), kb_flag,
                )
        else:
            logger.info("No data-named agents needed patching.")

        data_named = sum(1 for a in agents if _is_data_agent(a.name or "", a.description or ""))
        total = len(agents)
        logger.info(
            "Scanned %d agent(s) total — %d matched data regex, %d already had ask_data_agent, %d patched.",
            total, data_named, data_named - len(patched), len(patched),
        )

        if skipped and len(skipped) <= 20:
            logger.info("Skipped %d agent(s):", len(skipped))
            for agent_id, name, reason in skipped:
                logger.info("  - %s  (id=%s, reason=%s)", name, agent_id, reason)

        return 0
    except Exception as e:
        logger.error("Migration failed: %s", e)
        if "db" in locals():
            db.rollback()
        return 1
    finally:
        if "db" in locals():
            db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing to the database.",
    )
    args = parser.parse_args()
    sys.exit(main(dry_run=args.dry_run))
