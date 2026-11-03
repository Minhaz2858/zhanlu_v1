"""delegate_task tool — spawn a subagent with isolated context.

Creates a temporary sub-conversation, runs the agent runtime loop with a
restricted toolset (no delegate_task, no memory, no send_message to prevent
infinite recursion), and returns the final text response.

Supports batch mode: pass multiple tasks as a list to run them concurrently
via asyncio.gather().
"""

import asyncio
import json
import logging
import uuid

from sqlalchemy.orm import Session

from app.config import settings
from app.services.llm_router import LLMEndpoint
from app.services.tool_registry import registry
from app.services.sub_agent_reliability import (
    call_llm_with_reliability,
    pre_call_prep,
    persist_result_str,
    apply_turn_budget_to_batch,
    ToolLoopGuardController,
    synthetic_blocked_result,
    IterationBudget,
    metrics,
)

logger = logging.getLogger(__name__)

# Phase 8 T8-6: parent identity/trace keys copied into the child context for
# PROVENANCE LINKAGE only. Authorization-relevant keys are deliberately
# excluded — the child is permission-checked as itself (see the security
# note at the _child_ctx construction site).
_PARENT_REF_KEYS = ("agent_name", "agent_app_id", "run_id")
_PARENT_REF_PREFIX = "parent_"


def provenance_parent_refs(context: dict | None) -> dict:
    """Extract parent identity/trace references (provenance-only).

    Returns ``{parent_agent_name, parent_agent_app_id, parent_run_id}`` for
    whichever keys the parent context actually carries — never bindings,
    permissions, or tool configuration."""
    if not isinstance(context, dict):
        return {}
    refs: dict = {}
    for key in _PARENT_REF_KEYS:
        value = context.get(key)
        if value:
            refs[f"{_PARENT_REF_PREFIX}{key}"] = value
    return refs


def _bound_child_toolset(
    task: str,
    tool_schemas: list[dict],
    *,
    protected: set[str],
    budget: int | None = None,
) -> list[dict]:
    """Bound a delegate child's injected toolset (dynamic tool discovery).

    Keeps every ``protected`` schema (operator priority head, always-on
    evidence tools), fills the remaining budget with the TASK-RELEVANT
    top-K of the remainder (same ranking mechanism as the supervisor
    loop's ``select_relevant_tools``), and appends the ``search_tools``
    escape hatch so the child can discover and invoke any other registered
    tool on demand. No-op when TOOL_SEARCH_ENABLED is off or the list
    already fits the budget. Never raises — returns the input unchanged
    on any failure.
    """
    try:
        if not getattr(settings, "TOOL_SEARCH_ENABLED", False):
            return tool_schemas
        k = int(budget or getattr(settings, "TOOL_SEARCH_TOP_K", 32))
        if len(tool_schemas) <= k:
            return tool_schemas

        from app.services.tool_search import SEARCH_TOOLS_SCHEMA, rank_tools

        def _name(s: dict) -> str:
            fn = (s or {}).get("function", {}) or {}
            return fn.get("name") or (s or {}).get("name") or ""

        prot: list[dict] = []
        rest: list[dict] = []
        for s in tool_schemas:
            (prot if _name(s) in protected else rest).append(s)
        # Operator preference wins over the budget: the whole priority
        # head is always kept even if it alone exceeds ``budget``.
        slots = max(0, k - len(prot))
        if len(rest) <= slots:
            selected = rest
        else:
            scores = rank_tools(task or "", rest)
            order = sorted(range(len(rest)), key=lambda i: -scores[i])[:slots]
            selected = [rest[i] for i in order]
        merged = prot + selected
        if not any(_name(s) == "search_tools" for s in merged):
            merged.append(dict(SEARCH_TOOLS_SCHEMA))
        logger.info(
            "delegate_tool: bounded child toolset %d -> %d schemas (task %r)",
            len(tool_schemas), len(merged), (task or "")[:60],
        )
        return merged
    except Exception as exc:  # noqa: BLE001 — never break tool assembly
        logger.warning("delegate_tool: toolset bounding failed, keeping all (%s)", exc)
        return tool_schemas

# Tools that sub-agents CANNOT use (prevent infinite recursion and loops)
_RESTRICTED_TOOLS = {"delegate_task", "memory", "send_message"}


def _resolve_caller_org(db: Session, user_id: str | None,
                        context: dict | None) -> str | None:
    """Resolve the caller's organization (user → org_id, else the
    conversation's org). Returns None when unresolvable."""
    if user_id:
        try:
            from app.models.user import User

            _u = db.query(User).filter(User.id == user_id).first()
            if _u is not None:
                return getattr(_u, "org_id", None)
        except Exception:  # noqa: BLE001
            return None
    if context and context.get("conversation_id"):
        try:
            from app.models.agent_conversation import AgentConversation

            _conv = (
                db.query(AgentConversation)
                .filter(AgentConversation.id == context["conversation_id"])
                .first()
            )
            if _conv is not None:
                return getattr(_conv, "org_id", None)
        except Exception:  # noqa: BLE001
            return None
    return None


def _resolve_agent_app(db: Session, agent_name: str | None,
                       caller_org_id: str | None = None):
    """P0-1 (2026-10-31): resolve a PUBLISHED AgentApp by exact name.

    Phase 10 (audit S5.1): the resolution is ORG-SCOPED — a published
    AgentApp of ANOTHER organization never resolves (its prompt and
    tool_config are tenant data). ``caller_org_id`` comes from
    ``_resolve_caller_org``; callers that cannot pass a verified org must
    not use this function to execute foreign definitions.

    Returns None (never raises) when the name does not resolve to a
    published AgentApp of the caller's org — builtin definitions keep
    their existing resolution path. Only ``status == "published"`` apps
    are delegable: this is the same gate ``list_available_agents``
    advertises to the LLM (see _list_available_agents).
    """
    if not agent_name:
        return None
    try:
        from app.models.agent_app import AgentApp

        filters = [
            AgentApp.name == agent_name,
            AgentApp.is_deleted == False,  # noqa: E712
            AgentApp.status == "published",
        ]
        if caller_org_id:
            filters.append(AgentApp.org_id == caller_org_id)
        return (
            db.query(AgentApp)
            .filter(*filters)
            .first()
        )
    except Exception as exc:  # noqa: BLE001 — resolution is best-effort
        logger.debug("delegate: AgentApp resolution failed for %r: %s", agent_name, exc)
        return None


