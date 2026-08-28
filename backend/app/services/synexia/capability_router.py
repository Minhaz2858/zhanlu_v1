"""Capability router — dispatches plan nodes to the right executor.

Each plan node type maps to a specific executor:
- "tool" → existing execute_tool function
- "skill" → skills registry
- "nl2sql" → DataSnapshot service
- "sandbox" → Sandbox service (creates a sandbox job)
- "agent" → sub-agent delegation

The router records ObservationRecords for each execution.
"""

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from sqlalchemy.orm import Session

from app.config import settings
from app.models.execution import Execution, Plan, PlanNode, ObservationRecord
from app.services.synexia.policy_evaluator import evaluate_node

logger = logging.getLogger(__name__)


def execute_plan_nodes(
    db: Session,
    execution: Execution,
    plan: Plan,
    user_id: Optional[str] = None,
    *,
    replan_depth: int = 0,
    max_replan_depth: Optional[int] = None,
    data_ctx_extras: Optional[dict] = None,
    on_plan_node=None,
    adaptive_revisions: int = 0,
    endpoint=None,
) -> list[ObservationRecord]:
    """Execute all nodes in a plan in dependency order.

    Records an ObservationRecord for each node execution.

    Dynamic re-planning: after the initial pass, failed tool/skill nodes
    trigger a corrective sub-plan (via ``plan_dag.generate_plan`` with a
    failure context) which is executed recursively up to ``max_replan_depth``
    (default ``settings.SYNEXIA_ACT_REPLAN_MAX``). The data pipeline
    (nl2sql/synthesize/sandbox) is never re-planned here.

    ``on_plan_node``: optional callback ``(node_dict, status, detail=None)``
    invoked at each node lifecycle transition (``running`` / ``completed`` /
    ``failed`` / ``skipped`` / ``denied`` / ``replanning``). Used by the
    SynexiaFSM SSE path to surface per-node ``activity_step`` events so a
    watcher sees the plan executing step-by-step (and self-correcting) —
    the Manus-style activity feed. Exceptions in the callback are swallowed
    (observability must never break execution). ``node_dict`` carries
    ``seq``, ``name``, ``node_type``; ``detail`` carries an error/notice.

    ``endpoint``: optional hierarchical LLMEndpoint forwarded to every LLM
    call made while executing the plan (nl2sql narration, synthesize,
    adaptive planner, corrective re-plan) so the whole run answers with the
    pinned model.
    """
    observations = []

    if not plan or not plan.nodes:
        logger.info("No plan nodes to execute")
        return observations

    def _emit(node, status: str, detail: Optional[str] = None) -> None:
        if on_plan_node is None:
            return
        try:
            on_plan_node(
                {
                    "seq": getattr(node, "seq", None),
                    "name": getattr(node, "name", None),
                    "node_type": getattr(node, "node_type", None),
                },
                status,
                detail,
            )
        except Exception as _cb_err:  # noqa: BLE001 — observability is best-effort
            logger.debug("on_plan_node callback raised (non-fatal): %s", _cb_err)

    # Sort nodes by dependency (topological order)
    ordered_nodes = _topological_sort(plan.nodes)

    # ── Phase 3c: adaptive re-planning setup (opt-in, default off) ──────
    _adaptive_enabled = getattr(settings, "SYNEXIA_ADAPTIVE_PLANNING_ENABLED", False)
    _adaptive_max = getattr(settings, "SYNEXIA_ADAPTIVE_MAX_REVISIONS", 2)
    _adaptive_revisions = adaptive_revisions  # track across recursive calls

    def _adaptive_call_llm_fn(system_prompt, messages):
        # Sync wrapper around the async _synth_call_llm (run via the bridge)
        # so decide_adaptive_revision gets a plain {"content": ...} dict.
        return _run_coro_sync(_synth_call_llm(system_prompt, messages, endpoint=endpoint))

    for _idx, node in enumerate(ordered_nodes):
        # Check if dependencies are met
        if not _dependencies_met(node, plan.nodes):
            logger.warning("Skipping node %s — dependencies not met", node.name)
            node.status = "skipped"
            db.commit()
            _emit(node, "skipped", "dependencies not met")
            continue

        # Per-node policy check
        node_decision = evaluate_node(node, execution.policy_decision or {})
        if node_decision["decision"] == "deny":
            logger.warning("Node %s denied by policy: %s", node.name, node_decision["reason"])
            node.status = "failed"
            node.error = f"Policy denied: {node_decision['reason']}"
            db.commit()
            _emit(node, "denied", node_decision["reason"])
            continue

        # Execute the node
        node.status = "running"
        node.started_at = datetime.now(timezone.utc)
        db.commit()
        _emit(node, "running")

        try:
            obs = _execute_single_node(
                db, execution, node, user_id,
                data_ctx_extras=data_ctx_extras, endpoint=endpoint,
            )
            observations.append(obs)

            node.status = "completed" if obs.success else "failed"
            node.result = obs.result_data
            node.error = obs.error_message
            node.completed_at = datetime.now(timezone.utc)
            _emit(
                node,
                "completed" if obs.success else "failed",
                (obs.error_message if not obs.success else None),
            )

        except Exception as e:
            logger.error("Node %s execution failed: %s", node.name, e)
            node.status = "failed"
            node.error = str(e)
            node.completed_at = datetime.now(timezone.utc)

            # Record error observation
            obs = ObservationRecord(
                id=str(uuid4()),
                execution_id=execution.id,
                plan_node_id=node.id,
                seq=len(observations),
                observation_type="error",
                tool_name=node.name,
                success=False,
                error_message=str(e),
            )
            db.add(obs)
            observations.append(obs)
            _emit(node, "failed", str(e))

        db.commit()

        # ── Phase 3c: adaptive checkpoint (opt-in, default off) ──────────
        # After a successful checkpoint node, ask the adaptive planner
        # whether to proceed / insert / modify / complete_early. Fail-safe:
        # any error → proceed (run continues on the original plan).
        if (
            _adaptive_enabled
            and node.node_type in ("nl2sql", "synthesize", "sandbox")
            and node.status == "completed"
            and _adaptive_revisions < _adaptive_max
        ):
            _remaining = [
                {"node_type": n.node_type, "name": n.name,
                 "dependencies": n.dependencies or []}
                for n in ordered_nodes[_idx + 1:]
                if n.status == "pending"
            ]
            if _remaining:
                try:
                    from app.services.synexia.adaptive_planner import decide_adaptive_revision
                    _decision = decide_adaptive_revision(
                        user_message=execution.user_message or "",
                        task_spec=execution.task_spec or {},
                        observations=list(observations),
                        remaining_nodes=_remaining,
                        call_llm_fn=_adaptive_call_llm_fn,
                    )
                except Exception as _ae:
                    logger.warning("Adaptive planner raised (proceed): %s", _ae)
                    _decision = None
                if _decision is not None:
                    _adaptive_revisions += 1
                    if _decision.action == "complete_early":
                        logger.info("Adaptive: complete_early at node %s", node.name)
                        break
                    if _decision.action in ("insert_nodes", "modify_remaining") and _decision.nodes:
                        _tail = _persist_adaptive_tail(db, execution, _decision.nodes)
                        if _tail is not None:
                            observations.extend(execute_plan_nodes(
                                db, execution, _tail, user_id,
                                replan_depth=replan_depth + 1,
                                max_replan_depth=max_replan_depth,
                                data_ctx_extras=data_ctx_extras,
                                on_plan_node=on_plan_node,
                                adaptive_revisions=_adaptive_revisions,
                                endpoint=endpoint,
                            ))
                            break  # adaptive tail executed; stop original loop

    # ── Dynamic re-planning: recover failed tool/skill nodes ──────────
    # When tool/skill nodes fail (and the re-plan budget allows), generate a
    # corrective sub-plan from the failure context and execute it. The data
    # pipeline (nl2sql / synthesize / sandbox) is intentionally NOT re-planned
    # here — only general tool/skill steps. Policy-denied failures are excluded
    # (a corrective plan would just re-deny them).
    _max = max_replan_depth
    if _max is None:
        _max = getattr(settings, "SYNEXIA_ACT_REPLAN_MAX", 2)
    if replan_depth < _max:
        recoverable = _recoverable_failures(plan)
        if recoverable:
            logger.info(
                "ACT re-plan (depth %d): %d failed tool/skill node(s) — generating corrective plan",
                replan_depth + 1, len(recoverable),
            )
            # Surface the self-heal: one activity step per failed node being
            # recovered, so a watcher sees the agent re-planning (Manus feel).
            for _fail in recoverable:
                _emit(
                    type("_N", (), {"seq": None, "name": _fail.get("name"), "node_type": _fail.get("node_type")})(),
                    "replanning",
                    _fail.get("error"),
                )
            try:
                from app.services.synexia.plan_dag import generate_plan
                corrective = generate_plan(
                    db=db,
                    execution_id=execution.id,
                    task_spec=execution.task_spec or {},
                    context_manifest=execution.context_manifest or {},
                    agent_name=execution.agent_name or "general_assistant",
                    failure_context=recoverable,
                    plan_version=replan_depth + 2,
                    endpoint=endpoint,
                )
                observations.extend(
                    execute_plan_nodes(
                        db, execution, corrective, user_id,
                        replan_depth=replan_depth + 1, max_replan_depth=_max,
                        data_ctx_extras=data_ctx_extras,
                        on_plan_node=on_plan_node,
                        endpoint=endpoint,
                    )
                )
            except Exception as e:
                logger.warning("ACT re-plan failed (non-fatal): %s", e)

    return observations


