"""query_database tool — the NL2SQL pipeline boxed as a capability
(Option C Phase 2, 2026-10-14).

The architect's goal (approved design): ONE orchestrator loop whose data
capability is the full deterministic NL2SQL pipeline — semantic-store
grounding → plan → SQL → validation → execution → verification — instead
of a second agent brain. This tool wraps SynexiaFSM (the pipeline) so the
ReAct loop can invoke it like any other tool: the orchestrator narrates
from GROUNDED rows, the pipeline owns quality gates.

Contract returned to the orchestrator:
  {success, question, rows[:50], row_count, sql, source, verified,
   verify_detail?, error?}

The rows are GROUND TRUTH for the answer; the orchestrator must render
from them (the answer-contract rule). Rows are capped at 50 — the full
set stays in the conversation via the normal persistence path.

Flag: QUERY_DATABASE_TOOL_ENABLED (default true). The tool is only
offered to agents whose toolset includes data tools.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings  # noqa: F401 — tool enable flag
from app.services.tool_registry import registry

logger = logging.getLogger(__name__)

_MAX_ROWS_IN_RESULT = 50


async def _query_database(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    question = (args.get("question") or "").strip()
    if not question:
        return {"success": False, "error": "question is required"}

    ctx = context or {}
    agent_name = ctx.get("agent_name") or "general_assistant"
    bound_kb_ids = list(ctx.get("bound_kb_ids") or [])
    endpoint = ctx.get("endpoint")

    # The FSM builds its data grounding from the same runtime context the
    # v3 loop uses — reuse the canonical preparation with this agent's
    # bound sources (agent_app resolved by name; None is handled inside).
    data_ctx_extras: dict = {}
    try:
        from app.models.agent_app import AgentApp
        from app.services.data_source_runtime.data_source_runtime import (
            prepare_data_source_runtime,
        )

        agent_app = (
            db.query(AgentApp)
            .filter(
                AgentApp.name == agent_name,
                AgentApp.is_deleted == False,  # noqa: E712
            )
            .first()
        )
        _tools, _prompt, data_ctx_extras = prepare_data_source_runtime(
            db,
            agent_app,
            [],
            "",
            user_id=user_id,
            user_message=question,
        )
    except Exception as exc:  # noqa: BLE001 — fall back to bound ids
        logger.debug("query_database: runtime prep failed (non-fatal): %s", exc)
        if bound_kb_ids:
            data_ctx_extras = {"bound_kb_ids": bound_kb_ids}

    try:
        from app.services.synexia.fsm import ExecutionRequest, SynexiaFSM
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"pipeline unavailable: {exc}"}

    # ALWAYS a fresh session for the FSM run: fsm.run executes blocking DB
    # work via asyncio.to_thread while the CALLER's session stays owned by
    # the event loop — sharing it raised "This session is provisioning a
    # new connection; concurrent operations are not permitted" on the first
    # live Phase 3 turn (the orchestrator's retry masked it; the second
    # call won the race). Tenant context (app.current_org) propagates into
    # the thread via the contextvar set by database.py's after_begin hook.
    from app.database import SessionLocal as _SessionLocal

    fsm_db = _SessionLocal()
    own_session = True

    req = ExecutionRequest(
        conversation_id=ctx.get("conversation_id") or "",
        agent_name=agent_name,
        user_message=question,
        user_id=user_id,
        attachments=[],
        mode="dynamic",
        org_id=ctx.get("org_id"),
        app_id=ctx.get("app_id") or "default-app",
        data_ctx_extras=data_ctx_extras or None,
        endpoint=endpoint,
    )

    try:
        fsm = SynexiaFSM(fsm_db)
        result = await asyncio.to_thread(
            fsm.run, req, None, False, None
        )
    except Exception as exc:  # noqa: BLE001 — tool contract: report, don't raise
        logger.warning("query_database: FSM run failed: %s", exc)
        return {
            "success": False,
            "question": question,
            "error": f"data pipeline failed: {exc}",
            "hint": "Tell the user the query could not be completed — never invent data.",
        }
    finally:
        if own_session:
            try:
                fsm_db.close()
            except Exception:  # noqa: BLE001
                pass

    # ── Extract grounded rows from the pipeline observations ────────────
    observations = list(getattr(result, "observations", None) or [])
    if not observations:
        execution = getattr(result, "execution", None)
        observations = list(getattr(execution, "observations", None) or [])

    rows: list = []
    sql: str | None = None
    source: str | None = None
    for obs in reversed(observations):  # latest data first
        rd = getattr(obs, "result_data", None)
        if not isinstance(rd, dict):
            continue
        data = rd.get("rows") or rd.get("data")
        if isinstance(data, list) and data:
            rows = data
            sql = rd.get("sql") or sql
            source = rd.get("source_name") or source
            break

    if not rows:
        # Honest empty: the pipeline ran but produced no rows.
        return {
            "success": True,
            "question": question,
            "rows": [],
            "row_count": 0,
            "sql": sql,
            "source": source,
            "verified": False,
            "note": (
                "The data pipeline executed but returned no rows. Say the "
                "data has no matching rows — never invent figures."
            ),
        }

    verify_passed = None
    verify_detail = None
    try:
        verification = getattr(result, "verification", None) or {}
        if isinstance(verification, dict):
            verify_passed = verification.get("ok")
            verify_detail = verification.get("detail")
    except Exception:  # noqa: BLE001
        pass

    return {
        "success": True,
        "question": question,
        "rows": rows[:_MAX_ROWS_IN_RESULT],
        "row_count": len(rows),
        "sql": sql,
        "source": source,
        "verified": verify_passed,
        "verify_detail": verify_detail,
        "note": (
            "Grounded rows from the NL2SQL pipeline — render the answer "
            "from these rows; do not add figures that are not in them."
        ),
    }


QUERY_DATABASE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "query_database",
        "description": (
            "Answer a data question from the user's connected databases by "
            "running the governed NL2SQL pipeline (semantic grounding, SQL "
            "generation, validation, execution, verification). Returns "
            "grounded rows — render the answer from them. Use for rankings, "
            "aggregations, trends, comparisons, and reports."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The data question, as specific as possible.",
                },
            },
            "required": ["question"],
        },
    },
}

if getattr(settings, "QUERY_DATABASE_TOOL_ENABLED", True):
    registry.register(
        name="query_database",
        schema=QUERY_DATABASE_SCHEMA,
        handler=_query_database,
        category="data",
        toolset="data",
        description="Governed NL2SQL data queries (grounded rows).",
        emoji="🗄️",
        max_result_size_chars=100_000,
    )
