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
import time
import uuid
from datetime import datetime, timezone

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


async def _maybe_review_and_refine(
    task: str,
    agent_name: str,
    db: Session,
    user_id: str | None,
    endpoint: "LLMEndpoint | None",
    context: dict | None,
    result: dict,
) -> dict:
    """P2 (Part 1): optionally review a successful sub-agent result and, if the
    reviewer reports missing evidence, run a BOUNDED targeted re-investigation.

    Fact-safe by construction: the reviewer only ASSESSES provided material and
    never invents figures. The targeted follow-ups are real delegations (governed
    by the broker) to the SAME specialist with a narrow task derived from the
    reviewer's gap — they add real findings, they do not fabricate them.

    Selective: caller must have set ``context["_enable_review"]`` (via
    delegate_task review=True). Never runs for simple questions.
    """
    from app.services.agent_collaboration.reviewer import review_findings
    from app.services.agent_collaboration.shared_evidence import get_registry

    _conv = (context or {}).get("conversation_id")
    _items = []
    if _conv:
        try:
            _items = get_registry().list_items(_conv)
        except Exception:  # noqa: BLE001
            _items = []

    _report = review_findings(
        question=task, content=result.get("response") or "",
        evidence_items=_items, endpoint=endpoint,
    )
    result["review"] = _report.to_dict()

    _max = int(getattr(settings, "COLLAB_REVIEW_MAX_RETRIES", 1))
    if _report.needs_evidence and _max > 0:
        _extra: list = []
        # Bounded: at most 2 gaps, a single follow-up round. No review loops.
        for _gap in _report.needs_evidence[:2]:
            _narrow = (
                f"Targeted follow-up to the original task: {task[:200]}. "
                f"Investigate SPECIFICALLY this gap identified by review: {_gap}"
            )
            _ctx = dict(context or {})
            _ctx.pop("_enable_review", None)  # do not re-review the follow-up
            try:
                _sub = await _run_sub_agent(_narrow, agent_name, db, user_id, 3, endpoint, context=_ctx)
                if _sub.get("success"):
                    _extra.append(f"[follow-up: {_gap}] {_sub.get('response') or ''}")
                    if _sub.get("evidence_ids"):
                        result.setdefault("evidence_ids", []).extend(_sub["evidence_ids"])
            except Exception as _e:  # noqa: BLE001
                logger.debug("delegate: review follow-up failed (non-fatal): %s", _e)
        if _extra:
            result["response"] = (result.get("response") or "") + "\n\n" + "\n".join(_extra)
            # One re-review after gathering the targeted findings.
            _items2 = get_registry().list_items(_conv) if _conv else []
            _report2 = review_findings(
                question=task, content=result.get("response") or "",
                evidence_items=_items2, endpoint=endpoint,
            )
            result["review"] = _report2.to_dict()

    # P1.4 (Phase 1): attach the STRUCTURED replan decision so the parent LLM
    # observes an explicit verdict → action mapping (gather missing evidence /
    # revise / resolve contradictions) with the concrete gaps named verbatim.
    # Advisory only — never fabricates; the existing bounded targeted
    # re-investigation above remains the active mechanism. Flag-gated.
    if getattr(settings, "REVIEWER_REPLAN_GUIDANCE_ENABLED", True) and isinstance(result.get("review"), dict):
        try:
            from app.services.synexia.replan_policy import evaluate_reviewer_findings

            result["review_guidance"] = evaluate_reviewer_findings(result["review"])
        except Exception:  # noqa: BLE001 — guidance must never break delegation
            logger.debug("reviewer guidance evaluation failed", exc_info=True)
    # Phase 2.2/2.3: attach the EXECUTION DECISION — the structured
    # observation → action mapping the decision model computes from the
    # reviewer findings (11-value taxonomy, no chain-of-thought).
    try:
        from app.services.execution_decisions import decision_from_review

        result["execution_decision"] = decision_from_review(
            result.get("review") if isinstance(result.get("review"), dict) else None,
            specialist_available=True,  # the broker gates the actual delegation
            objective=task,
        ).to_dict()
    except Exception:  # noqa: BLE001 — decision attachment is advisory
        logger.debug("execution decision attachment failed", exc_info=True)
    return result