def _recoverable_failures(plan: Optional[Plan]) -> list[dict]:
    """Collect failed tool/skill nodes worth a corrective re-plan.

    Excludes the data pipeline (nl2sql / synthesize / sandbox) by design, and
    excludes policy-denied failures (re-planning would just re-deny them).
    """
    if not plan or not plan.nodes:
        return []
    out: list[dict] = []
    for node in plan.nodes:
        if node.status != "failed":
            continue
        if node.node_type not in ("tool", "skill"):
            continue
        err = node.error or ""
        if err.startswith("Policy denied"):
            continue
        out.append({
            "name": node.name,
            "node_type": node.node_type,
            "error": err or "unknown error",
        })
    return out


def _persist_adaptive_tail(db, execution, steps) -> Optional[Plan]:
    """Persist an adaptive-planner decision's step dicts as a new Plan + nodes.

    Returns the Plan, or None on any failure (caller proceeds on original plan).
    """
    try:
        from sqlalchemy import func
        max_v = (
            db.query(func.max(Plan.version))
            .filter(Plan.execution_id == execution.id)
            .scalar()
        ) or 0
        plan = Plan(
            id=str(uuid4()),
            execution_id=execution.id,
            version=max_v + 1,
            status="draft",
            summary="adaptive re-plan",
            is_acyclic=True,
        )
        db.add(plan)
        db.flush()
        for i, step in enumerate(steps):
            db.add(PlanNode(
                id=str(uuid4()),
                plan_id=plan.id,
                seq=i,
                node_type=step.get("node_type", "tool"),
                name=step.get("name", f"Step {i+1}"),
                description=step.get("description", ""),
                dependencies=step.get("dependencies", []) or [],
                inputs=step.get("inputs") or {},
                expected_output=step.get("expected_output"),
                output_artifact_type=step.get("output_artifact_type"),
                status="pending",
            ))
        db.commit()
        db.refresh(plan)
        return plan
    except Exception as e:
        logger.warning("Adaptive tail persistence failed (proceed): %s", e)
        return None


