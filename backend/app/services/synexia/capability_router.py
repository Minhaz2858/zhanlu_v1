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

    # ── IBM MAS fix (2026-10-08): parallel ready-batch execution ────────
    # The DAG used to execute strictly serially even when independent nodes
    # were both ready. This pre-pass executes ready nodes in parallel
    # batches (each in its OWN DB session — the parent session is not
    # concurrency-safe), then the serial loop below handles whatever
    # remains (dependent stages, adaptive replanning, error recovery).
    _parallel_ok = (
        getattr(settings, "PLAN_DAG_PARALLEL_ENABLED", True)
        and not _adaptive_enabled
        and len(plan.nodes) > 1
    )
    if _parallel_ok:
        try:
            _parallel_ready_batch_prepass(
                db, execution, plan, ordered_nodes, user_id,
                data_ctx_extras, endpoint, observations, _emit,
            )
        except Exception as e:  # noqa: BLE001 — fall back to serial, always
            logger.warning("Parallel plan pre-pass failed (serial fallback): %s", e)
            db.rollback()

    for _idx, node in enumerate(ordered_nodes):
        # Skip nodes the parallel pre-pass already settled (done/skipped).
        if getattr(node, "status", None) in ("completed", "skipped"):
            continue
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

        # P2 (Part 4): conditional delegation. A node may carry a `run_if`
        # condition in its inputs (no schema change needed). If the condition is
        # NOT satisfied by upstream results / shared evidence, skip the node
        # (do not execute, do not fail) — the dependent downstream nodes simply
        # never receive its output.
        if not _node_condition_met(node, plan, execution):
            node.status = "skipped"
            db.commit()
            _emit(node, "skipped", "condition not met (run_if)")
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

            # P2 (Part 4): human-approval gate. If a node result requires human
            # approval (e.g. an agent sub-task returned requires_approval), surface
            # it and PAUSE the run so downstream nodes do not execute on unapproved
            # output. The chat loop's existing approval/resume flow handles the rest.
            if isinstance(obs.result_data, dict) and obs.result_data.get("requires_approval"):
                try:
                    execution.needs_approval = True
                except Exception:  # noqa: BLE001 — model may lack the column
                    pass
                # Persist the pause state (completed node + gate) BEFORE returning
                # so a process restart / resume sees a consistent execution.
                try:
                    db.commit()
                except Exception:  # noqa: BLE001
                    pass
                _emit(node, "awaiting_approval", obs.result_data.get("approval_reason"))
                return observations

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

    # ── Dynamic re-planning: recover failed nodes ─────────────────────
    # When replannable nodes fail (and the re-plan budget allows), generate a
    # corrective sub-plan from the failure context and execute it. Eligible
    # node types and error classes live in synexia/replan_policy.py: general
    # tool/skill steps plus structural nl2sql failures; synthesize/sandbox,
    # policy denials and environmental errors are excluded.
    _max = max_replan_depth
    if _max is None:
        _max = getattr(settings, "SYNEXIA_ACT_REPLAN_MAX", 2)
    # Spend from the ONE shared per-turn refinement budget (see
    # synexia/refinement_budget.py). ACT depth alone allowed a corrective
    # plan at every level, so combined with the FSM's VERIFY re-plan a single
    # turn created 4 plans — spending the budget on planning and leaving the
    # plan's own final steps `pending`.
    from app.services.synexia.refinement_budget import may_refine

    if replan_depth < _max and may_refine(db, execution.id):
        recoverable = _recoverable_failures(plan)
        if recoverable:
            # 2B-4: give the corrective planner the STRUCTURED scope verdict
            # of each failed node, read from its durable observation — a
            # scope refusal must not be re-authored as the same plan.
            _enrich_failures_with_scope(db, execution.id, recoverable)
            logger.info(
                "ACT re-plan (depth %d): %d failed replannable node(s) — generating corrective plan",
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
    """Collect failed nodes worth a corrective re-plan.

    General tool/skill steps plus structural data-pipeline failures (nl2sql) —
    a failed query whose table/view was wrong can be fixed by a different plan,
    which is why the data pipeline is no longer blanket-excluded here.
    ``synthesize``/``sandbox`` stay excluded (they replay the failed input) and
    so do policy denials and environmental errors (credentials, connection,
    timeout, lock) — a new plan reproduces those identically.

    Eligibility is centralised in ``synexia/replan_policy.py`` so this loop and
    the FSM's VERIFY-driven re-plan agree.
    """
    from app.services.synexia.replan_policy import collect_replannable_failures

    if not plan or not plan.nodes:
        return []
    return collect_replannable_failures(plan.nodes)


def _enrich_failures_with_scope(db, execution_id, failures: list[dict]) -> None:
    """2B-4: attach the structured scope verdict of each failed node to its
    failure-context entry, read from the DURABLE observation records (keyed
    by execution + plan-node identity). Flag-gated
    (SCHEMA_CORRECTIVE_SCOPE_HINTS_ENABLED, ships OFF); any problem degrades
    to no enrichment — the planner then works from the error text alone, and
    enforcement stays the final authority."""
    try:
        from app.config import settings as _s

        if not getattr(_s, "SCHEMA_CORRECTIVE_SCOPE_HINTS_ENABLED", False):
            return
        from app.models.execution import ObservationRecord

        for f in failures or []:
            nid = f.get("node_id")
            if not nid:
                continue
            obs = (
                db.query(ObservationRecord)
                .filter(
                    ObservationRecord.execution_id == execution_id,
                    ObservationRecord.plan_node_id == nid,
                )
                .order_by(ObservationRecord.seq.desc())
                .first()
            )
            rd = getattr(obs, "result_data", None)
            if not isinstance(rd, dict):
                continue
            if rd.get("scope_check"):
                f["scope_check"] = rd["scope_check"]
            if rd.get("scope_disclosure"):
                f["scope_disclosure"] = rd["scope_disclosure"]
            # Fallbacks for shapes that carry the verdict only inside the
            # validation payload (execution-gate refusals).
            vc = rd.get("validation_checks")
            if isinstance(vc, dict) and not f.get("scope_check"):
                f["scope_check"] = vc.get("scope_authorization")
            ve = rd.get("validation_errors")
            if ve and not f.get("scope_disclosure"):
                _scope_errs = [
                    str(e) for e in ve
                    if "partition" in str(e).lower()
                    or "company-wide" in str(e).lower()
                ]
                if _scope_errs:
                    f["scope_disclosure"] = "; ".join(_scope_errs)
    except Exception as e:  # degrade — never break the re-plan
        logger.debug("failure-context scope enrichment skipped: %s", e)


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


def _node_condition_met(node, plan, execution) -> bool:
    """P2 (Part 4): evaluate a node's optional `run_if` condition.

    The condition lives in ``node.inputs["run_if"]`` (no schema migration needed).
    Supported sub-keys (all optional, ANDed):
      * requires_evidence_topic: skip if no shared evidence in this execution's
        registry matches the topic (a dependent investigation produced nothing).
      * requires_upstream_status: {node: NAME, status: "completed"} — skip if the
        named upstream node did not end in the required status.
    Returns True when no condition is set or all set conditions are satisfied.
    """
    _cond = ((getattr(node, "inputs", None) or {}) or {}).get("run_if")
    if not isinstance(_cond, dict):
        return True
    _conv = getattr(execution, "conversation_id", None)

    _topic = _cond.get("requires_evidence_topic")
    if _topic:
        try:
            from app.services.agent_collaboration.shared_evidence import get_registry

            if not get_registry().find_by_topic(_conv, _topic):
                return False
        except Exception:  # noqa: BLE001 — on any doubt, do not block
            pass

    _req = _cond.get("requires_upstream_status")
    if isinstance(_req, dict):
        _name = _req.get("node")
        _want = _req.get("status", "completed")
        if _name:
            _matched = False
            for n in (plan.nodes or []):
                if getattr(n, "name", None) == _name:
                    _matched = True
                    if getattr(n, "status", None) != _want:
                        return False
            if not _matched:
                # Unknown upstream — don't block the run on a typo'd name.
                return True
    return True


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
    elif node_type == "reviewer":
        # P2 (Part 1 + Part 4): a first-class critic/reviewer stage. Aggregates the
        # upstream findings (provided in node.inputs["content"]) and the shared
        # evidence produced in this execution, runs the fact-safe reviewer, and if
        # the reviewer reports missing evidence, dispatches BOUNDED targeted
        # follow-up delegations through the governed path before re-reviewing.
        from app.services.agent_collaboration.reviewer import review_findings
        from app.services.agent_collaboration.shared_evidence import get_registry

        _args = dict(node.inputs or {})
        _content = _args.get("content") or node.name
        _question = _args.get("question") or node.name
        _conv = getattr(execution, "conversation_id", None)
        _evidence = []
        if _conv:
            try:
                _evidence = get_registry().list_items(_conv)
            except Exception:  # noqa: BLE001
                _evidence = []

        _report = review_findings(
            question=_question, content=_content,
            evidence_items=_evidence, endpoint=endpoint,
        )

        _retries = int(getattr(settings, "COLLAB_REVIEW_MAX_RETRIES", 1))
        if _report.needs_evidence and _retries > 0 and _args.get("agent_name"):
            _extra = []
            for _gap in _report.needs_evidence[:2]:
                try:
                    from app.services.agent_tools import execute_tool_with_retry

                    _sub = _run_coro_sync(execute_tool_with_retry(
                        "delegate_task",
                        {"task": f"Targeted follow-up to review gap: {_gap}",
                         "agent_name": _args["agent_name"], "review": False},
                        db, user_id,
                        context={
                            "conversation_id": _conv,
                            "execution_id": getattr(execution, "id", None),
                            "agent_name": getattr(execution, "agent_name", None),
                            **(data_ctx_extras or {}),
                        },
                    ))
                    if isinstance(_sub, dict) and _sub.get("success"):
                        _extra.append(f"[follow-up: {_gap}] {_sub.get('response') or ''}")
                except Exception as _e:  # noqa: BLE001
                    logger.debug("reviewer node follow-up failed (non-fatal): %s", _e)
            if _extra:
                _content = _content + "\n\n" + "\n".join(_extra)
                _evidence = get_registry().list_items(_conv) if _conv else []
                _report = review_findings(
                    question=_question, content=_content,
                    evidence_items=_evidence, endpoint=endpoint,
                )

        _rd = _report.to_dict()
        return _record_observation(
            db, execution, node,
            observation_type="reviewer",
            tool_name="reviewer",
            request_args=_args,
            result_data={"review": _rd, "content": _content},
            result_text=f"Review verdict: {_rd['overall']} (answered={_rd['answered']})",
            success=True,
        )
    elif node_type == "agent":
        # P1 (2026-10-31, roadmap Q2): the "agent" node type was DECLARED in
        # this module's docstring but never dispatched — it silently fell
        # through to the tool node. Implement it by delegating to the
        # delegate_task TOOL through the same governed path
        # (execute_tool_with_retry → _run_sub_agent): the child runs with
        # its AgentApp prompt/toolset (P0 AgentApp-aware delegation), its
        # own authorized bindings, wall-clock budget, permission checks and
        # invocation linking. No new orchestrator — the DAG schedules, the
        # existing delegation runtime executes.
        from app.services.agent_tools import execute_tool_with_retry

        args = dict(node.inputs or {})
        delegate_args = {
            "task": args.get("task") or args.get("objective") or node.name,
            "agent_name": args.get("agent_name") or args.get("agent") or "general_assistant",
        }
        if args.get("max_iterations"):
            delegate_args["max_iterations"] = args["max_iterations"]
        tool_context = {
            "conversation_id": getattr(execution, "conversation_id", None),
            "execution_id": getattr(execution, "id", None),
            "agent_app_id": getattr(execution, "app_id", None) or getattr(execution, "agent_name", None),
            "agent_name": getattr(execution, "agent_name", None),
            **(data_ctx_extras or {}),
        }
        try:
            result = _run_coro_sync(
                execute_tool_with_retry("delegate_task", delegate_args, db, user_id, context=tool_context)
            )
            result_data = result if isinstance(result, dict) else {"result": str(result)}
            ok = bool(result_data.get("success", True))
            result_text = result_data.get("response") or str(result_data)
            if result_data.get("truncated"):
                result_text = "[PARTIAL — child hit its wall-clock budget]\n" + result_text
            return _record_observation(
                db, execution, node,
                observation_type="agent_delegation",
                tool_name="delegate_task",
                request_args=delegate_args,
                result_data=result_data,
                result_text=result_text,
                success=ok,
                error_message=None if ok else str(result_data.get("error", "agent node failed")),
            )
        except Exception as e:
            return _record_observation(
                db, execution, node,
                observation_type="agent_delegation",
                tool_name="delegate_task",
                request_args=delegate_args,
                success=False,
                error_message=str(e),
            )
    else:
        # Default: treat as tool
        return _execute_tool_node(db, execution, node, user_id, data_ctx_extras=data_ctx_extras)


def _parallel_ready_batch_prepass(
    db: Session,
    execution: Execution,
    plan: Plan,
    ordered_nodes: list,
    user_id: Optional[str],
    data_ctx_extras: Optional[dict],
    endpoint,
    observations: list,
    _emit,
) -> None:
    """Execute independent ready nodes in parallel batches (IBM MAS).

    Each batch = nodes whose dependencies are all satisfied. Every node runs
    in its OWN database session (SQLAlchemy sessions are not concurrency-safe)
    via asyncio.to_thread, bounded by ``PLAN_DAG_MAX_CONCURRENT``. After each
    batch the parent session expires its cache so dependency checks on the
    next round see the committed statuses. The serial loop in
    ``execute_plan_nodes`` handles whatever this leaves (dependent stages,
    adaptive checkpoints, replanning).
    """
    import asyncio as _asyncio
    from app.database import SessionLocal

    max_conc = max(2, int(getattr(settings, "PLAN_DAG_MAX_CONCURRENT", 4)))
    executed: set[str] = set()

    def _exec_isolated(node_id: str) -> dict | None:
        """Run one node in an isolated session; returns its observation id."""
        db2 = SessionLocal()
        try:
            node2 = db2.query(PlanNode).filter(PlanNode.id == node_id).first()
            exec2 = db2.query(Execution).filter(Execution.id == execution.id).first()
            if node2 is None or exec2 is None:
                return None
            try:
                obs = _execute_single_node(
                    db2, exec2, node2, user_id,
                    data_ctx_extras=data_ctx_extras, endpoint=endpoint,
                )
                node2.status = "completed" if obs.success else "failed"
                node2.result = obs.result_data
                node2.error = obs.error_message
                node2.completed_at = datetime.now(timezone.utc)
                obs.seq = 0  # renumbered by the parent after the batch
                db2.add(obs)
                db2.commit()
                return {"obs_id": obs.id}
            except Exception as e:  # noqa: BLE001 — mirror the serial error path
                logger.error("Parallel node %s failed: %s", node2.name, e)
                node2.status = "failed"
                node2.error = str(e)
                node2.completed_at = datetime.now(timezone.utc)
                err_obs = ObservationRecord(
                    id=str(uuid4()),
                    execution_id=execution.id,
                    plan_node_id=node2.id,
                    seq=0,
                    observation_type="error",
                    tool_name=node2.name,
                    success=False,
                    error_message=str(e),
                )
                db2.add(err_obs)
                db2.commit()
                return {"obs_id": err_obs.id}
        finally:
            db2.close()

    async def _gather_batch(batch_ids: list[str]) -> list[dict | None]:
        sem = _asyncio.Semaphore(max_conc)

        async def _one(nid: str):
            async with sem:
                return await _asyncio.to_thread(_exec_isolated, nid)

        return list(await _asyncio.gather(*[_one(nid) for nid in batch_ids]))

    for _round in range(len(ordered_nodes) + 1):
        db.expire_all()  # see isolated sessions' committed statuses
        ready: list = []
        for node in ordered_nodes:
            if node.id in executed:
                continue
            if not _dependencies_met(node, plan.nodes):
                continue
            decision = evaluate_node(node, execution.policy_decision or {})
            if decision["decision"] == "deny":
                node.status = "failed"
                node.error = f"Policy denied: {decision['reason']}"
                db.commit()
                _emit(node, "denied", decision["reason"])
                executed.add(node.id)
                continue
            ready.append(node)
            if len(ready) >= max_conc:
                break

        if not ready:
            return  # nothing runnable in parallel — hand off to serial loop

        for node in ready:
            node.status = "running"
            node.started_at = datetime.now(timezone.utc)
        db.commit()
        for node in ready:
            _emit(node, "running")

        results = _run_coro_sync(_gather_batch([n.id for n in ready]))
        db.expire_all()

        obs_ids = [r["obs_id"] for r in results if r and r.get("obs_id")]
        if obs_ids:
            obs_rows = (
                db.query(ObservationRecord)
                .filter(ObservationRecord.id.in_(obs_ids))
                .all()
            )
            by_id = {o.id: o for o in obs_rows}
            for r in results:
                if not r:
                    continue
                obs = by_id.get(r.get("obs_id"))
                if obs is not None:
                    obs.seq = len(observations)
                    db.commit()
                    observations.append(obs)

        for node in ready:
            fresh = db.query(PlanNode).filter(PlanNode.id == node.id).first()
            if fresh is not None:
                _emit(fresh, fresh.status or "failed", fresh.error)
            executed.add(node.id)


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
    # FSM tenant isolation (IC-2, 2026-10-31): the tool context carries the
    # org AND an org-resolved memory key — the previous value
    # (execution.app_id or bare agent_name) was org-ambiguous.
    tool_context = {
        "conversation_id": getattr(execution, "conversation_id", None),
        # Execution identity lets the artifact path ground a deck in the REAL
        # query rows this execution fetched (see deck_data.collect_grounded_rows).
        "execution_id": getattr(execution, "id", None),
        "org_id": getattr(execution, "org_id", None),
        "agent_app_id": _fsm_exec_memory_key(db, execution)
        or getattr(execution, "agent_name", None),
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
# The SAMPLE project binds two KBs: the ERP warehouse and the Market Research
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


def _fsm_exec_memory_key(db, execution) -> str | None:
    """Org-safe memory key for an FSM execution (IC-2, 2026-10-31).

    Resolves the execution's agent with STRICT org equality
    (synexia.identity) and returns identity.memory_write_key: the agent
    PK, or the bare-name legacy bridge for default-org callers only,
    or None when the org has no own row (fail closed — callers must not
    write memories under a shared key).
    """
    try:
        from app.services.synexia.identity import (
            memory_write_key,
            resolve_agent_in_org,
        )

        agent = resolve_agent_in_org(
            db,
            agent_id=getattr(execution, "agent_id", None),
            agent_name=getattr(execution, "agent_name", None),
            org_id=getattr(execution, "org_id", None),
        )
        return memory_write_key(
            agent_row=agent,
            agent_name=getattr(execution, "agent_name", None),
            org_id=getattr(execution, "org_id", None),
        )
    except Exception:  # noqa: BLE001 — isolation metadata must never break tools
        return None


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


def _resolve_node_policy(db, execution, data_ctx_extras):
    """Resolve the caller's data-access policy for an FSM data node (P0-1).

    Uses the shared ``resolve_policy_from_context`` — same resolution as
    ``answer_from_database`` and the delegation fastpath. Fails open (full
    access) when the node context carries no resource scoping.
    """
    try:
        from app.services.access_policy_service import resolve_policy_from_context

        return resolve_policy_from_context(
            db,
            getattr(execution, "user_id", None),
            data_ctx_extras or {},
        )
    except Exception as e:  # fail open — policy must never break the node
        logger.warning("FSM data node: policy resolution failed (fail open): %s", e)
        from app.services.access_policy_service import ResolvedPolicy

        return ResolvedPolicy(has_policies=False)


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
            # Per-turn SQL budget scope: prefer the CANONICAL TURN identity
            # threaded from the chat loop (one user message = one turn) so
            # repeated query_database invocations share one budget; fall
            # back to the per-execution scope for direct FSM entry.
            _dce = data_ctx_extras or {}
            _turn_scope = (
                _dce.get("turn_id")
                or (f"exec:{getattr(execution, 'id', '') or 'unknown'}")
            )
            result = _run_coro_sync(svc.answer(
                kb_id, question, endpoint=endpoint,
                turn_id=_turn_scope,
                # P0-1 (2026-10-30, audit D2): resolve the user's data-access
                # policy like every other caller — previously None here left
                # pre-generation policy filtering inert on the FSM path.
                # data_ctx_extras carries resource_type/resource_id/user_id
                # from prepare_data_source_runtime; no scoping → full access.
                resolved_policy=(
                    _resolve_node_policy(db, execution, data_ctx_extras)
                ),
                # 2B-4: a corrective re-plan annotates the replacement data
                # step with the structured scope refusal it must not repeat.
                scope_hint=(node.inputs or {}).get("scope_hint"),
            ))

            # Defensive: NLAnswerService may return an error dict on some failures
            if isinstance(result, dict) and result.get("success") is False:
                # 2B-4: the durable failure observation must carry the
                # STRUCTURED scope verdict/refusal (previously result_data
                # was dropped on failures, so the corrective re-plan could
                # only guess from the error string).
                _fail_rd: dict = {}
                if result.get("scope_check"):
                    _fail_rd["scope_check"] = result["scope_check"]
                if result.get("scope_disclosure"):
                    _fail_rd["scope_disclosure"] = result["scope_disclosure"]
                _val = result.get("validation")
                if isinstance(_val, dict):
                    _fail_rd["validation_checks"] = _val.get("checks")
                    _fail_rd["validation_errors"] = _val.get("errors")
                    _sc = (_val.get("checks") or {}).get("scope_authorization")
                    if _sc and not _fail_rd.get("scope_check"):
                        _fail_rd["scope_check"] = _sc
                    _scope_errs = [
                        str(e) for e in (_val.get("errors") or [])
                        if "partition" in str(e).lower()
                        or "company-wide" in str(e).lower()
                    ]
                    if _scope_errs and not _fail_rd.get("scope_disclosure"):
                        _fail_rd["scope_disclosure"] = "; ".join(_scope_errs)
                if result.get("error_kind"):
                    _fail_rd["error_kind"] = result["error_kind"]
                return _record_observation(
                    db, execution, node,
                    observation_type="nl2sql",
                    tool_name="nl2sql",
                    request_args={"question": question, "kb_id": kb_id},
                    result_data=(_fail_rd or None),
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
                    # Mixed-mode marker (2026-10-11): a semantic_lookup result
                    # (definitional question answered from the VERIFIED
                    # glossary) must pass through the finalize narrator
                    # verbatim — see fsm.stream_final_response.
                    "mixed_mode": result.get("mixed_mode") if isinstance(result, dict) else None,
                    "citations": result.get("citations") if isinstance(result, dict) else None,
                    # P3: the recorded observation is a whitelist, so the
                    # grounding envelope travels explicitly. Without this the
                    # workflow's stored evidence says a figure was returned and
                    # nothing says a different part of the question could not be.
                    "disclosure": (
                        result.get("disclosure") if isinstance(result, dict) else None
                    ),
                    "unsupported_slots": (
                        result.get("unsupported_slots") if isinstance(result, dict) else None
                    ),
                    # 2026-10-29: structured measure provenance (claimed
                    # concept → column, aggregation fingerprint) + family-
                    # grain disclosure. Consumed by cross-node reconciliation
                    # before synthesis (see _execute_synthesize_node).
                    "measure_provenance": (
                        result.get("measure_provenance") if isinstance(result, dict) else None
                    ),
                    "family_disclosure": (
                        result.get("family_disclosure") if isinstance(result, dict) else None
                    ),
                    # 2B-3 (review check 3): scope warnings/refusals travel the
                    # SAME structured envelope, so cross-node synthesis sees
                    # them even when it re-derives prose from rows alone.
                    "scope_disclosure": (
                        result.get("scope_disclosure") if isinstance(result, dict) else None
                    ),
                    "scope_check": (
                        result.get("scope_check") if isinstance(result, dict) else None
                    ),
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

    # ── No bound KBs → there is NO data source to execute against ──────────
    # 2026-10-21 HONESTY FIX (audit B1): the legacy schema-only path generated
    # SQL text and recorded success=True, which let the agent narrate
    # unexecuted SQL as a real answer ("0 rows" for a query that was never
    # run — the same class of bug fixed in query_database_tool). A node with
    # no bound data source cannot answer a data question; report it as a
    # FAILURE with an honest hint to bind a project/data source, never as a
    # successful observation. We still generate best-effort SQL for the
    # detail/transparency but mark success=False and never claim execution.
    service = DataSnapshotService(db)
    schema_desc = (node.inputs or {}).get("schema_description", "No schema available")
    generated_sql = None
    try:
        nl2sql_result = service.nl2sql(question=question, schema_description=schema_desc)
        if isinstance(nl2sql_result, dict) and nl2sql_result.get("valid"):
            generated_sql = nl2sql_result.get("sql")
    except Exception as e:  # noqa: BLE001 — non-fatal; we report the binding failure
        logger.debug("schema-only SQL gen failed (non-fatal): %s", e)

    return _record_observation(
        db, execution, node,
        observation_type="nl2sql",
        tool_name="nl2sql",
        request_args={"question": question},
        result_data={"sql": generated_sql, "executed": False},
        result_text=(
            "No bound data source reached this node — the query was NOT "
            "executed against any database. Bind a project or data source "
            "so the live NL2SQL pipeline can run."
        ),
        success=False,
        error_message=(
            "NL2SQL node had no bound data source (bound_kb_ids empty); "
            "schema-only SQL was generated but never executed, so this is a "
            "pipeline/binding failure, not empty data."
        ),
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

    # ── Cross-node measure reconciliation (2026-10-29 root-cause fix) ────
    # Before synthesis, compare what each successful node CLAIMED to measure
    # (claimed concept → column) with what the others claimed. Two nodes
    # claiming the same concept from different columns (e.g. one summing the
    # net amount, another the tax-inclusive amount) are a conflict: the
    # conflict is disclosed into the synthesis input so the report either
    # qualifies the figures or withholds the combined total — it never
    # silently blends incompatible measures. Generic: terms come from each
    # datasource's own verified metric catalog via the node provenance.
    _reconciliation_disclosures: list[str] = []
    try:
        from app.services.db.query_contract import reconcile_fingerprints

        _claimed = []
        _family_notes = []
        _scope_notes = []
        for _obs in data_obs:
            if not getattr(_obs, "success", False):
                continue
            _rd = getattr(_obs, "result_data", None)
            if not isinstance(_rd, dict):
                continue
            _mp = _rd.get("measure_provenance")
            if isinstance(_mp, dict) and isinstance(
                _mp.get("claimed_measure_terms"), dict
            ):
                _claimed.append(_mp["claimed_measure_terms"])
            _fd = _rd.get("family_disclosure")
            if _fd:
                _family_notes.append(str(_fd))
            # 2B-3: scope warnings/refusals must qualify the synthesized
            # report — a node that answered from partition data forces the
            # synthesis to say so, whatever prose it re-derives from rows.
            _sd = _rd.get("scope_disclosure")
            if _sd:
                _scope_notes.append(str(_sd))
        _reconciliation_disclosures.extend(reconcile_fingerprints(_claimed))
        _reconciliation_disclosures.extend(_family_notes)
        _reconciliation_disclosures.extend(_scope_notes)
    except Exception as _recon_err:  # pragma: no cover — never break synthesis
        logger.debug("synthesize: reconciliation check skipped: %s", _recon_err)
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
            disclosures=_reconciliation_disclosures or None,
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


def resolve_deliverable_format(
    step_type: str | None,
    *,
    is_terminal: bool,
    intents: list[str] | None,
) -> tuple[str, str | None]:
    """Reconcile a plan step's artifact type with the user's delivery intent.

    ``output_artifact_type`` is free-form LLM output — ``plan_dag`` stores the
    step's string verbatim, and the planner names the SHAPE of what a step
    computes ("dataframe", "metrics_table") rather than what the user receives.
    Measured live on 2026-09-20: the request was 用图表展示, the task spec
    recorded ``artifact_intents=[sales_chart, sales_report, data_visualization]``,
    and the planner wrote ``dataframe`` on the sandbox step. The sandbox then
    correctly resolved dataframe -> xlsx, and the turn's only artifact was a
    spreadsheet — the chart never existed, while the answer prose said it had
    been generated.

    For the LAST artifact step the two must agree, and the delivery intent wins:
    it is what the user asked for. Intermediate steps keep their own type, since
    a dataframe feeding a later chart step is legitimate.

    Returns ``(format, overridden_intent)``; the second element is the intent
    that overrode the step type, for logging, or None when nothing changed.
    """
    from app.services.tool_handlers.sandbox_tool import (
        _IMAGE_BY_FORMAT as _generator_formats,
        normalize_format,
    )

    step_fmt = normalize_format(step_type or "") or "docx"
    if not is_terminal or not intents:
        return step_fmt, None
    intent_fmt = normalize_format(intents[0])
    if intent_fmt in _generator_formats and intent_fmt != step_fmt:
        return intent_fmt, intents[0]
    return step_fmt, None


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

    # ── The terminal deliverable must be the format the user asked for ────
    # ``output_artifact_type`` is free-form LLM output (plan_dag stores the
    # step's string verbatim). Observed live: the user asked 用图表展示, the
    # task spec recorded artifact_intents=[sales_chart, sales_report,
    # data_visualization], and the planner wrote "dataframe" on the sandbox
    # step. The sandbox faithfully resolved dataframe->xlsx and the turn's only
    # artifact was a spreadsheet — the chart never existed, while the answer
    # prose said it had been generated. The shape word described what the step
    # COMPUTES; task_spec.artifact_intents is what the user RECEIVES. For the
    # last artifact step they must agree, and the delivery intent wins.
    # Intermediate steps keep their own type (a dataframe feeding a later chart
    # step is legitimate). Reuses normalize_format so there is ONE mapping.
    if getattr(node, "node_type", None) == "sandbox":
        try:
            later = (
                db.query(PlanNode)
                .filter(
                    PlanNode.plan_id == node.plan_id,
                    PlanNode.seq > node.seq,
                    PlanNode.node_type == "sandbox",
                )
                .first()
            )
            _intents = (execution.task_spec or {}).get("artifact_intents") or []
            _fmt, _overrode = resolve_deliverable_format(
                artifact_type, is_terminal=(later is None), intents=_intents,
            )
            if _overrode:
                logger.info(
                    "sandbox node %r: delivery intent %r overrides step type %r "
                    "(terminal deliverable — the user asked for %s)",
                    node.name, _overrode, artifact_type, _fmt,
                )
            artifact_type = _fmt
        except Exception as e:  # never let intent reconciliation lose the step
            logger.warning("sandbox node: artifact-type reconciliation skipped: %s", e)

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

    # ── No synthesize node? Use the rows the data step actually returned ───
    # Without this the ``data`` argument fell through to the literal
    # [{"note": "See generated report for details"}] placeholder. Measured live
    # (2026-09-20): a plan of nl2sql -> sandbox produced report.xlsx whose sheet1
    # was 'note | See generated report for details' and whose html preview was a
    # Plotly template with no series — an EMPTY artifact delivered as the turn's
    # chart, while the answer prose reported KPIs that the model had computed
    # from the rows itself. The rows were there the whole time: the synthesize
    # node reads them via _get_all_data_observations, and this node did not.
    if not chart_rows:
        for _obs in _get_all_data_observations(db, execution):
            if not getattr(_obs, "success", False):
                continue
            _rd = getattr(_obs, "result_data", None)
            if not isinstance(_rd, dict):
                continue
            _data = _rd.get("data")
            if isinstance(_data, list):
                chart_rows.extend(_data)
        if chart_rows:
            logger.info(
                "sandbox node %r: no synthesize step — rendering %d row(s) from "
                "the nl2sql observation(s) instead of the placeholder",
                node.name, len(chart_rows),
            )
        else:
            logger.warning(
                "sandbox node %r: no rows from synthesize OR nl2sql — the "
                "artifact will contain the placeholder note only",
                node.name,
            )

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
                "org_id": getattr(execution, "org_id", None),
                # Tenant isolation: bare agent_name is org-ambiguous; use
                # the org-resolved memory key (identity.memory_write_key).
                "agent_app_id": _fsm_exec_memory_key(db, execution),
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
    # FSM tenant isolation (IC-2): org + org-resolved memory key — same
    # contract as the tool-node path above.
    tool_context = {
        "conversation_id": getattr(execution, "conversation_id", None),
        # Execution identity lets the artifact path ground a deck in the REAL
        # query rows this execution fetched (see deck_data.collect_grounded_rows).
        "execution_id": getattr(execution, "id", None),
        "org_id": getattr(execution, "org_id", None),
        "agent_app_id": _fsm_exec_memory_key(db, execution)
        or getattr(execution, "agent_name", None),
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


def _extract_evidence_ids(result_data) -> list | None:
    """Phase 3.1: best-effort evidence-reference extraction from a tool
    result so ObservationRecord provenance is restorable after a restart.
    Recognizes the shared evidence-registry payload shapes; returns None
    when nothing is found (column stays NULL — never fabricated)."""
    if not isinstance(result_data, dict):
        return None
    for key in ("evidence_ids", "evidence_refs", "citations"):
        val = result_data.get(key)
        if isinstance(val, list) and val:
            return [str(x) for x in val][:50]
    ids = result_data.get("evidence_id")
    if ids:
        return [str(ids)]
    return None


def _record_observation(
    db, execution, node,
    observation_type, tool_name, request_args=None,
    result_data=None, result_text=None, success=True,
    error_message=None, artifact_ids=None, evidence_ids=None,
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
        # Phase 3.1: durable evidence references (restorable provenance
        # after restart). Best-effort extraction from result_data when the
        # caller did not pass them explicitly.
        evidence_ids=evidence_ids if evidence_ids is not None else _extract_evidence_ids(result_data),
    )
    db.add(obs)
    db.commit()
    db.refresh(obs)
    return obs


def _topological_sort(nodes) -> list:
    """Sort plan nodes in topological order (dependencies first).

    FIX 2026-09-22: a dependency CYCLE used to raise ValueError and kill the
    whole turn ("FSM execution failed: Dependency cycle") — for a defect that
    is always salvageable. Self-dependencies and back-edges are now BROKEN
    here (the offending dependency is removed from the node and the mutation
    persists with the executor's commit), so every authored plan runs:
    nodes are never removed, only impossible edges.
    """
    # Build node index by seq
    node_map = {n.seq: n for n in nodes}
    visited = set()   # fully explored (black)
    visiting = set()  # in current DFS path (gray)
    result = []

    # Pre-pass: drop self-dependencies outright.
    for n in nodes:
        deps = n.dependencies or []
        if n.seq in deps:
            n.dependencies = [d for d in deps if d != n.seq]

    def visit(seq):
        if seq in visiting:
            # Cycle: break it — remove seq from the dependencies of every
            # node on the current DFS path that points back at it.
            for s in list(visiting):
                n = node_map.get(s)
                if n and seq in (n.dependencies or []):
                    n.dependencies = [
                        d for d in (n.dependencies or []) if d != seq
                    ]
            visiting.discard(seq)
            return
        if seq in visited:
            return
        visiting.add(seq)
        node = node_map.get(seq)
        if not node:
            visiting.discard(seq)
            return
        for dep in list(node.dependencies or []):
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