def _published_agent_candidates(db: Session | None) -> list[dict]:
    """Phase 2.4: published AgentApps as fallback-selection candidates.

    Light read-only directory scan (name + description) — the SAME authority
    gate as ``_resolve_agent_app`` / ``_list_available_agents``: only
    ``status == "published"`` rows. Best-effort; errors yield an empty list
    (the honest refusal), never a crash.
    """
    if db is None:
        return []
    try:
        from app.models.agent_app import AgentApp

        rows = (
            db.query(AgentApp)
            .filter(
                AgentApp.is_deleted == False,  # noqa: E712
                AgentApp.status == "published",
            )
            .all()
        )
        return [
            {
                "name": r.name,
                "description": (r.description or "")[:300] if hasattr(r, "description") else "",
                "published": True,
                "kind": "specialist_agent",
                "source": "agent_directory",
            }
            for r in rows
        ]
    except Exception as exc:  # noqa: BLE001
        logger.debug("published agent scan failed (non-fatal): %s", exc)
        return []


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

    # P2 (Part 1): selective fact-safe review. The caller opts in per-delegation
    # (review=True). We copy the context (never mutate the caller's dict) and flag
    # it so the child's result is reviewed after it returns. Simple questions do
    # not set this, so they are never reviewed.
    if bool(args.get("review")):
        context = dict(context or {})
        context["_enable_review"] = True

    # Check for batch mode (list of tasks). P1 (2026-10-31, roadmap M1):
    # items may be plain strings OR {task, agent_name, max_iterations}
    # objects — heterogeneous multi-specialist fan-out in ONE call
    # (Production ∥ Inventory ∥ Supply Chain). Missing agent_name on an
    # item falls back to the call-level agent_name.
    # BUGFIX (P1): the batch check must precede the empty-task rejection —
    # the schema documents `tasks` as an "alternative to task", but the
    # old order rejected every batch-only call with "task is required".
    tasks = args.get("tasks")
    if tasks and isinstance(tasks, list):
        _norm: list[dict] = []
        for _t in tasks:
            if isinstance(_t, dict):
                _norm.append({
                    "task": str(_t.get("task") or "").strip(),
                    "agent_name": (str(_t.get("agent_name") or "").strip() or agent_name),
                    "max_iterations": _t.get("max_iterations", max_iterations),
                })
            else:
                _norm.append({"task": str(_t), "agent_name": agent_name, "max_iterations": max_iterations})
        _norm = [t for t in _norm if t["task"]]
        if not _norm:
            return {"success": False, "error": "task is required"}
        return await _delegate_batch(_norm, db, user_id, endpoint, context)

    if not task:
        return {"success": False, "error": "task is required"}

    # Single task
    result = await _run_sub_agent(task, agent_name, db, user_id, max_iterations, endpoint, context=context)
    # Phase 1.5 (Gap 4): record the ACTUAL execution strategy on the result so
    # the parent LLM (and the recorded plan_meta) can be checked against what
    # really ran — a single delegation IS a single_specialist execution.
    if isinstance(result, dict):
        result.setdefault("actual_execution_strategy", "single_specialist")
    # Phase 2.4: on failure, discover AUTHORIZED alternative capabilities
    # (published-only, capability-matched, failed agent excluded). Advisory —
    # the parent LLM decides; destructive rerouting is refused upstream.
    if isinstance(result, dict) and not result.get("success"):
        try:
            # Phase 3.2: semantic matching first (local embeddings, auto
            # degrade to the Phase-2 lexical matcher when the model is
            # unavailable). Published-only + exclusions are enforced inside.
            from app.services.semantic_capability_match import (
                semantic_select_specialists,
            )

            result["alternative_capabilities"] = semantic_select_specialists(
                task,
                _published_agent_candidates(db),
                embedder="auto",
                exclude={agent_name},
            )
        except Exception:  # noqa: BLE001 — fallback discovery is advisory
            logger.debug("alternative capability discovery failed", exc_info=True)
            result.setdefault("alternative_capabilities", [])
    return result