def _execute_single_node(
    db: Session,
    execution: Execution,
    node: PlanNode,
    user_id: Optional[str],
    data_ctx_extras: Optional[dict] = None,
    endpoint=None,
) -> ObservationRecord:
    """Execute a single plan node and record an observation.

    ``data_ctx_extras`` is forwarded to tool nodes so project-scoped
    KBs (and any other data-source runtime fields) reach handlers like
    ``ask_data_agent``. Non-tool nodes ignore it. ``endpoint`` (hierarchical
    LLMEndpoint) is forwarded to LLM-consuming nodes (nl2sql, synthesize).
    """
    node_type = node.node_type

    if node_type == "tool":
        return _execute_tool_node(db, execution, node, user_id, data_ctx_extras=data_ctx_extras)
    elif node_type == "nl2sql":
        return _execute_nl2sql_node(db, execution, node, data_ctx_extras=data_ctx_extras, endpoint=endpoint)
    elif node_type == "synthesize":
        return _execute_synthesize_node(db, execution, node, endpoint=endpoint)
    elif node_type == "sandbox":
        return _execute_sandbox_node(db, execution, node)
    elif node_type == "skill":
        return _execute_skill_node(db, execution, node, user_id, data_ctx_extras=data_ctx_extras)
    else:
        # Default: treat as tool
        return _execute_tool_node(db, execution, node, user_id, data_ctx_extras=data_ctx_extras)


def _run_coro_sync(coro):
    """Run an async coroutine from this sync context.

    The FSM normally runs inside ``asyncio.to_thread`` (or a sync FastAPI
    route's threadpool) — no running event loop here, so ``asyncio.run``
    is safe. If a future caller runs the sync FSM directly inside an event
    loop thread, fall back to a fresh thread with its own loop.
    """
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _execute_tool_node(db, execution, node, user_id, data_ctx_extras=None) -> ObservationRecord:
    """Execute a tool node via execute_tool_with_retry (async → bridged).

    Previously called the async ``execute_tool`` WITHOUT await — the
    coroutine was never executed and every tool node recorded a fabricated
    success with ``str(coroutine)`` as its result. Now bridged properly
    through ``_run_coro_sync``, and routed through the result-level retry
    wrapper so failure dicts get the Phase B self-heal treatment.

    ``data_ctx_extras`` (the dict returned by ``prepare_data_source_runtime``
    — primarily ``bound_kb_ids``) is merged into the tool context so
    project-scoped KBs propagate to ``ask_data_agent`` / ``list_data_sources``
    / ``execute_query`` inside the FSM. Without this, the FSM path drops
    the data-source binding and the agent reports "no data sources bound"
    even when the user has a connected database in their project.
    """
    from app.services.agent_tools import execute_tool_with_retry

    # When node.inputs carries a 'tool_name' key, use it as the dispatched
    # tool name instead of node.name (which is a human-readable label).
    # This allows plan nodes to have descriptive names while still routing
    # to the correct tool handler (e.g. node.name="Edit pptx artifact" with
    # inputs.tool_name="edit_artifact").
    args = dict(node.inputs or {})
    tool_name = args.pop("tool_name", None) or node.name

    # Build the tool context. Always include the FSM-friendly identifiers
    # so handlers can route to the right conversation/agent; then layer
    # the data-source runtime on top so KB scoping reaches ask_data_agent.
    tool_context = {
        "conversation_id": getattr(execution, "conversation_id", None),
        # Execution identity lets the artifact path ground a deck in the REAL
        # query rows this execution fetched (see deck_data.collect_grounded_rows).
        "execution_id": getattr(execution, "id", None),
        "agent_app_id": getattr(execution, "app_id", None) or getattr(execution, "agent_name", None),
        "agent_name": getattr(execution, "agent_name", None),
        **(data_ctx_extras or {}),
    }

    try:
        result = _run_coro_sync(
            execute_tool_with_retry(tool_name, args, db, user_id, context=tool_context)
        )
        result_data = result if isinstance(result, dict) else {"result": str(result)}
        result_text = result_data.get("response", str(result_data))
        ok = bool(result_data.get("success", True))

        return _record_observation(
            db, execution, node,
            observation_type="tool_call",
            tool_name=tool_name,
            request_args=args,
            result_data=result_data,
            result_text=result_text,
            success=ok,
            error_message=None if ok else str(result_data.get("error", "tool failed")),
        )
    except Exception as e:
        return _record_observation(
            db, execution, node,
            observation_type="tool_call",
            tool_name=tool_name,
            request_args=args,
            success=False,
            error_message=str(e),
        )