def _normalize_subagent_schema(name: str, schema: dict, description: str = "") -> dict:
    """Wrap a registry schema in the OpenAI function envelope.

    Most registry entries are already wrapped (``{type: "function",
    function: {...}}``). Two shapes are NOT and get rejected by
    DeepSeek/OpenAI with ``400 tools[N].type: unknown variant 'object'``:

    1. Bare parameters dicts — the ``universal_*`` DB-agnostic data tools
       register ``{type: "object", properties, required}`` (no name).
    2. Flat form — ``collect_enterprise_data`` is
       ``{name, description, parameters}`` with no top-level ``type``.

    This normalizes all three shapes to the envelope form.
    """
    if not isinstance(schema, dict):
        return schema
    if schema.get("type") == "function" and "function" in schema:
        return schema
    if schema.get("name") or "parameters" in schema:
        return {
            "type": "function",
            "function": {
                "name": schema.get("name") or name,
                "description": schema.get("description", ""),
                "parameters": schema.get("parameters")
                or {k: v for k, v in schema.items() if k not in ("name", "description")},
            },
        }
    # Bare parameters dict (universal_* tools)
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description or "",
            "parameters": schema,
        },
    }


def _build_parent_context_digest(db: Session, context: dict | None) -> str:
    """Compact digest of the parent conversation for sub-agent grounding.

    IBM MAS shared-memory fix: sources, in priority order —
    1. ContextBus sections (if the caller wired one)
    2. The parent conversation's last exchanges (DB, bounded)
    Bounded to ~1200 chars so fan-out of 5 sub-agents doesn't 5× the cost.
    """
    try:
        ctx = context or {}
        conv_id = ctx.get("conversation_id")

        # 1. ContextBus (shared knowledge base) when the caller provided one
        bus = ctx.get("context_bus")
        if bus is not None:
            try:
                digest = bus.format_for_react()
                if digest and digest.strip():
                    return digest[:1200]
            except Exception:  # noqa: BLE001
                pass

        # 2. Last exchanges from the parent conversation
        if conv_id:
            from app.models.agent_conversation import AgentConversation

            conv = (
                db.query(AgentConversation)
                .filter(AgentConversation.id == conv_id)
                .first()
            )
            if conv is not None:
                msgs = conv.messages or []
                recent = [
                    m for m in msgs[-6:]
                    if isinstance(m, dict) and m.get("content")
                ]
                lines = [
                    f"{m.get('role', '?')}: {str(m.get('content'))[:300]}"
                    for m in recent
                ]
                if lines:
                    return "\n".join(lines)[:1200]
    except Exception as e:  # noqa: BLE001 — inheritance is best-effort
        logger.debug("parent context digest failed (non-fatal): %s", e)
    return ""


async def _verify_sub_agent_output(task: str, response: str) -> dict | None:
    """Optional quality gate on sub-agent output (ADLC/MAS supervision).

    Attaches an LLM-as-judge verdict to the delegation result. Flag-gated
    (DELEGATE_VERIFY_ENABLED, default False — one judge call per delegation).
    """
    try:
        if not getattr(settings, "DELEGATE_VERIFY_ENABLED", False):
            return None
        from app.services.synexia.quality_eval import evaluate_quality

        result = evaluate_quality(user_message=task, assistant_text=response)
        return {
            "verification": {
                "verdict": result.verdict,
                "completeness": getattr(result, "completeness_score", None),
                "confidence": getattr(result, "confidence", None),
            }
        }
    except Exception as e:  # noqa: BLE001 — verification is advisory
        logger.debug("sub-agent verification failed (non-fatal): %s", e)
        return None


