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
import socket
import time
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings  # noqa: F401 — tool enable flag
from app.services.tool_registry import registry

logger = logging.getLogger(__name__)

_MAX_ROWS_IN_RESULT = 50


# ── Pre-flight datasource reachability probe + circuit breaker ──────────────
# 2026-10-20 ROOT CAUSE (5-6 min hangs, conv eaa1bfba/c341f610): the warehouse
# MySQL flaps, and every attempt burned 30-60s of LLM pipeline calls BEFORE a
# 15s connect timeout revealed the DB was dead — then reliability retries and
# replans repeated the whole thing. A cached 2.5s TCP probe runs before the
# FSM run; any failure trips a breaker that fails every call instantly for a
# backoff window. Turned measured 300s+ failures into a sub-3s honest error.
PROBE_TIMEOUT_S = 2.5
PROBE_OK_TTL_S = 30.0        # a success caches "reachable" for this long
PROBE_DOWN_BACKOFF_S = 60.0  # breaker open window after a failed probe
_PROBE_STATE: dict[tuple[str, int], dict] = {}


def _is_non_actionable_error(error: str | None) -> bool:
    """True for environmental failures (connection/timeout/credentials) that
    a retry or replan reproduces identically. One policy, shared with the
    FSM replan loop (synexia/replan_policy.py)."""
    try:
        from app.services.synexia.replan_policy import is_non_actionable_error

        return is_non_actionable_error(error)
    except Exception:  # noqa: BLE001 — degrade to a keyword check
        low = (error or "").strip().lower()
        return any(
            m in low
            for m in ("can't connect", "timed out", "timeout", "connection")
        )


def _reset_probe_state() -> None:
    """Test helper — clear breaker/probe cache between scenarios."""
    _PROBE_STATE.clear()


def _probe_tcp(host: str, port: int, timeout: float = PROBE_TIMEOUT_S) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _check_datasource_reachable(host: str, port: int) -> tuple[bool, bool]:
    """Return (reachable, breaker_open). A cached success skips the probe;
    a failed probe opens the breaker (no further network attempts) for a
    backoff window that grows with the failure streak (capped 3x)."""
    now = time.monotonic()
    key = (host, int(port))
    st = _PROBE_STATE.get(key)
    if st:
        if st["ok_until"] > now:
            return True, False
        if st["down_until"] > now:
            return False, True
    ok = _probe_tcp(host, port)
    if ok:
        _PROBE_STATE[key] = {
            "ok_until": now + PROBE_OK_TTL_S,
            "down_until": 0.0,
            "fail_streak": 0,
        }
        return True, False
    streak = int((st or {}).get("fail_streak") or 0) + 1
    _PROBE_STATE[key] = {
        "ok_until": 0.0,
        "down_until": now + PROBE_DOWN_BACKOFF_S * min(streak, 3),
        "fail_streak": streak,
    }
    return False, False


def _unreachable_result(
    question: str, host: str, port: int, names: list[str], breaker_open: bool,
) -> dict:
    why = (
        "circuit breaker open after repeated connection failures"
        if breaker_open
        else "connection probe failed"
    )
    return {
        "success": False,
        "question": question,
        "rows": [],
        "row_count": 0,
        "source": f"{host}:{port}",
        "unreachable": True,
        "error": (
            f"The data source {host}:{port}"
            f"{' (' + ', '.join(names) + ')' if names else ''} is "
            f"unreachable — {why}."
        ),
        "detail": (
            "Pre-flight connection check failed before the NL2SQL pipeline "
            "ran, so no pipeline work was wasted. This is a network/VPN "
            "problem between the app and the database — not empty data and "
            "not a binding failure."
        ),
        "hint": (
            "Do NOT retry the same call and never invent data. Tell the "
            "user the data source cannot be reached right now (network/VPN "
            "down) and to restore connectivity, then try again."
        ),
    }