# ---------------------------------------------------------------------------
# Market-intent KB grounding (Task B2)
# ---------------------------------------------------------------------------
# The C5_C9 project binds two KBs: the ERP warehouse and the Market Research
# KB copy (name contains 'Market'). When the user intent mentions market /
# industry keywords, the data query must ground on the Market Research KB
# instead of blindly taking the first bound KB. Deterministic keyword
# matching only — no LLM involved.
_MARKET_INTENT_KEYWORDS = ("market", "industry", "research", "市场", "行业", "行情")


def _contains_market_keyword(text) -> bool:
    """True when ``text`` mentions any market/industry keyword (case-insensitive)."""
    if not text:
        return False
    lowered = str(text).lower()
    return any(kw in lowered for kw in _MARKET_INTENT_KEYWORDS)


def _kb_is_market(kb_meta: dict) -> bool:
    """True when a KB's name/description marks it as the Market Research source."""
    if not kb_meta:
        return False
    haystack = " ".join(
        [
            str(kb_meta.get("name") or ""),
            str(kb_meta.get("description") or ""),
        ]
    ).lower()
    return any(kw in haystack for kw in _MARKET_INTENT_KEYWORDS)


def _select_grounding_kb(
    bound_kb_ids: list[str],
    kb_meta_map: dict,
    user_message,
) -> Optional[str]:
    """Pick the primary KB for a data query from the bound set.

    Market-intent preference: when ``user_message`` mentions market/industry
    keywords (market / industry / research / 市场 / 行业 / 行情), prefer the
    bound KB whose name/description marks it as the Market Research source
    (``kb_meta_map``: kb_id -> {"name": ..., "description": ...}). Otherwise
    keep the existing behavior — the first bound KB. Pure and deterministic:
    no LLM, no randomness. ``None``/empty messages are handled safely.
    """
    if not bound_kb_ids:
        return None
    if _contains_market_keyword(user_message):
        for kb_id in bound_kb_ids:
            if _kb_is_market((kb_meta_map or {}).get(kb_id)):
                return kb_id
    return bound_kb_ids[0]


def _load_bound_kb_meta(db, bound_kb_ids: list[str]) -> dict:
    """Load ``kb_id -> {"name", "description"}`` for the bound KBs.

    Best-effort: returns {} on any DB failure so callers fall back to the
    existing first-bound behavior instead of raising.
    """
    if not bound_kb_ids:
        return {}
    try:
        from app.models.knowledge_base import KnowledgeBase

        rows = (
            db.query(KnowledgeBase)
            .filter(
                KnowledgeBase.id.in_(bound_kb_ids),
                KnowledgeBase.is_deleted == False,  # noqa: E712
            )
            .all()
        )
        return {
            str(row.id): {
                "name": row.name or "",
                "description": row.description or "",
            }
            for row in rows
        }
    except Exception as e:
        logger.warning("KB meta load failed for market grounding (fallback to first bound): %s", e)
        return {}