async def _delegate_batch(
    tasks: list[dict],
    db: Session,
    user_id: str | None,
    endpoint: LLMEndpoint | None = None,
    context: dict | None = None,
) -> dict:
    """Run multiple sub-agent tasks concurrently.

    P1 (2026-10-31, roadmap M1): each item is ``{task, agent_name,
    max_iterations}`` — DIFFERENT specialists can fan out in one call
    (Production ∥ Inventory ∥ Supply Chain), each under its own AgentApp
    prompt/toolset and its own authorized bindings. Concurrency, wall-clock
    budgets, session isolation and invocation linking are unchanged.

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
    for i, t in enumerate(tasks):
        _emit(f"{t['agent_name']} #{i + 1}: {t['task'][:60]}", "running")

    # IBM MAS fix (2026-10-08): batch fan-out was unbounded — every sub-agent
    # launched at once, hammering the LLM provider and the DB pool. Bound it.
    import asyncio as _asyncio

    _max_conc = max(1, int(getattr(settings, "DELEGATE_MAX_CONCURRENT", 4)))
    _sem = _asyncio.Semaphore(_max_conc)

    async def _run_one(i: int, item: dict) -> dict:
        async with _sem:
            try:
                res = await _run_sub_agent(
                    item["task"], item["agent_name"], db, user_id,
                    item["max_iterations"], endpoint, context=context,
                )
            except Exception as exc:  # noqa: BLE001
                res = {"success": False, "error": str(exc), "task": item["task"]}
        # P1 contract: a failed child must carry status=failed even when the
        # exception path skipped the _run_sub_agent contract fields.
        if res.get("success"):
            res.setdefault("status", "partial" if res.get("truncated") else "completed")
        else:
            res["status"] = "failed"
        dur = int((_time.monotonic() - starts[i]) * 1000)
        _emit(
            f"{item['agent_name']} #{i + 1}: {item['task'][:60]}",
            "done" if res.get("success") else "failed",
            dur_ms=dur,
        )
        return res

    results = await asyncio.gather(*[_run_one(i, t) for i, t in enumerate(tasks)], return_exceptions=True)

    batch_results = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            batch_results.append({
                "task": tasks[i]["task"][:100],
                "agent_name": tasks[i]["agent_name"],
                "success": False,
                "status": "failed",
                "error": str(r),
            })
        else:
            item = {
                "task": tasks[i]["task"][:100],
                "agent_name": tasks[i]["agent_name"],
                **r,
            }
            # P1 contract: EVERY result carries a machine-readable status.
            # _run_sub_agent only sets it on the truncated path; the happy
            # path (max iterations reached → final text) returns no status,
            # so force one here — the UI evidence card and the parent LLM
            # both key off it.
            item.setdefault(
                "status",
                "failed" if not item.get("success")
                else ("partial" if item.get("truncated") else "completed"),
            )
            batch_results.append(item)

    # P1/P2 contract: batch-level summary the parent LLM cannot miss —
    # completed/partial/failed counts, plus per-agent attribution.
    # Honest partial-completion semantics (scenario F): a batch that produced
    # SOME successful results but also had failures is "partial" (you got real
    # findings from the survivors), not "failed". Only a batch with ZERO
    # successes is "failed". Truncation on a survivor still yields "partial".
    _succeeded = [b for b in batch_results if b.get("success")]
    _failed = [b for b in batch_results if not b.get("success")]
    _partial = [b for b in batch_results if b.get("success") and b.get("truncated")]
    return {
        "success": all(r.get("success", False) for r in batch_results),
        # Phase 1.5 (Gap 4): a batch fan-out IS a multi_agent execution —
        # recorded so plan_meta strategy claims stay checkable against reality.
        "actual_execution_strategy": "multi_agent",
        "status": (
            "failed" if not _succeeded
            else "partial" if (_failed or _partial)
            else "completed"
        ),
        "summary": (
            f"{len(batch_results) - len(_failed) - len(_partial)} completed, "
            f"{len(_partial)} partial, {len(_failed)} failed"
        ),
        "results": batch_results,
        "count": len(batch_results),
    }


def _compute_child_cost(token_usage: dict | None) -> float | None:
    """Estimate sub-agent cost ONLY from known pricing metadata.

    Returns None when the model is not in the known pricing table — we never
    invent a cost for an unknown model (Part 1.3: measured vs estimated). The
    fallback rate in cost_service is intentionally NOT used here so that an
    unpriced model stays explicitly unknown. Pricing infra is optional — if it
    cannot be imported, metering simply yields None (never breaks delegation).
    """
    if not token_usage:
        return None
    _model = (token_usage.get("model") or "").lower()
    _p = int(token_usage.get("prompt_tokens") or 0)
    _c = int(token_usage.get("completion_tokens") or 0)
    if not _model or (_p == 0 and _c == 0):
        return None
    try:
        from app.services.governance.cost_service import TOKEN_COSTS
        for key, rates in TOKEN_COSTS.items():
            if key in _model:
                return float((_p / 1000 * rates["prompt"]) + (_c / 1000 * rates["completion"]))
    except Exception:  # pragma: no cover - pricing infra optional
        return None
    return None


def _invocation_row_exists(db: Session, invocation_id: str) -> bool:
    """True iff the AgentInvocation start row actually persisted.

    Chat-experience repair: the finally-block fallback must only insert a
    terminal row when the start row is genuinely ABSENT — a refused terminal
    update on an existing row means first-terminal-wins, never a duplicate.
    """
    if not invocation_id:
        return False
    try:
        from app.models.agent_invocation import AgentInvocation

        row = (
            db.query(AgentInvocation.id)
            .filter(AgentInvocation.id == invocation_id)
            .first()
        )
        return row is not None
    except Exception:  # noqa: BLE001 — conservative: assume present
        return True


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
                parent_invocation_id=_child_context.get("parent_invocation_id"),
                trace_id=_child_context.get("trace_id"),
                delegation_mode="peer" if _requesting and _requesting != "general_assistant" else "supervisor",
                # Part 1.3: persist measured token usage + (where reliable
                # pricing exists) estimated cost. Unknown model => cost None.
                token_usage=_tu,
                cost_amount=_compute_child_cost(_tu),
            )
        except Exception as _inv_err:  # noqa: BLE001 — non-fatal
            logger.warning("delegate: invocation record failed (non-fatal): %s", _inv_err)
    # ── Multi-agent milestone: terminal live event for the specialist's
    # execution card (matches the finalized invocation row's state).
    try:
        from app.services.agent_loop.sse_builders import (
            append_agent_execution_event,
        )

        _term_ok = bool(result.get("success")) and result.get("status") not in (
            "failed", "partial",
        )
        append_agent_execution_event(
            (context or {}).get("progress_emitter"),
            "agent_completed" if _term_ok else "agent_failed",
            {
                "agent_execution_id": _agent_execution_id,
                "parent_agent_execution_id": _child_context.get("parent_invocation_id"),
                "agent_name": agent_name,
                "agent_label": (_agent_app.name if _agent_app else agent_name),
                "status": (
                    "completed" if _term_ok
                    else (result.get("status") or "failed")
                ),
                "duration_ms": max(
                    0, int((datetime.now(timezone.utc) - _started).total_seconds() * 1000)
                ),
                "delegation_mode": (
                    "peer" if _requesting and _requesting != "general_assistant"
                    else "supervisor"
                ),
                "evidence_ids": (result.get("evidence_ids") or [])[:20],
                "evidence_count": len(result.get("evidence_ids") or []),
                "error": (str(result.get("error"))[:300]
                          if result.get("error") and not _term_ok else None),
            },
        )
    except Exception:  # noqa: BLE001 — observability never breaks delegation
        pass
    return result


async def _run_sub_agent_inner(
    task: str,
    agent_name: str,
    db: Session,
    user_id: str | None,
    max_iterations: int,
    endpoint: LLMEndpoint | None = None,
    context: dict | None = None,
    agent_app=None,
    wall_seconds: float | None = None,
) -> dict:
    """Run a sub-agent conversation with restricted tools."""
    from app.services.agent_prompts import get_system_prompt, _get_all_crud_schemas

    # P0-1 (2026-10-31, audit breakpoint 2): when the caller resolved a
    # published AgentApp for this name, run the child with ITS OWN 5-layer
    # prompt instead of the generic fallback. get_system_prompt already
    # assembles the layered prompt from an AgentApp (agent_prompts.py).
    system_prompt = get_system_prompt(agent_name, agent_app=agent_app) or (
        "You are a helpful AI assistant. Complete the task concisely and accurately."
    )
    # Add delegation context — the child-side finding contract (mission §2.4).
    # Structured, grounded, honest: the supervisor synthesizes, so the child
    # must return material it can cite and limitations it can flag.
    system_prompt += (
        "\n\nYou are operating as a delegated specialist inside a "
        "multi-agent workflow.\n"
        "TOOLS: your specialization determines your PRIORITY tools, not a "
        "cap. If the task needs a tool that is not in your current list, "
        "call `search_tools` to discover it, then invoke it — every "
        "invocation is permission-checked, and a denied tool fails "
        "honestly.\n"
        "FINDING CONTRACT — return:\n"
        "1. A direct answer to the delegated question first.\n"
        "2. Grounding: cite the concrete source for every figure or claim "
        "(table/query/file/document section). Do not state numbers you did "
        "not retrieve this run.\n"
        "3. Limitations: explicitly report missing data, failed tool calls, "
        "or scope you could not cover — never paper over them.\n"
        "4. Keep it structured and concise; the supervisor will synthesize "
        "across specialists."
    )

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

    # Build available tools (exclude restricted tools).
    # ── Universal Tool Access — unified contract (milestone 2026-11-01) ──
    # POLICY, three separated layers (discovery ≠ availability ≠
    # authorization):
    #   1. Every eligible child may DISCOVER the shared tool registry.
    #   2. An AgentApp's ``enabled_tools`` are PREFERENCES (priority head
    #      of the tool list); ``disabled_tools`` are a hard operator deny;
    #      ``tool_config.strict_tools: true`` is the explicit opt-in that
    #      turns ``enabled_tools`` into an EXACT allowlist (no discovery).
    #   3. AVAILABILITY is bounded, not exhaustive: the child's prompt
    #      carries its priority head + the top-K task-relevant tools +
    #      ``search_tools`` (see ``_bound_child_toolset``) instead of the
    #      entire registry. AUTHORIZATION is never relaxed: schemas pass
    #      through the registry's RBAC filter (B1) and every invocation is
    #      permission-checked again inside ``execute_tool`` (org/project
    #      boundaries, per-tool RBAC, data-source policies, credential
    #      isolation).
    # Rollback: AGENT_UNIVERSAL_TOOL_ACCESS=false restores the legacy
    # contract (AgentApp exact allowlist / builtin enabled_by_default).
    _universal = getattr(settings, "AGENT_UNIVERSAL_TOOL_ACCESS", True)
    _nested_ok = (
        getattr(settings, "COLLAB_NESTED_DELEGATION_ENABLED", True)
        and getattr(settings, "COLLAB_POLICY_ENFORCED", True)
    )
    _restricted = set(_RESTRICTED_TOOLS)
    if _nested_ok:
        _restricted.discard("delegate_task")
    _crud_schemas: dict = {}
    _head_names: list = []
    _strict_tools = False
    if agent_app is not None:
        _tc = agent_app.tool_config or {}
        _disabled = set(_tc.get("disabled_tools") or [])
        _strict_tools = bool(_tc.get("strict_tools", False))
        _head_names = [
            t for t in (_tc.get("enabled_tools") or [])
            if t not in _restricted and t not in _disabled
            and registry.get_entry(t) is not None
        ]
        if _strict_tools or not _universal:
            # Explicit operator strict mode (or global rollback): exact
            # allowlist, no discovery beyond the configured tools.
            available_names = _head_names
        else:
            # Default contract: configured tools are the priority head;
            # the rest of the shared registry is discoverable (bounded
            # at injection time, see _bound_child_toolset below).
            _head_set = set(_head_names)
            available_names = _head_names + [
                t for t in registry.list_available()
                if t not in _restricted and t not in _disabled
                and t not in _head_set
            ]
    elif _universal:
        # Builtin child + universal access: the whole shared registry,
        # minus restricted tools (no role-based tool locking).
        available_names = [
            t for t in registry.list_available() if t not in _restricted
        ]
        _crud_schemas = _get_all_crud_schemas()
    else:
        # Legacy builtin child: only default-enabled tools.
        available_names = [
            t for t in registry.list_available()
            if t not in _restricted
            and (registry.get_entry(t) is not None and registry.get_entry(t).enabled_by_default)
        ]
        _crud_schemas = _get_all_crud_schemas()
    # Availability honesty (unified contract layer 2): strip tools the
    # current role cannot invoke — the SAME RBAC availability filter
    # registry.get_schemas applies (B1) — so the child is never advertised
    # a tool it cannot use. Non-fatal: execute_tool re-checks permission
    # at invocation time regardless.
    try:
        from app.services.authz import get_authorizer
        from app.services.authz.base import ResourceType as _RT
        from app.services.tracing import TraceContext as _TC

        available_names = get_authorizer().filter_resources(
            role=(_TC.current_role() or "user"),
            resource_type=_RT.TOOL,
            resource_ids=list(available_names),
        )
    except Exception as _exc:  # noqa: BLE001
        logger.debug("delegate_tool: RBAC availability filter skipped (non-fatal): %s", _exc)
    tool_schemas = []
    _added_names: set = set()
    for name in available_names:
        entry = registry.get_entry(name)
        if entry is None:
            continue
        if agent_app is not None or entry.enabled_by_default or _universal:
            tool_schemas.append(_normalize_subagent_schema(name, entry.schema, entry.description or ""))
            _added_names.add(name)

    # Also include CRUD tool schemas if the sub-agent needs them
    # (but exclude delegate_task which is not in CRUD anyway)
    for name, schema in _crud_schemas.items():
        if name not in _restricted and schema not in tool_schemas:
            tool_schemas.append(schema)
            _added_names.add(name)

    # P2 (Part 3): shared-evidence tools are ALWAYS available to sub-agents — they
    # are how specialists exchange findings by reference (no dataset copies). The
    # supervisor aggregates via the registry directly and does not need them.
    for _ev_name in ("provide_evidence", "request_evidence"):
        if _ev_name in _added_names:
            continue
        # Unified contract: an explicit operator deny (disabled_tools)
        # wins even over the always-on evidence contract.
        if agent_app is not None and _ev_name in _disabled:
            continue
        _ev = registry.get_entry(_ev_name)
        if _ev is not None:
            tool_schemas.append(_normalize_subagent_schema(_ev_name, _ev.schema, _ev.description or ""))
            _added_names.add(_ev_name)

    # Dynamic tool discovery (milestone §1.3): bound the injected schema
    # list — priority head + always-on evidence tools kept, remainder
    # ranked by task relevance, ``search_tools`` escape hatch appended.
    # Skipped for strict operator allowlists (exact contract) and the
    # legacy rollback path. The child can still INVOKE any tool it
    # discovers via search_tools: the tool loop executes by name through
    # the permission-checked execute_tool layer.
    if not (_strict_tools or not _universal):
        _protected = set(_head_names) | {"provide_evidence", "request_evidence"}
        if _crud_schemas:
            # Dynamic entity-CRUD schemas are the builtin child's
            # record-operation contract — never dropped by bounding.
            _protected |= set(_crud_schemas.keys())
        tool_schemas = _bound_child_toolset(task, tool_schemas, protected=_protected)

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
    # Part 1.2: `_run_sub_agent` passes the effective deadline (already chosen
    # from COLLAB_TIMEOUT_SECONDS / DELEGATE_WALL_SECONDS) so the inner loop and
    # the real asyncio.wait_for cancellation share ONE deadline. Fall back to the
    # settings-derived value when called directly (tests / other callers).
    _wall_s = wall_seconds
    if _wall_s is None:
        _wall_s = float(getattr(settings, "DELEGATE_WALL_SECONDS", 600))
        if getattr(settings, "COLLAB_POLICY_ENFORCED", True):
            _wall_s = float(getattr(settings, "COLLAB_TIMEOUT_SECONDS", _wall_s))

    # Part 1.3: accumulate provider token usage across the sub-agent's LLM calls
    # so cost can be metered per child (and against the parent task budget).
    _usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    _models: list = []

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
        # Part 1.3: accumulate provider token usage (never invented — straight
        # from the provider response surfaced by call_llm_with_reliability).
        _u = llm_response.get("usage") or {}
        _usage["prompt_tokens"] += int(_u.get("prompt_tokens") or 0)
        _usage["completion_tokens"] += int(_u.get("completion_tokens") or 0)
        _usage["total_tokens"] += int(_u.get("total_tokens") or 0)
        if llm_response.get("model"):
            _models.append(llm_response["model"])

        if not raw_tool_calls:
            # Final text response
            metrics.record_budget(conv_budget.used, conv_budget.max_total)
            # P1/P2: a single LLM call may itself exceed the wall-clock budget.
            # Don't silently report it complete — break so the unified post-loop
            # return marks it truncated/partial (the parent must never treat a
            # cut-off child as whole).
            if _time.monotonic() - _wall_start > _wall_s:
                break
            return {
                "success": True,
                "task": task,
                "response": content,
                "iterations": iteration + 1,
                "token_usage": {**_usage, "model": _models[-1] if _models else None},
                "_agent_app_id": agent_app.id if agent_app is not None else None,
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

        # ── Multi-agent milestone: per-tool live events under THIS child's
        # execution identity, so the Agent Execution panel renders each
        # specialist's own tool loop in real time (tools under the agent
        # that invoked them — never counted as agents).
        _child_exec_id = _child_ctx.get("_agent_execution_id")

        def _tool_live(event_type: str, tool_name: str, *,
                       status: str = "running", duration_ms: int | None = None,
                       rows: int = 0, error: str | None = None) -> None:
            if not _child_exec_id:
                return
            try:
                from app.services.agent_loop.sse_builders import (
                    append_agent_execution_event,
                )

                _p = {
                    "agent_execution_id": _child_exec_id,
                    "parent_agent_execution_id": _child_ctx.get("parent_invocation_id"),
                    "agent_name": agent_name,
                    "tool_name": tool_name,
                    "status": status,
                    "row_count": rows,
                }
                if duration_ms is not None:
                    _p["duration_ms"] = duration_ms
                if error:
                    _p["error"] = str(error)[:200]
                append_agent_execution_event(
                    _child_ctx.get("progress_emitter"), event_type, _p)
            except Exception:  # noqa: BLE001 — observability only
                pass

        if len(parsed_calls) == 1:
            call = parsed_calls[0]
            _gd = guard_ctrl.before_call(call["tool_name"], call["tool_args"])
            if _gd.allows_execution:
                _tt0 = time.monotonic()
                _tool_live("agent_tool_started", call["tool_name"])
                _r1 = await execute_tool(
                    call["tool_name"],
                    call["tool_args"],
                    db, user_id,
                    context=_child_ctx,
                )
                _tool_live(
                    "agent_tool_completed", call["tool_name"],
                    status=(
                        "completed" if (isinstance(_r1, dict) and _r1.get("success"))
                        else "failed"
                    ),
                    duration_ms=int((time.monotonic() - _tt0) * 1000),
                    rows=(len(_r1.get("rows") or []) if isinstance(_r1, dict) else 0),
                    error=(None if (isinstance(_r1, dict) and _r1.get("success"))
                           else (_r1 or {}).get("error")),
                )
                results = [_r1]
            else:
                results = [json.loads(synthetic_blocked_result(_gd))]
        else:
            async def _exec_one(call):
                from app.database import tool_session
                with tool_session() as _s:
                    _gd = guard_ctrl.before_call(call["tool_name"], call["tool_args"])
                    if _gd.allows_execution:
                        _tt0 = time.monotonic()
                        _tool_live("agent_tool_started", call["tool_name"])
                        try:
                            _r = await execute_tool(
                                call["tool_name"], call["tool_args"], _s, user_id,
                                context=_child_ctx,
                            )
                        except Exception as _te:
                            _tool_live(
                                "agent_tool_completed", call["tool_name"],
                                status="failed",
                                duration_ms=int((time.monotonic() - _tt0) * 1000),
                                error=str(_te))
                            raise
                        _tool_live(
                            "agent_tool_completed", call["tool_name"],
                            status=(
                                "completed" if (isinstance(_r, dict) and _r.get("success"))
                                else "failed"
                            ),
                            duration_ms=int((time.monotonic() - _tt0) * 1000),
                            rows=(len(_r.get("rows") or []) if isinstance(_r, dict) else 0),
                            error=(None if (isinstance(_r, dict) and _r.get("success"))
                                   else (_r or {}).get("error")),
                        )
                        return _r
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
    # P1 child-result contract (2026-10-31, roadmap Q3): the parent must see
    # partial/failed states EXPLICITLY — never silently consume a truncated
    # child result as complete. status is machine-readable; the response
    # prefix makes it unavoidable in the parent's context window.
    _status = "partial" if _wall_exceeded else "completed"
    _response = content or "Sub-agent completed but did not produce a final response."
    if _wall_exceeded:
        _response = (
            "[PARTIAL RESULT — sub-agent hit its wall-clock budget before "
            "finishing; figures below may be incomplete. Re-delegate with a "
            "narrower task if completeness matters.]\n\n" + _response
        )
    # Part 1.3: record cumulative token consumption against the per-conversation
    # budget store (best-effort; only when a conversation id is known so that
    # ephemeral/dedup-isolated calls stay isolated).
    if context and context.get("conversation_id") and _usage["total_tokens"]:
        try:
            from app.services.agent_collaboration.governance_store import get_store
            get_store().conv_token_add(
                context["conversation_id"], _usage["total_tokens"],
            )
        except Exception as _tok_err:  # noqa: BLE001 — non-fatal metering
            logger.warning("delegate: token metering update failed: %s", _tok_err)

    # P2 (Part 3): surface the evidence THIS child produced (the provide_evidence
    # tool already stored the items in the shared registry). The parent /
    # orchestrator references them by id instead of re-querying. We only list
    # items produced by this agent during this run (created_at >= _wall_start).
    _ev_ids: list = []
    _conv = (context or {}).get("conversation_id")
    if _conv:
        try:
            from app.services.agent_collaboration.shared_evidence import get_registry
            for _it in get_registry().list_items(_conv):
                if _it.producer_agent == agent_name and _it.created_at >= (_wall_start - 0.001):
                    _ev_ids.append(_it.evidence_id)
        except Exception as _e:  # noqa: BLE001
            logger.debug("delegate: evidence id collection failed (non-fatal): %s", _e)

    return {
        "success": True,
        "status": _status,
        "task": task,
        "agent_name": agent_name,
        "response": _response,
        "iterations": max_iterations,
        "truncated": _wall_exceeded,
        "note": "Reached max iterations or wall-clock budget" if _wall_exceeded else None,
        "evidence_ids": _ev_ids,
        "token_usage": {
            **_usage,
            "model": _models[-1] if _models else None,
        },
        "_agent_app_id": agent_app.id if agent_app is not None else None,
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
            "The sub-agent gets its own toolset (its specialization's tools "
            "first, plus dynamic discovery of the shared registry — every "
            "invocation stays permission-checked) and returns a structured, "
            "grounded response with stated limitations. "
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
                    "items": {
                        "anyOf": [
                            {"type": "string"},
                            {
                                "type": "object",
                                "properties": {
                                    "task": {"type": "string", "description": "The subtask description."},
                                    "agent_name": {"type": "string", "description": "Specialist agent for THIS task (from list_available_agents)."},
                                    "max_iterations": {"type": "integer"},
                                },
                                "required": ["task"],
                            },
                        ]
                    },
                    "description": "Multiple tasks to run concurrently (batch mode). Items may be plain strings (all use agent_name) or {task, agent_name} objects for multi-specialist fan-out. Alternative to 'task'.",
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
                "review": {
                    "type": "boolean",
                    "description": (
                        "P2 (Part 1) optional fact-safe REVIEW of the sub-agent output "
                        "(numerical accuracy, evidence completeness, source attribution, "
                        "contradictory findings, unsupported conclusions, assumptions/"
                        "uncertainty, whether the question was answered). SELECTIVE — use "
                        "only for complex or high-risk tasks. If the reviewer finds missing "
                        "evidence it may trigger a bounded targeted re-investigation."
                    ),
                    "default": False,
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

# P2 (Part 3): register the shared-evidence tools so they are available to the
# sub-agent (child) toolset. They are opt-in there (enabled_by_default=False on
# the registry entry) and explicitly added to children in _run_sub_agent_inner.
try:
    from app.services.agent_collaboration.evidence_tools import register_evidence_tools

    register_evidence_tools()
except Exception as _ev_reg_err:  # pragma: no cover - registration is best-effort
    logger.debug("delegate: evidence tools registration skipped: %s", _ev_reg_err)


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
    """List every agent available for delegation, with capabilities.

    P1 (2026-10-31): capability-based routing (docs/architecture/
    zhanlu-enterprise-agent-discovery-and-routing.md §4.2). Published
    AgentApp entries are enriched with department / home project / bound
    data domains / portfolio lifecycle so the parent LLM can route by
    CAPABILITY + DATA AVAILABILITY, not by guessing names. DISCOVERY ONLY —
    this tool never grants data access; delegated children still run under
    their own bindings and the fail-closed permission checker.
    """
    from app.services.agent_definitions import list_agent_definitions
    from app.models.agent_app import AgentApp
    from app.models.project import Project
    from app.models.knowledge_base import KnowledgeBase
    from app.models.agent_portfolio import AgentPortfolioEntry
    from app.models.agent_conversation import AgentConversation

    dept_filter = (args.get("department") or "").strip() or None
    cap_filter = (args.get("capability") or "").strip().lower() or None

    # Default department: the conversation's selected department (chat UI
    # department chip → conversation metadata.department). Explicit arg wins.
    if dept_filter is None and context and context.get("conversation_id"):
        try:
            conv = (
                db.query(AgentConversation)
                .filter(AgentConversation.id == context["conversation_id"])
                .first()
            )
            if conv is not None:
                dept_filter = ((conv.metadata_ or {}).get("department") or "").strip() or None
        except Exception:  # noqa: BLE001 — metadata lookup is best-effort
            pass

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
            "agents + published user agents) with capabilities, department, "
            "home project, bound data domains and lifecycle stage. Call "
            "this BEFORE delegate_task when a request needs specialist "
            "capabilities; filter by department when the user selected one."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "department": {
                    "type": "string",
                    "description": "Optional department filter (defaults to the conversation's selected department).",
                },
                "capability": {
                    "type": "string",
                    "description": "Optional capability keyword filter (e.g. 'forecast', 'quality', 'inventory').",
                },
            },
        },
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