async def _delegate_task(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    task = args.get("task", "").strip()
    agent_name = args.get("agent_name", "general_assistant")
    # 2026-10-10: the ≤10 clamp was a literal that truncated multi-step
    # subtasks ("iteration budget exhausted"). Settings-backed now.
    _delegate_clamp = int(getattr(settings, "DELEGATE_ITERATIONS_HARD_CLAMP", 25))
    max_iterations = min(args.get("max_iterations", settings.DELEGATE_MAX_ITERATIONS), _delegate_clamp)
    endpoint: LLMEndpoint | None = (context or {}).get("endpoint")

    if not task:
        return {"success": False, "error": "task is required"}

    # Check for batch mode (list of tasks)
    tasks = args.get("tasks")
    if tasks and isinstance(tasks, list):
        return await _delegate_batch(tasks, agent_name, db, user_id, max_iterations, endpoint, context)

    # Single task
    result = await _run_sub_agent(task, agent_name, db, user_id, max_iterations, endpoint, context=context)
    return result


async def _delegate_batch(
    tasks: list[str],
    agent_name: str,
    db: Session,
    user_id: str | None,
    max_iterations: int,
    endpoint: LLMEndpoint | None = None,
    context: dict | None = None,
) -> dict:
    """Run multiple sub-agent tasks concurrently.

    When the v3 FSM passes ``context["progress_emitter"]`` (mutable list) +
    ``context["step_counter"]`` ([int]), emits one activity_step pair per
    sub-agent (running → done) so the chat UI shows parallel worker cards.
    No-op otherwise (safe for tests / batch callers).
    """
    import time as _time

    _progress = (context or {}).get("progress_emitter")
    _step_ctr = (context or {}).get("step_counter")

    def _emit(label: str, status: str, dur_ms: int | None = None, tool: str = "delegate_task") -> None:
        if _progress is None or _step_ctr is None:
            return
        _step_ctr[0] += 1
        from app.services.agent_loop.sse_builders import _emit_activity_step

        _progress.append(
            _emit_activity_step(_step_ctr[0], label, status, duration_ms=dur_ms, tool_name=tool)
        )

    starts = [_time.monotonic() for _ in tasks]
    for i, task in enumerate(tasks):
        _emit(f"Sub-agent {i + 1}: {task[:60]}", "running")

    # IBM MAS fix (2026-10-08): batch fan-out was unbounded — every sub-agent
    # launched at once, hammering the LLM provider and the DB pool. Bound it.
    import asyncio as _asyncio

    _max_conc = max(1, int(getattr(settings, "DELEGATE_MAX_CONCURRENT", 4)))
    _sem = _asyncio.Semaphore(_max_conc)

    async def _run_one(i: int, task: str) -> dict:
        async with _sem:
            try:
                res = await _run_sub_agent(task, agent_name, db, user_id, max_iterations, endpoint, context=context)
            except Exception as exc:  # noqa: BLE001
                res = {"success": False, "error": str(exc), "task": task}
        dur = int((_time.monotonic() - starts[i]) * 1000)
        _emit(
            f"Sub-agent {i + 1}: {task[:60]}",
            "done" if res.get("success") else "failed",
            dur_ms=dur,
        )
        return res

    results = await asyncio.gather(*[_run_one(i, t) for i, t in enumerate(tasks)], return_exceptions=True)

    batch_results = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            batch_results.append({
                "task": tasks[i][:100],
                "success": False,
                "error": str(r),
            })
        else:
            batch_results.append({
                "task": tasks[i][:100],
                **r,
            })

    return {
        "success": all(r.get("success", False) for r in batch_results),
        "results": batch_results,
        "count": len(batch_results),
    }


async def _run_sub_agent(
    task: str,
    agent_name: str,
    db: Session,
    user_id: str | None,
    max_iterations: int,
    endpoint: LLMEndpoint | None = None,
    context: dict | None = None,
) -> dict:
    """Run a sub-agent conversation with restricted tools (recorded)."""
    from datetime import datetime, timezone

    from app.services.agent_invocations import (
        record_invocation, update_invocation_terminal,
    )

    _started = datetime.now(timezone.utc)
    # P1/P2 (2026-10-31): Collaboration Broker governance for Pattern A/B/C.
    # Evaluate BEFORE spawning the child. On block, return a structured
    # "blocked" result — the finally-block records it as an observable
    # (blocked) invocation row. No silent swallow, no crash.
    from app.services.agent_collaboration import broker

    _allowed, _reason, _child_context = broker.evaluate_delegation(
        target_agent_id=agent_name, objective=task, context=context, db=db,
        parent_invocation_id=(context or {}).get("parent_invocation_id"),
    )
    if not _allowed:
        # The broker already recorded an auditable "blocked" invocation via
        # _audit_block — do not double-record here.
        return {
            "success": False,
            "status": "blocked",
            "error": _reason or "delegation blocked by collaboration policy",
            "task": task,
            "agent_name": agent_name,
        }
    # P0-1: resolve the child AgentApp ONCE here so both the inner loop
    # (prompt + toolset) and the invocation record use the same object.
    # Phase 10 (audit S5.1): org-scoped — when AgentApp-aware delegation is
    # enabled but the caller's org CANNOT be verified, the delegation is
    # BLOCKED (fail closed): an unscoped resolution could execute another
    # org's published agent definition.
    _agent_app = None
    if getattr(settings, "DELEGATE_AGENTAPP_AWARE", False):
        _caller_org = _resolve_caller_org(db, user_id, context)
        if not _caller_org:
            logger.warning(
                "delegate: caller org unresolvable (user_id=%r) — blocking "
                "AgentApp delegation (fail closed)", user_id)
            return {
                "success": False,
                "status": "blocked",
                "error": (
                    "Delegation refused: the caller's organization could "
                    "not be verified for agent-scoped delegation."
                ),
                "task": task,
                "agent_name": agent_name,
            }
        _agent_app = _resolve_agent_app(db, agent_name, _caller_org)
    # Part 1.2: single effective deadline shared by the inner wall-check AND the
    # real coroutine-level cancellation below.
    _wall_s = float(getattr(settings, "DELEGATE_WALL_SECONDS", 600))
    if getattr(settings, "COLLAB_POLICY_ENFORCED", True):
        _wall_s = float(getattr(settings, "COLLAB_TIMEOUT_SECONDS", _wall_s))
    # P1/P2: global top-level concurrency gate (COLLAB_MAX_CONCURRENT). Nested
    # Pattern-C delegations are excluded inside the broker (deadlock-free); this
    # bounds the entry-point fan-out across all conversations/parents. A blocked
    # attempt is returned as status="blocked" and recorded by the finally block.
    _acquired = False
    result = None
    try:
        _acq, _acq_reason = broker.acquire_concurrency(context)
        if not _acq:
            return {
                "success": False,
                "status": "blocked",
                "error": _acq_reason or "concurrent task limit exceeded",
                "task": task,
                "agent_name": agent_name,
            }
        _acquired = True
        # Mission §23 / Hybrid H1 (2026-10-31): record the child invocation
        # AT START as "running" so in-flight specialists are visible in the
        # authoritative execution tree (reconnection mid-flight shows real
        # state, not an empty tree). The SAME row is finalized in the finally
        # block — no duplicate, no fabricated completion. If the start row
        # cannot be created (e.g. no resolvable agent app, same rule as the
        # legacy terminal insert), the finally block falls back to the legacy
        # terminal-only insert, so behavior is never worse than before.
        _req0 = _child_context.get("requesting_agent_id")
        _inv_row_id = None
        try:
            _inv_row_id = record_invocation(
                db,
                conversation_id=_child_context.get("conversation_id"),
                agent_app_id=(_agent_app.id if _agent_app else None),
                user_id=user_id,
                invocation_type="sub_agent",
                trigger="agent" if _req0 and _req0 != "general_assistant" else "supervisor",
                input_message=task[:2000],
                status="running",
                open_invocation=True,
                started_at=_started,
                model_name=endpoint.model_id if endpoint else None,
                parent_invocation_id=_child_context.get("parent_invocation_id"),
                trace_id=_child_context.get("trace_id"),
                delegation_mode="peer" if _req0 and _req0 != "general_assistant" else "supervisor",
            )
        except Exception as _inv_start_err:  # noqa: BLE001 — non-fatal
            logger.warning("delegate: invocation start record failed (non-fatal): %s",
                           _inv_start_err)
            _inv_row_id = None
        # ── Multi-agent milestone (2026-11-01): the specialist now runs with
        # a LIVE execution identity — structured agent-execution events are
        # appended to the parent's progress channel (drained in real time by
        # the router heartbeat) so the Agent Execution panel renders the
        # specialist BEFORE the final answer. ``_agent_execution_id`` is the
        # invocation-row id (same id the persisted tree uses) — live cards
        # and the post-turn tree dedupe on it.
        _agent_execution_id = _inv_row_id or str(uuid.uuid4())
        _child_context = dict(_child_context or {})
        _child_context["_agent_execution_id"] = _agent_execution_id
        try:
            from app.services.agent_loop.sse_builders import (
                append_agent_execution_event,
            )

            append_agent_execution_event(
                (context or {}).get("progress_emitter"), "agent_started",
                {
                    "agent_execution_id": _agent_execution_id,
                    "parent_agent_execution_id": _child_context.get("parent_invocation_id"),
                    "agent_id": (_agent_app.id if _agent_app else None),
                    "agent_name": agent_name,
                    "agent_label": (_agent_app.name if _agent_app else agent_name),
                    "task_id": _child_context.get("turn_id"),
                    "conversation_id": _child_context.get("conversation_id"),
                    "status": "running",
                    "delegation_mode": (
                        "peer" if _req0 and _req0 != "general_assistant" else "supervisor"
                    ),
                    "task": task[:200],
                    # 2026-11-02 (dynamic orchestration timeline): link the
                    # agent to the plan task it serves, when the caller's
                    # context carries one — the frontend nests the agent row
                    # under that plan task instead of guessing by timestamp.
                    "plan_task_id": (context or {}).get("plan_task_id"),
                },
            )
            # Peer-to-peer collaboration is a distinct orchestration fact
            # (2026-11-02): announce it on the timeline. The event type was
            # whitelisted since Phase 2.8 but never emitted.
            if _req0 and _req0 != "general_assistant":
                append_agent_execution_event(
                    (context or {}).get("progress_emitter"),
                    "peer_collaboration_started",
                    {
                        "agent_execution_id": _agent_execution_id,
                        "agent_label": (_agent_app.name if _agent_app else agent_name),
                        "conversation_id": _child_context.get("conversation_id"),
                    },
                )
        except Exception:  # noqa: BLE001 — observability never breaks delegation
            pass
        # P1 contract: force a machine-readable status on EVERY sub-agent result
        # (single-task path — the batch path does the same). The parent LLM and
        # the evidence card both key off it; never ship a bare success dict.
        # Part 1.2: asyncio.wait_for CANCELS the child coroutine on timeout
        # (interrupting an in-flight LLM/HTTP await) — not merely truncating the
        # parent's view. The inner wall-check remains defense-in-depth.
        try:
            _child = await asyncio.wait_for(
                _run_sub_agent_inner(
                    task, agent_name, db, user_id, max_iterations, endpoint,
                    context=_child_context, agent_app=_agent_app,
                    wall_seconds=_wall_s,
                ),
                timeout=_wall_s,
            )
        except asyncio.TimeoutError:
            _child = {
                "success": False, "status": "timeout",
                "error": f"sub-agent exceeded deadline ({_wall_s}s)",
                "task": task, "agent_name": agent_name,
            }
        if _child.get("success"):
            _child.setdefault("status", "partial" if _child.get("truncated") else "completed")
            # MAS supervision gate (opt-in): attach an LLM-judge verdict to the
            # sub-agent's answer before the parent consumes it.
            if _child.get("response"):
                verification = await _verify_sub_agent_output(task, _child["response"])
                if verification:
                    _child.update(verification)
        else:
            _child["status"] = _child.get("status") or "failed"
        result = _child
        # P2 (Part 1): selective fact-safe review + bounded targeted
        # re-investigation. Only when the caller opted in (review=True).
        if result.get("success") and (context or {}).get("_enable_review"):
            result = await _maybe_review_and_refine(
                task, agent_name, db, user_id, endpoint, context, result,
            )
    except asyncio.CancelledError:
        # Parent cancellation propagated into this child. Record it honestly,
        # then re-raise so the cancellation continues after cleanup (do NOT
        # swallow a shutdown as a success).
        result = {
            "success": False, "status": "cancelled",
            "error": "delegation cancelled by parent",
            "task": task, "agent_name": agent_name,
        }
        raise
    except Exception as exc:  # noqa: BLE001
        result = {"success": False, "error": str(exc), "task": task}
    finally:
        if _acquired:
            broker.release_concurrency()
        # Per-sub-agent invocation row (P1, 2026-08-29): duration + status so
        # delegation is observable and costed like the main loop.
        try:
            if result is None:
                result = {"success": False, "status": "cancelled",
                          "task": task, "agent_name": agent_name}
            _it = result.get("iterations")
            _requesting = _child_context.get("requesting_agent_id") or _child_context.get("agent_name")
            _tu = result.get("token_usage") or None
            _terminal_status = "completed" if result.get("success") else (result.get("status") or "failed")
            _terminal_at = datetime.now(timezone.utc)
            _terminal_out = {
                "task": task[:200],
                "iterations": _it,
                "agent_name": agent_name,
                "requesting_agent_id": _requesting,
                "truncated": bool(result.get("truncated")),
                "delegation_path": _child_context.get("delegation_path"),
                # P1/P2: persist the negotiated Agent Task Contract on the
                # invocation so the parent-child link is auditable/observable
                # (not just carried in the in-memory context).
                "task_contract": _child_context.get("agent_task_contract"),
                # P2 Phase 3: persist the reviewer verdict (if this
                # delegation was reviewed) and the evidence ids it produced /
                # consumed so the execution-tree visualization can render the
                # critic/reviewer panel and evidence references historically.
                "review": result.get("review"),
                "evidence_ids": result.get("evidence_ids"),
            }
            # Mission §23 / Hybrid H1: finalize the SAME row created at
            # delegation start (single row, truthful terminal state). If the
            # start row never materialized, fall back to the legacy
            # terminal-only insert so the record is never lost. Chat-experience
            # repair (2026-10-31): a REFUSED update (row already terminal, or
            # transient failure) must NOT insert a second row — that produced
            # a stale "running" row PLUS a terminal duplicate in the
            # execution tree. Fall back only when the start row genuinely
            # does not exist.
            if _inv_row_id and update_invocation_terminal(
                db,
                _inv_row_id,
                status=_terminal_status,
                output_json=_terminal_out,
                error_message=result.get("error"),
                completed_at=_terminal_at,
                duration_ms=max(0, int((_terminal_at - _started).total_seconds() * 1000)),
                token_usage=_tu,
                cost_amount=_compute_child_cost(_tu),
                assistant_content=(result.get("response") or "")[:10000],
                tool_call_count=_it if isinstance(_it, int) else None,
                model_name=endpoint.model_id if endpoint else None,
            ):
                pass
            elif _inv_row_id and _invocation_row_exists(db, _inv_row_id):
                # Row exists but the terminal update was refused (already
                # terminal — first-terminal-wins). Nothing to write.
                logger.warning(
                    "delegate: terminal update refused for invocation %s "
                    "(already terminal?) — no fallback insert", _inv_row_id)
            else:
                record_invocation(
                db,
                # P0-5 (2026-10-31): link the delegation to the parent
                # conversation (was hardcoded None — sub-agent runs were
                # invisible in the observability drawer's conversation view).
                conversation_id=_child_context.get("conversation_id"),
                agent_app_id=(result.pop("_agent_app_id", None) or None),
                user_id=user_id,
                invocation_type="sub_agent",
                trigger="agent" if _requesting and _requesting != "general_assistant" else "supervisor",
                input_message=task[:2000],
                status="completed" if result.get("success") else (result.get("status") or "failed"),
                output_json={
                    "task": task[:200],
                    "iterations": _it,
                    "agent_name": agent_name,
                    "requesting_agent_id": _requesting,
                    "truncated": bool(result.get("truncated")),
                    "delegation_path": _child_context.get("delegation_path"),
                    # P1/P2: persist the negotiated Agent Task Contract on the
                    # invocation so the parent-child link is auditable/observable
                    # (not just carried in the in-memory context).
                    "task_contract": _child_context.get("agent_task_contract"),
                    # P2 Phase 3: persist the reviewer verdict (if this
                    # delegation was reviewed) and the evidence ids it produced /
                    # consumed so the execution-tree visualization can render the
                    # critic/reviewer panel and evidence references historically.
                    "review": result.get("review"),
                    "evidence_ids": result.get("evidence_ids"),
                },
                assistant_content=(result.get("response") or "")[:10000],
                error_message=result.get("error"),
                started_at=_started,
                completed_at=datetime.now(timezone.utc),
                duration_ms=max(0, int((datetime.now(timezone.utc) - _started).total_seconds() * 1000)),
                model_name=endpoint.model_id if endpoint else None,
                tool_call_count=_it if isinstance(_it, int) else None,
            )
        except Exception as _inv_err:  # noqa: BLE001 — non-fatal
            logger.warning("delegate: invocation record failed (non-fatal): %s", _inv_err)
    return result


async def _run_sub_agent_inner(
    task: str,
    agent_name: str,
    db: Session,
    user_id: str | None,
    max_iterations: int,
    endpoint: LLMEndpoint | None = None,
    context: dict | None = None,
) -> dict:
    """Run a sub-agent conversation with restricted tools."""
    from app.services.agent_prompts import get_system_prompt, _get_all_crud_schemas

    # Build system prompt for the sub-agent
    system_prompt = get_system_prompt(agent_name) or (
        "You are a helpful AI assistant. Complete the task concisely and accurately."
    )
    # Add delegation context
    system_prompt += "\n\nYou are operating as a sub-agent with a restricted toolset. " \
                     "Complete the task efficiently and return your findings."

    # IBM MAS fix (2026-10-08): sub-agents used to start with ZERO parent
    # context — only the task string. Inject a compact digest of the parent
    # conversation (shared knowledge base) so the sub-agent models the
    # parent's goal instead of re-deriving it. Flag-gated.
    if getattr(settings, "DELEGATE_CONTEXT_INHERITANCE", True):
        parent_digest = _build_parent_context_digest(db, context)
        if parent_digest:
            system_prompt += (
                "\n\n[PARENT CONVERSATION CONTEXT — background for your task; "
                "do not answer it directly]\n" + parent_digest
            )

    # Build available tools (exclude restricted tools)
    all_tool_names = registry.list_available()
    available_names = [t for t in all_tool_names if t not in _RESTRICTED_TOOLS]
    tool_schemas = []
    for name in available_names:
        entry = registry.get_entry(name)
        if entry and entry.enabled_by_default:
            tool_schemas.append(_normalize_subagent_schema(name, entry.schema, entry.description or ""))

    # Also include CRUD tool schemas if the sub-agent needs them
    # (but exclude delegate_task which is not in CRUD anyway)
    crud_schemas = _get_all_crud_schemas()
    for name, schema in crud_schemas.items():
        if name not in _RESTRICTED_TOOLS and schema not in tool_schemas:
            tool_schemas.append(schema)

    # Build messages
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]

    # P0-2 (2026-10-31, audit breakpoint 3): the child's tool calls run with
    # a context that carries the PARENT's conversation_id + bound_kb_ids so
    # ask_data_agent inside the child resolves the parent's bound data
    # sources. SECURITY: the child is permission-checked AS ITSELF — the
    # parent's agent_name / agent_app_id are deliberately NOT inherited
    # (context["agent_name"] drives the system-meta-agent confirmation
    # bypass and the tier policy; inheriting it would leak the parent's
    # privileges into the child).
    _child_ctx: dict = {"agent_name": agent_name}
    if agent_app is not None:
        _child_ctx["agent_app_id"] = agent_app.id
    # Phase 8 T8-6: provenance linkage — the child's run can be traced to
    # its parent. Identity/trace references only; no authority transfer.
    _child_ctx.update(provenance_parent_refs(context))
    if getattr(settings, "DELEGATE_INHERIT_BINDINGS", False) and context:
        if context.get("conversation_id"):
            _child_ctx["conversation_id"] = context["conversation_id"]
        if context.get("bound_kb_ids"):
            _child_ctx["bound_kb_ids"] = list(context["bound_kb_ids"])
    # P1/P2 (2026-10-31): carry the collaboration governance lineage into the
    # child's context so a specialist-initiated delegation (Pattern C) is
    # itself governed — its own delegate_task call will see this path and be
    # cycle/ depth/ child-count checked. SECURITY: we copy ONLY the lineage
    # keys (path, ids, trace) — never permissions or agent identity override.
    if context:
        for _k in (
            "delegation_path", "requesting_agent_id", "parent_invocation_id",
            "trace_id", "conversation_id", "organization_id", "user_id",
            "project_id", "department", "_collab_children", "_collab_seen",
            "token_budget",
            # Multi-agent milestone: the live execution identity + the
            # parent's progress channel so the child's tool loop emits
            # agent_tool_started/completed frames under ITS execution id —
            # the panel renders tools under the specialist that ran them.
            "_agent_execution_id", "progress_emitter", "step_counter",
            "turn_id",
            # P1/P2: carry the negotiated Agent Task Contract so a nested
            # (Pattern-C) delegation sees the same governing contract and the
            # contract survives to the child's own invocation record.
            "agent_task_contract",
        ):
            if _k in context and _k not in _child_ctx:
                _child_ctx[_k] = context[_k]

    # P0 reliability: per-turn guardrail controller + iteration budget
    guard_ctrl = ToolLoopGuardController()
    conv_budget = IterationBudget(max_total=max_iterations)

    # 2026-10-22 P1 (audit: no central tool timeout): hard wall-clock ceiling
    # so a single delegate_task sub-agent can never run unbounded (the audit
    # noted a worst-case >2h hang with no wait_for anywhere). On timeout we
    # break and return whatever response text was accumulated (truncated=True)
    # rather than failing hard or hanging.
    import time as _time

    _wall_start = _time.monotonic()
    _wall_s = float(getattr(settings, "DELEGATE_WALL_SECONDS", 600))

    # P1-4 ReWOO light mode: research-shaped sub-agent tasks run as
    # plan→execute→solve (2 LLM calls instead of one per tool step).
    # Fail-open: any failure/invalid plan falls through to ReAct below.
    try:
        from app.services.rewoo import rewoo_should_run, run_rewoo

        if rewoo_should_run(agent_name, task, tool_schemas):
            rewoo_result = await run_rewoo(
                task, tool_schemas, db=db, user_id=user_id,
                endpoint=endpoint,
                max_steps=int(getattr(settings, "REWOO_MAX_STEPS", 6)),
            )
            if rewoo_result.get("success"):
                metrics.record_budget(1, conv_budget.max_total)
                return rewoo_result
            logger.info(
                "rewoo: falling back to ReAct for %r: %s",
                task[:80], rewoo_result.get("error"),
            )
    except Exception as _rewoo_err:  # noqa: BLE001 — never break delegation
        logger.warning("rewoo: crashed, ReAct fallback: %s", _rewoo_err)

    # Run tool-calling loop
    for iteration in range(max_iterations):
        # P0: consume one iteration from the conversation-level budget
        if not conv_budget.consume():
            logger.info(
                "Sub-agent (delegate) iteration budget exhausted (%d/%d), breaking",
                conv_budget.used, conv_budget.max_total,
            )
            break

        # P1: hard wall-clock cap (prevents runaway sub-agents).
        if _time.monotonic() - _wall_start > _wall_s:
            logger.warning(
                "Sub-agent (delegate) wall-clock budget exceeded (%.1fs > %.1fs), truncating",
                _time.monotonic() - _wall_start, _wall_s,
            )
            break

        # P1.3/P2: pre-API pruning + message sanitization
        pre_call_prep(messages)

        try:
            llm_response = await call_llm_with_reliability(
                messages, tool_schemas, endpoint=endpoint
            )
        except Exception as e:
            logger.warning("Sub-agent LLM call failed: %s", e)
            metrics.record_budget(conv_budget.used, conv_budget.max_total)
            return {"success": False, "error": f"LLM call failed: {e}", "task": task}

        content = llm_response.get("content", "")
        raw_tool_calls = llm_response.get("tool_calls", [])

        if not raw_tool_calls:
            # Final text response
            metrics.record_budget(conv_budget.used, conv_budget.max_total)
            return {
                "success": True,
                "task": task,
                "response": content,
                "iterations": iteration + 1,
            }

        # Process tool calls — parallel execution for multiple calls,
        # sequential for single (mirrors the main agent loop pattern).
        # return_exceptions=True ensures one failure doesn't cancel siblings.
        parsed_calls = []
        for tc in raw_tool_calls:
            func = tc.get("function", {})
            tool_name = func.get("name", "")
            args_str = func.get("arguments", "{}")
            tool_call_id = tc.get("id", str(uuid.uuid4()))
            try:
                tool_args = json.loads(args_str) if isinstance(args_str, str) else args_str
            except json.JSONDecodeError:
                tool_args = {}
            parsed_calls.append({
                "tool_name": tool_name,
                "tool_args": tool_args,
                "args_str": args_str,
                "tool_call_id": tool_call_id,
            })

        # Execute tools with P0 guardrail before_call checks.
        from app.services.agent_tools import execute_tool
        if len(parsed_calls) == 1:
            call = parsed_calls[0]
            _gd = guard_ctrl.before_call(call["tool_name"], call["tool_args"])
            if _gd.allows_execution:
                results = [await execute_tool(
                    call["tool_name"],
                    call["tool_args"],
                    db, user_id,
                )]
            else:
                results = [json.loads(synthetic_blocked_result(_gd))]
        else:
            async def _exec_one(call):
                from app.database import tool_session
                with tool_session() as _s:
                    _gd = guard_ctrl.before_call(call["tool_name"], call["tool_args"])
                    if _gd.allows_execution:
                        return await execute_tool(
                            call["tool_name"], call["tool_args"], _s, user_id,
                        )
                    return json.loads(synthetic_blocked_result(_gd))
            raw_results = await asyncio.gather(
                *[_exec_one(c) for c in parsed_calls],
                return_exceptions=True,
            )
            results = []
            for i, r in enumerate(raw_results):
                if isinstance(r, Exception):
                    logger.warning(
                        "Sub-agent tool '%s' raised: %s",
                        parsed_calls[i]["tool_name"], r,
                    )
                    results.append({"success": False, "error": f"{type(r).__name__}: {r}"})
                else:
                    results.append(r)

        # Build messages in original order (LLM API requires matching order).
        # P0: apply Layer 2 per-result persistence to large results.
        for call, result in zip(parsed_calls, results):
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": call["tool_call_id"],
                    "type": "function",
                    "function": {
                        "name": call["tool_name"],
                        "arguments": call["args_str"],
                    },
                }],
            })
            _result_str = persist_result_str(
                call["tool_name"], result, None,
                context_window_tokens=(
                    endpoint.context_window if endpoint else None
                ),
            )
            messages.append({
                "role": "tool",
                "tool_call_id": call["tool_call_id"],
                "content": _result_str,
            })
            # P0: guardrail after_call records outcome for loop detection
            guard_ctrl.after_call(call["tool_name"], call["tool_args"], _result_str)

        # P0: Layer 3 — apply per-turn aggregate budget to this batch's results
        _batch_ids = [c["tool_call_id"] for c in parsed_calls]
        _batch_names = [c["tool_name"] for c in parsed_calls]
        apply_turn_budget_to_batch(
            messages, _batch_ids, _batch_names, None,
            context_window_tokens=(
                endpoint.context_window if endpoint else None
            ),
        )

        # P0: if guardrail controller tripped a halt, inject nudge and break
        if guard_ctrl.halt_decision:
            _hd = guard_ctrl.halt_decision
            logger.warning(
                "Guardrail halt in sub-agent (delegate): %s (tool=%s, count=%d)",
                _hd.code, _hd.tool_name, _hd.count,
            )
            metrics.record_guardrail_halt(_hd.code)
            messages.append({
                "role": "user",
                "content": (
                    f"Tool '{_hd.tool_name}' is looping (count={_hd.count}). "
                    "Stop calling it and produce your final answer now."
                ),
            })
            break

    metrics.record_budget(conv_budget.used, conv_budget.max_total)

    # Exceeded max iterations, wall-clock budget, or guardrail halt — return
    # last content. Flag wall-clock truncation so the parent can re-plan.
    _wall_exceeded = (_time.monotonic() - _wall_start) > _wall_s
    return {
        "success": True,
        "task": task,
        "response": content or "Sub-agent completed but did not produce a final response.",
        "iterations": max_iterations,
        "truncated": _wall_exceeded,
        "note": "Reached max iterations or wall-clock budget",
    }