def _execute_nl2sql_node(db, execution, node, data_ctx_extras=None, endpoint=None) -> ObservationRecord:
    """Execute an NL2SQL node — query a real data source when bound KBs exist.

    When ``data_ctx_extras`` carries ``bound_kb_ids`` (injected by
    ``prepare_data_source_runtime`` in the v3 streaming path), the node
    routes through the live ``NLAnswerService`` pipeline: schema linker
    (if enabled) → SQL generation → execution → narration.  The
    observation ``result_data`` carries both ``sql`` and ``data`` (rows)
    so the synthesize node downstream can consume them.

    When no bound KBs are available, falls back to the legacy
    ``DataSnapshotService.nl2sql()`` which generates SQL without executing
    it against a real database.

    ``endpoint`` (hierarchical LLMEndpoint) is forwarded to
    ``NLAnswerService.answer`` so SQL generation AND narration use the
    pinned model.
    """
    from app.services.data_snapshot.snapshot_service import DataSnapshotService

    question = (node.inputs or {}).get("question", node.description or node.name)
    bound_kb_ids: list[str] = list((data_ctx_extras or {}).get("bound_kb_ids") or [])

    # ── Live data-source path: bound KBs exist ─────────────────────────
    if bound_kb_ids:
        from app.services.db import NLAnswerService
        from app.services.db.connector_factory import DriverUnavailable

        # Market-intent grounding (Task B2): when the user message mentions
        # market/industry keywords, prefer the Market Research KB over the
        # first-bound ERP warehouse. Deterministic — no LLM.
        user_message = getattr(execution, "user_message", None) or question
        kb_id = _select_grounding_kb(
            bound_kb_ids,
            _load_bound_kb_meta(db, bound_kb_ids),
            user_message,
        )
        try:
            svc = NLAnswerService(db)
            result = _run_coro_sync(svc.answer(kb_id, question, endpoint=endpoint))

            # Defensive: NLAnswerService may return an error dict on some failures
            if isinstance(result, dict) and result.get("success") is False:
                return _record_observation(
                    db, execution, node,
                    observation_type="nl2sql",
                    tool_name="nl2sql",
                    request_args={"question": question, "kb_id": kb_id},
                    success=False,
                    error_message=result.get("error", "Data source query failed"),
                )

            rows = result.get("rows", []) if isinstance(result, dict) else []
            sql = (result.get("sql") or "") if isinstance(result, dict) else ""
            answer = (result.get("answer") or "") if isinstance(result, dict) else ""

            return _record_observation(
                db, execution, node,
                observation_type="nl2sql",
                tool_name="nl2sql",
                request_args={"question": question, "kb_id": kb_id, "sql": sql},
                result_data={
                    "sql": sql,
                    "data": rows,
                    "source_id": result.get("source_id") if isinstance(result, dict) else None,
                    "source_name": result.get("source_name") if isinstance(result, dict) else None,
                    "answer": answer,
                    "citations": result.get("citations") if isinstance(result, dict) else None,
                },
                result_text=answer or f"Generated SQL: {sql}",
                success=True,
            )
        except DriverUnavailable as e:
            return _record_observation(
                db, execution, node,
                observation_type="nl2sql",
                tool_name="nl2sql",
                request_args={"question": question, "kb_id": kb_id},
                success=False,
                error_message=f"Database driver unavailable: {e}",
            )
        except Exception as e:
            return _record_observation(
                db, execution, node,
                observation_type="nl2sql",
                tool_name="nl2sql",
                request_args={"question": question, "kb_id": kb_id},
                success=False,
                error_message=f"Data source query failed: {e}",
            )

    # ── Fallback: no bound KBs → schema-only SQL generation ────────────
    service = DataSnapshotService(db)
    schema_desc = (node.inputs or {}).get("schema_description", "No schema available")
    try:
        nl2sql_result = service.nl2sql(question=question, schema_description=schema_desc)

        if not nl2sql_result["valid"]:
            return _record_observation(
                db, execution, node,
                observation_type="nl2sql",
                tool_name="nl2sql",
                request_args={"question": question},
                success=False,
                error_message=f"SQL validation failed: {nl2sql_result['errors']}",
            )

        return _record_observation(
            db, execution, node,
            observation_type="nl2sql",
            tool_name="nl2sql",
            request_args={"question": question, "sql": nl2sql_result["sql"]},
            result_data=nl2sql_result,
            result_text=f"Generated SQL: {nl2sql_result['sql']}",
            success=True,
        )
    except Exception as e:
        return _record_observation(
            db, execution, node,
            observation_type="nl2sql",
            tool_name="nl2sql",
            success=False,
            error_message=str(e),
        )


def _get_previous_observation(
    db: Session,
    execution: Execution,
) -> Optional[ObservationRecord]:
    """Get the most recent observation for this execution (if any).

    Used by chained nodes (synthesize reads nl2sql's output, sandbox
    reads synthesize's output) to pass data through the DAG without
    coupling node implementations.
    """
    return (
        db.query(ObservationRecord)
        .filter(ObservationRecord.execution_id == execution.id)
        .order_by(ObservationRecord.seq.desc())
        .first()
    )


def _get_all_data_observations(db, execution) -> list:
    """Get ALL nl2sql observations for this execution, chronologically.

    Phase 3b (G5): synthesize aggregates every data observation instead of
    only the most recent, so multi-step retrieval isn't lost. Single-obs
    plans return a one-element list (identical to prior behavior).
    """
    return (
        db.query(ObservationRecord)
        .filter(
            ObservationRecord.execution_id == execution.id,
            ObservationRecord.observation_type == "nl2sql",
        )
        .order_by(ObservationRecord.seq.asc())
        .all()
    )


def _get_latest_skill_observation(db, execution) -> Optional[ObservationRecord]:
    """Get the latest successful skill-load observation for this execution."""
    return (
        db.query(ObservationRecord)
        .filter(
            ObservationRecord.execution_id == execution.id,
            ObservationRecord.observation_type == "skill_call",
            ObservationRecord.success == True,
        )
        .order_by(ObservationRecord.seq.desc())
        .first()
    )


async def _synth_call_llm(system_prompt: str, messages: list[dict], endpoint=None) -> dict:
    """Adapter bridging ``synthesize_report``'s ``call_llm_fn`` to async ``call_llm``.

    ``synthesize_report`` awaits ``call_llm_fn(system, messages)`` and expects
    ``{"content": "...", "kpis": [...]}``; ``call_llm`` returns
    ``{"response": "..."}``. ``endpoint`` (hierarchical LLMEndpoint) pins the
    call to the selected provider+model.
    """
    from app.services.llm_service import call_llm
    result = await call_llm(
        prompt=system_prompt, messages=messages, temperature=0.7,
        endpoint=endpoint,
    )
    return {"content": result.get("response", "")}