def _preflight_bound_datasources(
    db: Session, kb_ids: list, question: str,
) -> Optional[dict]:
    """Honest fast-fail error dict when a bound database source is
    unreachable, else None. File-backed KBs (no host) are skipped."""
    ids = [k for k in (kb_ids or []) if k]
    if not ids:
        return None
    try:
        from app.models.knowledge_base import KnowledgeBase

        rows = db.query(KnowledgeBase).filter(KnowledgeBase.id.in_(ids)).all()
    except Exception as exc:  # noqa: BLE001 — probe must never break the tool
        logger.debug("query_database: preflight KB lookup skipped: %s", exc)
        return None
    targets: dict[tuple[str, int], list[str]] = {}
    for kb in rows or []:
        host = str(getattr(kb, "host", None) or "").strip()
        if not host:
            continue
        port = int(getattr(kb, "port", None) or 3306)
        targets.setdefault((host, port), []).append(
            str(getattr(kb, "name", None) or "")
        )
    for (host, port), names in targets.items():
        reachable, breaker_open = _check_datasource_reachable(host, port)
        if reachable:
            continue
        logger.warning(
            "query_database: preflight FAILED for %s:%s (%s, names=%s) — "
            "failing fast before the pipeline",
            host, port,
            "breaker open" if breaker_open else "probe failed",
            names,
        )
        return _unreachable_result(question, host, port, names, breaker_open)
    return None


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
    # 2026-10-20 ROOT-CAUSE FIX (agent "unable to answer directly from the
    # database"): the CALLER's data context is authoritative. The v3 loop
    # already resolved this turn's data scope — agent bindings UNION the
    # conversation's project KBs, workspace auto-bind, pinned source — and
    # passes it in ctx as ``bound_kb_ids``. Re-deriving the scope here (the
    # old behavior) loses that context: for general_assistant in a chat with
    # no project selected, the anti-leak guard in prepare_data_source_runtime
    # zeroes bound_ids, so the FSM's nl2sql nodes fell into the "no bound
    # KBs → schema-only" fallback — SQL was GENERATED but never EXECUTED —
    # and the tool reported "success: true, 0 rows" for a query that was
    # never run. Measured live (conv fa045471, 2026-10-20): the same turn
    # where ask_data_agent (which inherits ctx) returned real rows,
    # query_database returned 0 rows three times.
    bound_kb_ids = list(ctx.get("bound_kb_ids") or [])
    endpoint = ctx.get("endpoint")

    data_ctx_extras: dict = {}
    if bound_kb_ids:
        data_ctx_extras = {"bound_kb_ids": bound_kb_ids}
    else:
        # No caller-provided scope — fall back to deriving it (non-v3
        # callers: automations, skills). Best-effort; failures fall back to
        # whatever the ctx carried.
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

    # Pre-flight: fail in ~2.5s with an honest "unreachable" error when a
    # bound database source cannot be TCP-reached — instead of running the
    # full LLM pipeline into a 15s connect timeout, repeatedly (2026-10-20).
    if getattr(settings, "NL2SQL_PREFLIGHT_PROBE_ENABLED", True):
        _preflight_error = _preflight_bound_datasources(
            db,
            list((data_ctx_extras or {}).get("bound_kb_ids") or bound_kb_ids),
            question,
        )
        if _preflight_error is not None:
            return _preflight_error

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
        # Assemble INSIDE the try: the contract's recovery path reads the
        # durable ``observation_records`` by execution id, so the session must
        # still be open here. (Normalizing after the ``finally`` closed it
        # would make every recovery lookup fail.)
        from app.services.db.result_contract import normalize_query_result

        normalized = normalize_query_result(result, fsm_db, question=question)
    except Exception as exc:  # noqa: BLE001 — tool contract: report, don't raise
        logger.warning("query_database: FSM run failed: %s", exc)
        return {
            "success": False,
            "question": question,
            "error": f"data pipeline failed: {exc}",
            "hint": "Tell the user the query could not be completed — never invent data.",
            # Typed: an FSM-level crash before any execution is not a
            # transient condition worth re-running the pipeline for (A3).
            "failure_kind": "permanent_binding_failure",
        }
    finally:
        if own_session:
            try:
                fsm_db.close()
            except Exception:  # noqa: BLE001
                pass

    # ── Assemble the result through the ONE shared contract ─────────────
    # 2026-10-27 ROOT-CAUSE FIX (RC-1). The previous code read
    # ``getattr(result, "observations", None)`` — but ``ExecutionResult``
    # (the FSM's return type) has NEITHER ``observations`` NOR ``execution``,
    # so both lookups silently returned None, ``observations`` was always
    # empty, and this tool reported "The NL2SQL pipeline did not execute any
    # query" while the SQL had actually run and returned rows. The live DB
    # proved it: every nl2sql observation for the affected turn was
    # ``success = t`` with 10 rows. That false failure is retryable by
    # default (RC-3), so the whole pipeline re-ran the same question six
    # times over 4.5 minutes.
    #
    # The contract module owns the authoritative success decision and reads
    # the durable ``observation_records`` by execution id. Recovery never
    # re-executes SQL (approved design A2); ``failure_kind`` is always
    # emitted so the reliability layer classifies it typed-first (A3).
    # (``normalized`` was computed INSIDE the try block above so the
    # observation lookup ran against the still-open session.)
    tool_result = normalized.to_tool_result()
    tool_result["question"] = question

    if not normalized.success:
        logger.warning(
            "query_database: %s (%s/%s) for question %r — execution_id=%s",
            normalized.failure_kind.value,
            normalized.execution_status.value,
            normalized.result_status.value,
            question[:80],
            normalized.provenance.get("execution_id"),
        )
        return tool_result

    verify_passed = None
    verify_detail = None
    try:
        verification = getattr(result, "verification", None) or {}
        if isinstance(verification, dict):
            verify_passed = verification.get("ok")
            verify_detail = verification.get("detail")
    except Exception:  # noqa: BLE001
        pass

    rows = normalized.rows
    tool_result.update(
        {
            "rows": rows[:_MAX_ROWS_IN_RESULT],
            "source": normalized.source_name or normalized.source_id,
            "verified": verify_passed,
            "verify_detail": verify_detail,
            # Keep the agent-facing guidance that already worked, but only
            # when rows actually exist (a zero-row success gets its own note
            # from the contract so the model never invents figures).
            "note": (
                "Grounded rows from the NL2SQL pipeline — render the answer "
                "from these rows; do not add figures that are not in them."
                if rows
                else "The query executed successfully and matched no rows. "
                "Say the data has no matching rows — never invent figures."
            ),
        }
    )
    return tool_result


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