async def _call_sub_llm(messages: list[dict], tools: list[dict] | None) -> dict:
    """Call the LLM for a sub-agent conversation.

    .. deprecated::
        Retained for backward compatibility. New code should use
        :func:`app.services.sub_agent_reliability.call_llm_with_reliability`
        which adds prompt caching, error classification, and metrics.
    """
    return await call_llm_with_reliability(messages, tools)


# ---------------------------------------------------------------------------
# Schema & Registration
# ---------------------------------------------------------------------------

DELEGATE_TASK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delegate_task",
        "description": (
            "Delegate a task to a sub-agent with isolated context. "
            "The sub-agent runs with a restricted toolset (no delegation, no memory) "
            "and returns its final response. "
            "Use for complex subtasks that need focused attention, or when you need "
            "to parallelize independent tasks.\n\n"
            "Pass 'tasks' (array of strings) instead of 'task' to run multiple "
            "tasks concurrently and get all results at once."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The task description for the sub-agent to complete.",
                },
                "tasks": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Multiple tasks to run concurrently (batch mode). Alternative to 'task'.",
                },
                "agent_name": {
                    "type": "string",
                    "description": "Agent name for the sub-agent (default 'general_assistant')",
                    "default": "general_assistant",
                },
                "max_iterations": {
                    "type": "integer",
                    "description": "Max tool-calling iterations for the sub-agent (default 5)",
                    "default": 5,
                },
            },
            "required": [],
        },
    },
}