def _finalize_result_to_result_data(finalize_result) -> dict:
    """Convert a :class:`FinalizeResult` to the ObservationRecord ``result_data`` shape.

    Preserves backward compat with the sandbox node (which reads
    ``result_data['instructions']`` and ``result_data['synth_data']['chart']['data']``)
    and adds ``report_card_payload`` for FINALIZE to surface (spec §4.2).
    """
    rcp = getattr(finalize_result, "report_card_payload", None)
    rcp_dict = rcp.model_dump() if rcp is not None else {}
    prose = getattr(finalize_result, "assistant_content", "") or ""
    summary = rcp_dict.get("summary") or prose
    synth_data = {
        "title": rcp_dict.get("title") or "Report",
        "summary": summary,
        "instructions": prose or summary,
        "kpis": rcp_dict.get("kpis") or [],
        "chart": rcp_dict.get("chart"),
        "insights": rcp_dict.get("insights") or [],
    }
    return {
        "summary": summary,
        "instructions": synth_data["instructions"],
        "synth_data": synth_data,
        "report_card_payload": rcp_dict or None,
    }


def _select_finalize_report_card_payload(observations, artifact_payload):
    """Choose the ReportCardPayload dict for FINALIZE.

    File-export artifact payload wins; otherwise surface the synthesize
    node's structured payload for non-file data tasks (spec §4.2).
    Returns a dict or None.
    """
    if artifact_payload is not None:
        return artifact_payload
    for obs in observations or []:
        if (getattr(obs, "observation_type", "") == "synthesize"
                and getattr(obs, "success", False)):
            _rcp = (getattr(obs, "result_data", None) or {}).get("report_card_payload")
            if isinstance(_rcp, dict) and _rcp:
                return _rcp
    return None


def _execute_synthesize_node(db, execution, node, endpoint=None) -> ObservationRecord:
    """Execute a synthesize node — write an executive summary via LLM.

    This is the "Synthesizer" agent role.  It reads the previous node's
    result (e.g. nl2sql-generated data) and produces a narrative summary
    that serves as instructions for the Presentation Designer sandbox.

    ``endpoint`` (hierarchical LLMEndpoint) pins the synthesis LLM calls.
    """
    from app.services.synexia import report_synthesis

    # Phase 3b (G5): aggregate ALL nl2sql observations instead of only the
    # most recent, so multi-step retrieval isn't lost. Single-obs plans
    # behave identically to the prior last-only behavior.
    data_obs = _get_all_data_observations(db, execution)
    rows: list = []
    sql_parts: list[str] = []
    for _obs in data_obs:
        if not getattr(_obs, "success", False):
            continue
        _rd = getattr(_obs, "result_data", None)
        if not isinstance(_rd, dict):
            continue
        _s = _rd.get("sql")
        if _s:
            sql_parts.append(str(_s))
        _data = _rd.get("data")
        if isinstance(_data, list):
            rows.extend(_data)
    sql = "; ".join(sql_parts) if sql_parts else None

    user_message = execution.user_message or ""
    task_spec = execution.task_spec or {}
    entities = task_spec.get("entities", {}) or {}
    source_name = entities.get("source_name") or entities.get("data_source")
    source_id = entities.get("source_id")
    skill_obs = _get_latest_skill_observation(db, execution)
    skill_result = skill_obs.result_data if skill_obs and isinstance(skill_obs.result_data, dict) else {}
    skill_name = skill_result.get("name") or ((skill_obs.request_args or {}).get("name") if skill_obs else None)
    skill_methodology = skill_result.get("body") if isinstance(skill_result, dict) else None

    try:
        from functools import partial

        finalize_result = _run_coro_sync(report_synthesis.synthesize_report(
            user_message=user_message,
            rows=rows,
            sql=sql,
            source_name=source_name,
            source_id=source_id,
            call_llm_fn=partial(_synth_call_llm, endpoint=endpoint),
            skill_name=skill_name,
            skill_methodology=skill_methodology,
        ))
        result_data = _finalize_result_to_result_data(finalize_result)
        summary_text = result_data.get("summary", "")
        return _record_observation(
            db, execution, node,
            observation_type="synthesize",
            tool_name="synthesizer",
            request_args={
                "user_message": user_message,
                **({"skill_name": skill_name} if skill_name else {}),
            },
            result_data=result_data,
            result_text=summary_text,
            success=True,
        )
    except Exception as e:
        logger.warning(
            "Rich synthesize_report failed, falling back to legacy LLM call: %s", e,
        )
        return _legacy_synthesize_fallback(
            db, execution, node, user_message, task_spec, rows, sql,
            endpoint=endpoint,
        )


