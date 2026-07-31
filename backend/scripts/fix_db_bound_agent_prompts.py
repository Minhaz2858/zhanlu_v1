"""One-shot migration: rewrite `prompt_tools` for every agent that has
database KnowledgeBases bound, so the L4 layer explicitly names the
literal function name `ask_data_agent`.

Background
----------
The runtime correctly injects `ask_data_agent` into the LLM's tool list
and appends a "Bound Data Sources" section. But the per-agent
`prompt_tools` field was historically written by an LLM at agent-creation
time and refers to the **human display name** "Database Query" (from the
seeded `Tool` table). The LLM has no way to bridge display name → function
name, so when the user asks a data question the model hallucinates a
workflow ("Query schema" / "Present results") instead of emitting a real
`ask_data_agent` tool call.

Idempotency
-----------
This script overwrites `prompt_tools` on every qualifying agent with a
deterministic template. Re-running is safe — no change on a second run
other than the `updated_date` column.

Usage
-----
    cd /root/zhanlu/backend
    PYTHONPATH=. ./venv/bin/python scripts/fix_db_bound_agent_prompts.py

    # Dry-run (show what would change, no DB writes):
    PYTHONPATH=. ./venv/bin/python scripts/fix_db_bound_agent_prompts.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("fix_db_bound_agent_prompts")


# ---------------------------------------------------------------------------
# New template
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
- Do NOT generate SQL in your reply text; the SQL lives inside the `ask_data_agent` payload under the `sql` field."""


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
        patched: list[tuple[str, str, int]] = []  # (id, name, before_len)
        skipped: list[tuple[str, str]] = []       # (id, name, reason)

        for agent in agents:
            kbs = agent.knowledge_bases or []
            if not (isinstance(kbs, list) and len(kbs) > 0):
                skipped.append((agent.id, agent.name, "no bound KBs"))
                continue

            current = agent.prompt_tools or ""
            if current == NEW_PROMPT_TOOLS_TEMPLATE:
                skipped.append((agent.id, agent.name, "already up-to-date"))
                continue

            patched.append((agent.id, agent.name, len(current)))
            if not dry_run:
                agent.prompt_tools = NEW_PROMPT_TOOLS_TEMPLATE
                db.add(agent)

        if not dry_run and patched:
            db.commit()

        # Report
        if patched:
            logger.info(
                "%s %d agent(s) with bound KBs:",
                "Would patch" if dry_run else "Patched",
                len(patched),
            )
            for agent_id, name, before_len in patched:
                logger.info(
                    "  - %s  (id=%s, old prompt_tools length=%d → new=%d)",
                    name, agent_id, before_len, len(NEW_PROMPT_TOOLS_TEMPLATE),
                )
        else:
            logger.info("No agents needed patching.")

        if skipped:
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