registry.register(
    name="delegate_task",
    schema=DELEGATE_TASK_SCHEMA,
    handler=_delegate_task,
    category="delegation",
    enabled_by_default=True,
    description="Delegate a task to a sub-agent with isolated context.",
)


# ── IBM MAS: LLM-queryable agent directory (task allocation) ────────────
# The LLM used to pick sub-agent names from a hardcoded prompt list. This
# tool exposes the real directory: builtin agent definitions + published
# user agents, with capabilities, so the LLM can route by specialization.

async def _list_available_agents(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    """List every agent available for delegation, with capabilities."""
    from app.services.agent_definitions import list_agent_definitions
    from app.models.agent_app import AgentApp

    agents = []
    for d in list_agent_definitions():
        agents.append({
            "name": d.name,
            "kind": "builtin",
            "description": (d.description or "")[:200],
            "tools": len(d.tools) if d.tools else None,  # None = all tools
            "source": d.source,
        })

    # P0 tenant-isolation fix (enterprise reliability milestone, 2026-11-01):
    # the discovery directory must never span organizations — published
    # agent names/descriptions are tenant data. Resolve the caller's org
    # (user → org_id, else the conversation's org) and scope the published
    # query to it. When the caller's org CANNOT be resolved (legacy test
    # harnesses pass user_id=None, context=None) the historical unscoped
    # behavior is kept with a loud warning — real tool invocations always
    # carry a user, so the wall holds on every production path. Discovery
    # never grants data access either way (children run under their own
    # fail-closed bindings).
    caller_org_id = None
    if user_id:
        try:
            from app.models.user import User

            _u = db.query(User).filter(User.id == user_id).first()
            if _u is not None:
                caller_org_id = getattr(_u, "org_id", None)
        except Exception:  # noqa: BLE001 — discovery must not crash on lookup issues
            caller_org_id = None
    if not caller_org_id and context and context.get("conversation_id"):
        try:
            _conv = (
                db.query(AgentConversation)
                .filter(AgentConversation.id == context["conversation_id"])
                .first()
            )
            if _conv is not None:
                caller_org_id = getattr(_conv, "org_id", None)
        except Exception:  # noqa: BLE001
            caller_org_id = None

    _pub_filters = [
        AgentApp.status == "published",
        AgentApp.is_deleted == False,  # noqa: E712
    ]
    if caller_org_id:
        _pub_filters.append(AgentApp.org_id == caller_org_id)
    else:
        logger.warning(
            "list_available_agents: caller org unresolvable (user_id=%r, "
            "no conversation org) — returning the UNSCOPED legacy directory; "
            "production callers must always carry a user",
            user_id,
        )
    published = (
        db.query(AgentApp)
        .filter(*_pub_filters)
        .order_by(AgentApp.name)
        .all()
    )

    # Batch-resolve department/home-project, KB domains and portfolio rows
    project_ids = {a.project_id for a in published if a.project_id}
    projects = {
        p.id: p
        for p in (
            db.query(Project).filter(Project.id.in_(project_ids)).all()
            if project_ids
            else []
        )
    }
    kb_map: dict[str, list[str]] = {}
    if project_ids:
        # Home membership OR explicit share link (multi-project sharing,
        # 2026-10-31). Linked KBs appear under every project that links them.
        from app.services.project_kb_membership import project_kb_link_subquery
        from sqlalchemy import or_ as _or

        for pid in project_ids:
            for kb in (
                db.query(KnowledgeBase)
                .filter(
                    _or(
                        KnowledgeBase.project_id == pid,
                        KnowledgeBase.id.in_(project_kb_link_subquery(pid)),
                    ),
                    KnowledgeBase.is_deleted == False,  # noqa: E712
                )
                .all()
            ):
                kb_map.setdefault(pid, []).append(kb.name)
    agent_ids = [a.id for a in published]
    portfolio = {
        row.agent_id: row
        for row in (
            db.query(AgentPortfolioEntry)
            .filter(AgentPortfolioEntry.agent_id.in_(agent_ids))
            .all()
            if agent_ids
            else []
        )
    }

    for a in published:
        proj = projects.get(a.project_id)
        department = (proj.department if proj else None) or None
        if dept_filter and (department or "").lower() != dept_filter.lower():
            continue
        capabilities = a.capabilities or []
        if cap_filter and not any(
            cap_filter in str(c).lower() for c in capabilities
        ) and cap_filter not in (a.description or "").lower():
            continue
        entry = {
            "name": a.name,
            "kind": "user_agent",
            "agent_id": a.id,
            "description": (a.description or "")[:200],
            "capabilities": capabilities,
            "department": department,
            "home_project": proj.name if proj else None,
            "data_domains": (kb_map.get(a.project_id) or [])[:5],
        }
        pf = portfolio.get(a.id)
        if pf is not None:
            entry["lifecycle_stage"] = getattr(pf, "lifecycle_stage", None)
            entry["business_owner"] = getattr(pf, "business_owner", None)
        agents.append(entry)

    return {
        "success": True,
        "department_filter": dept_filter,
        "agents": agents,
        "hint": (
            "Pick the agent whose CAPABILITY and DATA DOMAINS match the "
            "subtask. Pass the name as agent_name to delegate_task (batch "
            "tasks accept {task, agent_name} objects for multi-specialist "
            "fan-out). Discovery never grants data access — children run "
            "under their own authorized bindings."
        ),
    }


LIST_AVAILABLE_AGENTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_available_agents",
        "description": (
            "List every agent available for delegation (builtin specialist "
            "agents + published user agents) with their capabilities. Call "
            "this BEFORE delegate_task / swarm_spawn_agent when unsure which "
            "specialist fits the subtask."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

registry.register(
    name="list_available_agents",
    schema=LIST_AVAILABLE_AGENTS_SCHEMA,
    handler=_list_available_agents,
    category="delegation",
    enabled_by_default=True,
    description="Directory of agents available for delegation, with capabilities.",
)