def _legacy_synthesize_fallback(db, execution, node, user_message, task_spec, rows, sql, endpoint=None) -> ObservationRecord:
    """Pre-Phase-2 simple synthesize path, used when ``synthesize_report`` raises.

    Mirrors the original ``_execute_synthesize_node`` logic but correctly
    awaits the async ``call_llm`` via ``_run_coro_sync`` (the prior code
    called it without await, silently always falling through to the stub).
    Produces the same ``result_data`` shape, with ``report_card_payload=None``.

    ``endpoint`` (hierarchical LLMEndpoint) pins the LLM call.
    """
    import json
    from app.services.llm_service import call_llm

    entities = task_spec.get("entities", {}) or {}
    nl2sql_context = ""
    if sql:
        nl2sql_context = (
            f"SQL executed:\n{sql}\n\nReturned {len(rows)} rows."
            if rows else f"SQL generated:\n{sql}"
        )

    system_prompt = (
        f"You are the REPORT SYNTHESIZER for a data-driven chat agent.\n\n"
        f"User request: {user_message}\n"
        f"Entities: {json.dumps(entities, ensure_ascii=False)}\n"
        f"{nl2sql_context}\n\n"
        f"Write a 1-2 paragraph executive summary that a Presentation "
        f"Designer will use as **instructions** to build a professional "
        f"report/document. Include:\n"
        f"1. The title and scope of the report\n"
        f"2. Key metrics or KPIs to highlight (with values if known)\n"
        f"3. The narrative flow (executive summary, findings, recommendations)\n"
        f"4. Any specific formatting or style instructions\n\n"
        f"Respond with a JSON object:\n"
        f"{{\n"
        f'  "title": "Report title",\n'
        f'  "summary": "Executive summary narrative (2-3 sentences)",\n'
        f'  "instructions": "Detailed instructions for the Presentation Designer",\n'
        f'  "kpis": [{{"label": "KPI name", "value": "KPI value", "caption": "context"}}],\n'
        f'  "chart": {{"type": "bar", "title": "...", "x_key": "...", "y_keys": ["..."], "data": [...], "unit": "..."}} | null,\n'
        f'  "insights": [{{"icon": "trending-up", "text": "..."}}]\n'
        f"}}\n"
        f"Respond with ONLY the JSON object, no surrounding text."
    )

    try:
        result = _run_coro_sync(call_llm(
            prompt=system_prompt, messages=[], temperature=0.3,
            endpoint=endpoint,
        ))
        response_text = result.get("response", "").strip()
        if "```json" in response_text:
            response_text = response_text.split("```json")[1].split("```")[0]
        elif "```" in response_text:
            response_text = response_text.split("```")[1].split("```")[0]
        synth_data = json.loads(response_text) if response_text else {}
    except Exception as e:
        logger.warning("Legacy synthesize fallback LLM call failed: %s", e)
        synth_data = {
            "title": entities.get("report_title", "Report"),
            "summary": f"Analysis of {entities.get('metric', 'data')}.",
            "instructions": "Generate a professional report from the provided data.",
            "kpis": [], "chart": None, "insights": [],
        }

    summary_text = synth_data.get("summary", "")
    instructions_text = synth_data.get("instructions", synth_data.get("summary", ""))
    return _record_observation(
        db, execution, node,
        observation_type="synthesize",
        tool_name="synthesizer",
        request_args={"user_message": user_message},
        result_data={
            "summary": summary_text,
            "instructions": instructions_text,
            "synth_data": synth_data,
            "report_card_payload": None,
        },
        result_text=summary_text,
        success=True,
    )


def _execute_sandbox_node(db, execution, node) -> ObservationRecord:
    """Execute a sandbox node — generate a file artifact in Docker.

    If a previous synthesize node produced instructions, those are
    merged into the sandbox's input_package so the Presentation Designer
    can follow them.  The function blocks until the sandbox job completes
    (up to 120s), mirroring the ``run_sandbox_skill_sync`` pattern used
    by the legacy finalize path.
    """
    from app.services.sandbox.sandbox_service import SandboxService
    from app.services.tool_handlers.sandbox_tool import (
        run_sandbox_skill_sync,
    )

    skill_name = (node.inputs or {}).get("skill_name", node.name)
    artifact_type = node.output_artifact_type or "docx"

    # ── Gather instructions from the previous synthesize node ─────────
    prev_obs = _get_previous_observation(db, execution)
    instructions = ""
    chart_rows = []
    synth_data = {}
    if prev_obs and prev_obs.success and prev_obs.observation_type == "synthesize":
        result = prev_obs.result_data or {}
        instructions = result.get("instructions", "")
        synth_data = result.get("synth_data", {})
        # Pull chart data from the synthesized payload if available
        chart_spec = synth_data.get("chart") if isinstance(synth_data, dict) else None
        if chart_spec and isinstance(chart_spec, dict):
            chart_rows = chart_spec.get("data", [])

    title = (node.inputs or {}).get("title") or synth_data.get("title", node.name)

    try:
        # Use run_sandbox_skill_sync which handles Artifact creation, job
        # enqueue, polling, and result extraction in one call.
        sandbox_result = run_sandbox_skill_sync(
            args={
                "format": artifact_type,
                "data": chart_rows or [{"note": "See generated report for details"}],
                "title": title,
                "instructions": instructions or f"Generate a {artifact_type.upper()} report titled '{title}'.",
            },
            db=db,
            user_id=execution.agent_name,
            context={
                "conversation_id": execution.conversation_id,
                "agent_app_id": execution.agent_name,
            },
        )

        if sandbox_result.get("success"):
            artifact_id = sandbox_result["artifact_id"]

            # Collect artifact IDs on the execution
            art_ids = execution.artifact_ids or []
            if artifact_id not in art_ids:
                art_ids.append(artifact_id)
                execution.artifact_ids = art_ids

            return _record_observation(
                db, execution, node,
                observation_type="sandbox",
                tool_name=skill_name,
                request_args={"artifact_type": artifact_type, "title": title},
                result_data={
                    "artifact_id": artifact_id,
                    "artifact_version_id": sandbox_result.get("artifact_version_id"),
                    "format": artifact_type,
                    "status": "completed",
                    "sandbox_result": sandbox_result,
                },
                result_text=f"Generated {artifact_type.upper()} artifact {artifact_id}",
                success=True,
                artifact_ids=[artifact_id],
            )
        else:
            return _record_observation(
                db, execution, node,
                observation_type="sandbox",
                tool_name=skill_name,
                request_args={"artifact_type": artifact_type},
                success=False,
                error_message=sandbox_result.get("error", "Sandbox job failed"),
            )

    except Exception as e:
        logger.error("Sandbox node execution failed: %s", e)
        return _record_observation(
            db, execution, node,
            observation_type="sandbox",
            tool_name=skill_name,
            success=False,
            error_message=str(e),
        )


def _execute_skill_node(
    db, execution, node,
    user_id: Optional[str] = None,
    data_ctx_extras: Optional[dict] = None,
) -> ObservationRecord:
    """Execute a skill node by loading its full methodology body."""
    from app.services.agent_tools import execute_tool_with_retry

    args = dict(node.inputs or {})
    skill_name = args.get("skill_name") or args.get("name") or node.name
    skill_id = args.get("skill_id")
    tool_context = {
        "conversation_id": getattr(execution, "conversation_id", None),
        # Execution identity lets the artifact path ground a deck in the REAL
        # query rows this execution fetched (see deck_data.collect_grounded_rows).
        "execution_id": getattr(execution, "id", None),
        "agent_app_id": getattr(execution, "app_id", None) or getattr(execution, "agent_name", None),
        "agent_name": getattr(execution, "agent_name", None),
        **(data_ctx_extras or {}),
    }
    request_args = {
        **({"skill_id": skill_id} if skill_id else {}),
        **({"name": skill_name} if skill_name else {}),
    }

    try:
        result = _run_coro_sync(
            execute_tool_with_retry("load_skill_body", request_args, db, user_id, context=tool_context)
        )
        result_data = result if isinstance(result, dict) else {"result": str(result)}
        result_text = result_data.get("body") or result_data.get("response") or str(result_data)
        ok = bool(result_data.get("success", True))

        return _record_observation(
            db, execution, node,
            observation_type="skill_call",
            tool_name=skill_name or "load_skill_body",
            request_args=request_args,
            result_data=result_data,
            result_text=result_text,
            success=ok,
            error_message=None if ok else str(result_data.get("error", "skill load failed")),
        )
    except Exception as e:
        return _record_observation(
            db, execution, node,
            observation_type="skill_call",
            tool_name=skill_name or "load_skill_body",
            request_args=request_args,
            success=False,
            error_message=str(e),
        )


def _record_observation(
    db, execution, node,
    observation_type, tool_name, request_args=None,
    result_data=None, result_text=None, success=True,
    error_message=None, artifact_ids=None,
) -> ObservationRecord:
    """Record an observation for a plan node execution."""
    # Get next sequence number
    existing = (
        db.query(ObservationRecord)
        .filter(ObservationRecord.execution_id == execution.id)
        .count()
    )

    obs = ObservationRecord(
        id=str(uuid4()),
        execution_id=execution.id,
        plan_node_id=node.id,
        seq=existing,
        observation_type=observation_type,
        tool_name=tool_name,
        request_args=request_args,
        result_data=result_data,
        result_text=result_text,
        success=success,
        error_message=error_message,
        artifact_ids=artifact_ids,
    )
    db.add(obs)
    db.commit()
    db.refresh(obs)
    return obs


def _topological_sort(nodes) -> list:
    """Sort plan nodes in topological order (dependencies first).

    Raises ValueError if a dependency cycle is detected.
    """
    # Build node index by seq
    node_map = {n.seq: n for n in nodes}
    visited = set()   # fully explored (black)
    visiting = set()  # in current DFS path (gray)
    result = []

    def visit(seq):
        if seq in visiting:
            raise ValueError(f"Dependency cycle detected at node seq={seq}")
        if seq in visited:
            return
        visiting.add(seq)
        node = node_map.get(seq)
        if not node:
            visiting.discard(seq)
            return
        for dep in (node.dependencies or []):
            visit(dep)
        visiting.discard(seq)
        visited.add(seq)
        result.append(node)

    for n in nodes:
        visit(n.seq)

    return result


def _dependencies_met(node, all_nodes) -> bool:
    """Check if all dependencies of a node are completed."""
    node_map = {n.seq: n for n in all_nodes}
    for dep_seq in (node.dependencies or []):
        dep_node = node_map.get(dep_seq)
        if not dep_node or dep_node.status not in ("completed", "skipped"):
            return False
    return True
