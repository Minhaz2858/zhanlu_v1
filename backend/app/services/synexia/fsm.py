"""Synexia FSM — the cognitive loop state machine.

Replaces the raw LLM tool-calling loop with a governed pipeline:

    INIT → GOAL → CONTEXT → PLAN → GATE → ACT → OBSERVE → VERIFY → FINALIZE

The FSM wraps the existing `execute_tool` function — tool behavior is
unchanged, but the FSM adds plan/gate/observe/verify AROUND the execution.

Feature flag: SYNEXIA_FSM_ENABLED (default False).  When False, the
existing raw tool loop runs.  When True, the FSM orchestrates.
"""

import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import settings
from app.models.execution import Execution, Plan, PlanNode, ObservationRecord, FSM_STATES
from app.services.planning_trigger import is_followup_refinement as _is_followup_refinement

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# File-intent detection — used in FINALIZE to decide whether to create an
# artifact (only when user explicitly asked for a file).
# ---------------------------------------------------------------------------
_FILE_INTENT_KEYWORDS = (
    # English — file formats and file-action verbs
    "docx", "pptx", "pdf", "xlsx", "excel", "word",
    "powerpoint", "slide", "deck", "document",
    "download", "export", "as a file", "as a doc",
    "as a deck", "save as", "send me the file",
    # Chinese — file-action phrases
    "做成", "生成", "导出", "下载", "存为", "发我",
    "做成文件", "生成报告", "导出报表", "下载报告",
)


def _contains_file_intent(message: str) -> bool:
    """True if the user explicitly asked for a file deliverable.

    Conservative: returns True only when the message carries an unambiguous
    file-format or file-action cue.  Inline data-analysis questions
    ("i want July 2026 sales report") return False.
    """
    text = (message or "").lower()
    return any(kw in text for kw in _FILE_INTENT_KEYWORDS)


class FSMState(str, Enum):
    INIT = "init"
    GOAL = "goal"
    CONTEXT = "context"
    PLAN = "plan"
    GATE = "gate"
    ACT = "act"
    OBSERVE = "observe"
    VERIFY = "verify"
    FINALIZE = "finalize"
    QUALITY_EVAL = "quality_eval"
    DONE = "done"
    FAIL = "fail"


class ExecutionRequest(BaseModel):
    """Input to the FSM — replaces raw user message in add_message."""
    conversation_id: Optional[str] = None
    agent_name: str = "general_assistant"
    user_message: str
    user_id: Optional[str] = None
    attachments: list[str] = Field(default_factory=list)
    mode: str = "dynamic"  # "dynamic" | "frozen"
    org_id: str = "default-org"
    app_id: str = "default-app"
    conversation_context: Optional[dict] = None
    # Pre-built follow-up context (transcript + recent_artifacts +
    # prior_entities). When supplied by the chat-loop router, _run_goal
    # reuses it instead of calling build_conversation_context again —
    # avoiding a duplicate DB query on follow-up turns that the router
    # already detected. Defaults to None (build it inside _run_goal).
    # Data-source runtime context (e.g. ``bound_kb_ids``) computed by
    # the chat router. The FSM merges this into the per-tool-call
    # context so ask_data_agent / list_data_sources see the same
    # scoped KB list the v2 tool loop uses. Without this, the FSM
    # path silently drops project-scoped KB bindings and the agent
    # reports "no data sources bound" even when the project has a
    # connected database.
    data_ctx_extras: Optional[dict] = None
    # Skill explicitly selected by the user in chat. This is authoritative
    # routing context: selected skills override default-skill auto-pick, but
    # still pass through normal permission/policy/tool gates downstream.
    selected_skill: Optional[dict] = None
    # True when this run originates from a scheduled automation (the SSE
    # path sets it from the ``force_planning`` body flag, which only the
    # automation executor sends). Drives the stricter quality-gate
    # threshold for unattended runs (0.6 vs 0.4 for interactive chat).
    is_automation: bool = False
    # Resolved hierarchical LLM endpoint (project/agent → llm_models pin).
    # When set, EVERY LLM call inside the FSM (GOAL task-spec parse, PLAN
    # generation, ACT tool/nl2sql/synthesize nodes, VERIFY rubric,
    # QUALITY_EVAL, FINALIZE response + streaming) targets this exact
    # provider+model instead of the legacy .env defaults.  The chat-loop
    # routers resolve it via ``resolve_effective_llm`` and attach it here.
    endpoint: Optional[Any] = None


class ExecutionResult(BaseModel):
    """Output of the FSM — what add_message returns to frontend."""
    execution_id: str
    assistant_content: str = ""
    tool_calls: list[dict] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    confidence_factors: Optional[dict] = None
    plan_summary: Optional[dict] = None
    state: str = "done"
    report_card_payload: Optional[dict] = Field(
        default=None,
        description="The ReportCardPayload dict (consumed by ReportCard.jsx).",
    )
    file_exports: dict[str, dict] = Field(
        default_factory=dict,
        description=(
            "Dict keyed by format (e.g. 'docx') with artifact_id, "
            "preview_url, download_url.  The chat loop propagates this "
            "to the frontend's file_exports field."
        ),
    )
    export_artifact_id: Optional[str] = Field(
        default=None,
        description="The file-export artifact id (docx/pptx/etc.), if different from artifact_ids.",
    )
    quality_gate: Optional[dict] = Field(
        default=None,
        description=(
            "Phase B quality-gate outcome. Present when artifacts were "
            "produced; passed=False means they were held back (low "
            "confidence) and are listed in held_artifact_ids."
        ),
    )
    quality_eval: Optional[dict] = Field(
        default=None,
        description=(
            "QUALITY_EVAL (Tier 2) outcome — combined completeness + "
            "reflexion verdict on the generated response, with the "
            "corrective-loop iteration count. Present when the QUALITY_EVAL "
            "phase ran (SYNEXIA_QUALITY_EVAL_ENABLED)."
        ),
    )


class SynexiaFSM:
    """The cognitive loop state machine.

    Orchestrates the INIT→GOAL→CONTEXT→PLAN→GATE→ACT→OBSERVE→VERIFY→FINALIZE
    pipeline, creating Execution/Plan/ObservationRecord records along the way.
    """

    def __init__(self, db: Session):
        self.db = db
        self.execution: Optional[Execution] = None
        self.plan: Optional[Plan] = None
        # Most recent VERIFY result — used by the VERIFY-driven re-plan loop
        # to decide whether to loop back to PLAN. Reset each run().
        self._last_verify_result = None

    def run(
        self,
        request: ExecutionRequest,
        on_state_change=None,
        generate_response: bool = True,
        on_plan_node=None,
        on_verify=None,
    ) -> ExecutionResult:
        """Run the full FSM pipeline for a user message.

        Args:
            request: The execution request.
            on_state_change: Optional callback invoked after each state
                transition. Receives the new state value (str). Used by the
                SSE path in ``add_message_stream`` to yield ``fsm_state``
                events. Exceptions in the callback are caught and logged.
            on_plan_node: Optional callback ``(node_dict, status, detail)``
                invoked per plan-node lifecycle transition during ACT/OBSERVE
                (running/completed/failed/skipped/denied/replanning). Used by
                the SSE path to surface per-node ``activity_step`` events so a
                watcher sees the plan executing — and self-correcting via the
                ACT re-plan — step by step (the Manus-style activity feed).
                Exceptions in the callback are swallowed by the router.
            on_verify: Optional callback ``(passed: bool, result)`` invoked
                after VERIFY with the deterministic VerifyResult. Lets the SSE
                path emit an HONEST verdict — ``verify_passed`` only when the
                checks really passed, ``verify_failed`` otherwise — instead of
                unconditionally claiming success whenever FINALIZE is entered.
        """
        # INIT — create execution record
        self.execution = Execution(
            id=str(uuid4()),
            conversation_id=request.conversation_id,
            agent_name=request.agent_name,
            user_message=request.user_message,
            current_state=FSMState.INIT.value,
            mode=request.mode,
            started_at=datetime.now(timezone.utc),
            org_id=request.org_id,
            app_id=request.app_id,
        )
        self.db.add(self.execution)
        self.db.commit()
        self.db.refresh(self.execution)
        logger.info("Execution %s created (agent=%s)", self.execution.id, request.agent_name)
        # Fire the init callback too (callers can treat it as "execution started").
        if on_state_change is not None:
            try:
                on_state_change(FSMState.INIT.value)
            except Exception as _cb_err:
                logger.warning("FSM run() init callback raised (non-fatal): %s", _cb_err)

        try:
            # GOAL — parse user message into TaskSpec
            self._transition(FSMState.GOAL, on_state_change=on_state_change)
            self._run_goal(request)

            # CONTEXT — assemble context manifest
            self._transition(FSMState.CONTEXT, on_state_change=on_state_change)
            self._run_context(request)

            # PLAN→GATE→ACT→VERIFY with a bounded VERIFY-driven re-plan loop.
            # When a CRITICAL deterministic check fails AND the active plan has
            # failed tool/skill nodes, the FSM loops back to PLAN with the
            # failures as context (up to SYNEXIA_VERIFY_REPLAN_MAX times).
            # Data-pipeline-only failures never trigger a re-plan.
            verify_budget = getattr(settings, "SYNEXIA_VERIFY_REPLAN_MAX", 1)
            failure_context: Optional[list] = None
            for attempt in range(verify_budget + 1):
                self._transition(FSMState.PLAN, on_state_change=on_state_change)
                self._run_plan(
                    request, failure_context=failure_context, plan_version=attempt + 1,
                )

                # GATE — evaluate policy on the plan
                self._transition(FSMState.GATE, on_state_change=on_state_change)
                self._run_gate(request)

                # ACT + OBSERVE — execute plan nodes
                self._transition(FSMState.ACT, on_state_change=on_state_change)
                self._run_act_observe(request, on_plan_node=on_plan_node)

                # VERIFY — validate outputs
                self._transition(FSMState.VERIFY, on_state_change=on_state_change)
                self._run_verify(request, on_verify=on_verify)

                if attempt < verify_budget and self._should_verify_replan():
                    failure_context = self._build_verify_replan_context()
                    logger.info(
                        "VERIFY critical failure (attempt %d/%d) — re-planning",
                        attempt + 1, verify_budget,
                    )
                    continue
                break

            # FINALIZE — compute confidence, prepare result
            self._transition(FSMState.FINALIZE, on_state_change=on_state_change)
            return self._run_finalize(
                request,
                generate_response=generate_response,
                on_state_change=on_state_change,
            )

        except Exception as e:
            logger.error("FSM execution failed: %s", e)
            self._transition(FSMState.FAIL, on_state_change=on_state_change)
            self.execution.error_message = str(e)
            self.execution.current_state = FSMState.FAIL.value
            self.db.commit()
            return ExecutionResult(
                execution_id=self.execution.id,
                assistant_content=f"I encountered an error while processing your request: {e}",
                state=FSMState.FAIL.value,
            )

    def _transition(self, new_state: FSMState, on_state_change=None):
        """Transition to a new FSM state.

        Args:
            new_state: The destination state.
            on_state_change: Optional callback invoked AFTER the state is
                committed. Receives the new state value (str) as its single
                argument. Used by the SSE path in ``add_message_stream`` to
                yield an ``fsm_state`` event per transition. Exceptions in
                the callback are caught and logged — a misbehaving callback
                must never corrupt the FSM pipeline.
        """
        old_state = self.execution.current_state
        self.execution.current_state = new_state.value
        self.db.commit()
        logger.debug("FSM transition: %s → %s (execution=%s)", old_state, new_state.value, self.execution.id)
        if on_state_change is not None:
            try:
                on_state_change(new_state.value)
            except Exception as _cb_err:
                logger.warning("FSM _transition callback raised (non-fatal): %s", _cb_err)

    def _run_goal(self, request: ExecutionRequest):
        """GOAL state — parse user message into a TaskSpec using LLM."""
        from app.services.synexia.task_spec_parser import parse_task_spec
        from app.services.synexia.context_assembler import build_conversation_context

        # Build follow-up context BEFORE parsing so GOAL can detect
        # refinement intent and inherit entities.  GOAL runs before
        # CONTEXT, so we build the context here (non-fatal) rather than
        # relying on the CONTEXT state's manifest.  Without this, every
        # turn is parsed as a brand-new, context-free request.
        #
        # If the chat-loop router already built the context (follow-up
        # override path), reuse it to avoid a duplicate DB query.
        conv_ctx = request.conversation_context
        if conv_ctx is None:
            conv_ctx = build_conversation_context(
                self.db, request.conversation_id, request.agent_name,
            )

        task_spec = parse_task_spec(
            request.user_message,
            request.agent_name,
            active_skill=request.selected_skill,
            conversation_context=conv_ctx or None,
            db=self.db,
            endpoint=request.endpoint,
        )

        # ── Follow-up reuse: if prior turn has answer datasets and the
        # current request is a refinement (summary/breakdown/explain),
        # set reuse_prior_data so PLAN generates a synthesis-only node
        # (no re-query needed, ~5s instead of ~35s).
        if conv_ctx and conv_ctx.get("prior_datasets"):
            _is_refinement = _is_followup_refinement(request.user_message)
            if _is_refinement:
                task_spec["reuse_prior_data"] = True
                task_spec["prior_datasets"] = conv_ctx["prior_datasets"]
                logger.info(
                    "GOAL: follow-up refinement detected — reuse_prior_data=True "
                    "(%d prior datasets, conv=%s)",
                    len(conv_ctx["prior_datasets"]),
                    request.conversation_id,
                )

        self.execution.task_spec = task_spec
        self.db.commit()
        logger.info("TaskSpec parsed: task_kind=%s, reuse_prior_data=%s",
                     task_spec.get("task_kind", "unknown"),
                     task_spec.get("reuse_prior_data", False))

    def _run_context(self, request: ExecutionRequest):
        """CONTEXT state — assemble context manifest (memory, KB, attachments)."""
        from app.services.synexia.context_assembler import assemble_context

        context = assemble_context(
            db=self.db,
            conversation_id=request.conversation_id,
            agent_name=request.agent_name,
            user_message=request.user_message,
            task_spec=self.execution.task_spec,
            # Phase 1: forward the user's uploaded file_urls so the
            # context assembler can extract their text and fold it into
            # the LLM prompt. Without this, ExecutionRequest.attachments
            # was declared but never read — the FSM silently dropped
            # everything the user uploaded.
            attachments=request.attachments,
        )
        self.execution.context_manifest = context
        self.db.commit()
        logger.info("Context assembled: %d items", len(context.get("items", [])))

    def _run_plan(
        self,
        request: ExecutionRequest,
        failure_context: Optional[list] = None,
        plan_version: int = 1,
    ):
        """PLAN state — generate a PlanDAG using LLM.

        When ``failure_context`` is provided (a VERIFY-driven re-plan), the
        planner is asked for a *corrective* plan that recovers from the
        listed failures instead of planning from scratch.
        """
        from app.services.synexia.plan_dag import generate_plan

        # The TaskSpec parsed by GOAL carries the LLM's classification
        # (task_kind, entities, …) but NOT the raw user message — the
        # LLM's JSON schema doesn't echo it back.  The planner needs the
        # original text to build a usable nl2sql node (inputs.question);
        # without it the node executes with question="" and fails with
        # "Question is required", which cascades into a VERIFY failure
        # and the agent claiming it has no data access.  The execution
        # row has the message — inject it here for both the default and
        # LLM planner paths.
        if self.execution.task_spec:
            self.execution.task_spec["user_message"] = (
                self.execution.task_spec.get("user_message") or request.user_message
            )

        self.plan = generate_plan(
            db=self.db,
            execution_id=self.execution.id,
            task_spec=self.execution.task_spec,
            context_manifest=self.execution.context_manifest,
            agent_name=request.agent_name,
            failure_context=failure_context,
            plan_version=plan_version,
            endpoint=request.endpoint,
        )
        logger.info(
            "Plan generated (v%s%s): %d nodes",
            plan_version,
            " corrective" if failure_context else "",
            len(self.plan.nodes) if self.plan else 0,
        )

    def _run_gate(self, request: ExecutionRequest):
        """GATE state — evaluate policy on the plan (whole-plan check)."""
        from app.services.synexia.policy_evaluator import evaluate_plan

        decision = evaluate_plan(
            plan=self.plan,
            task_spec=self.execution.task_spec,
            agent_name=request.agent_name,
        )
        self.execution.policy_decision = decision
        self.db.commit()
        logger.info("Policy decision: %s (risk=%s)", decision.get("decision", "allow"), decision.get("risk_tier", "low"))

    def _run_act_observe(self, request: ExecutionRequest, on_plan_node=None):
        """ACT + OBSERVE states — execute plan nodes and record observations.

        ``on_plan_node`` (when supplied by the caller) is forwarded to
        ``execute_plan_nodes`` so per-node lifecycle transitions are surfaced
        as activity steps (the Manus-style step-by-step activity feed).
        """
        from app.services.synexia.capability_router import execute_plan_nodes

        observations = execute_plan_nodes(
            db=self.db,
            execution=self.execution,
            plan=self.plan,
            user_id=request.user_id,
            data_ctx_extras=request.data_ctx_extras,
            on_plan_node=on_plan_node,
            endpoint=request.endpoint,
        )
        logger.info("Executed %d plan nodes, %d observations", len(self.plan.nodes) if self.plan else 0, len(observations))

    def _load_evaluation_profile(self, request: ExecutionRequest) -> Optional[dict]:
        """Return the agent's ``evaluation_profile`` dict, or None.

        Looks up ``AgentApp`` by ``request.app_id`` (preferred) or
        ``request.agent_name`` as fallback. Returns the JSON column
        unchanged. Best-effort: any failure (table missing, agent not
        found, malformed JSON) returns None and logs a warning. Never
        raises.
        """
        if not getattr(self.db, "execute", None):
            return None
        try:
            # Late import so a missing models package never breaks FSM init.
            from app.models.agent_app import AgentApp
        except Exception as _imp_err:
            logger.debug("VERIFY: AgentApp model unavailable: %s", _imp_err)
            return None
        try:
            agent = None
            if getattr(request, "app_id", None) and request.app_id != "default-app":
                agent = self.db.query(AgentApp).filter(
                    AgentApp.id == request.app_id,
                ).first()
            if agent is None and getattr(request, "agent_name", None):
                agent = self.db.query(AgentApp).filter(
                    AgentApp.name == request.agent_name,
                ).first()
            if agent is None:
                return None
            profile = getattr(agent, "evaluation_profile", None)
            return profile if isinstance(profile, dict) else None
        except Exception as _lookup_err:
            logger.warning(
                "VERIFY: evaluation_profile lookup failed (non-fatal): %s",
                _lookup_err,
            )
            return None

    def _run_verify(self, request: ExecutionRequest, on_verify=None):
        """VERIFY state — validate outputs (artifacts, data integrity).

        Calls the deterministic validator in :mod:`app.services.synexia.verifier`
        and, when ``SYNEXIA_VERIFIER_LLM_ENABLED`` is on, augments the result
        with an LLM rubric pass. When the agent has an
        ``evaluation_profile`` with ``grounding_checks`` configured (P2),
        those checks are also run deterministically and merged in. The
        combined result is persisted to
        ``self.execution.confidence_factors["verification"]`` so the downstream
        confidence scorer can use it.

        VERIFY is intentionally non-fatal: a ``passed=False`` result is
        information, not a gate. Partial results are still useful and the
        confidence score will reflect the issues.

        ``on_verify(passed, result)`` is invoked after the checks run so the
        SSE path can surface an honest verdict to the user.
        """
        from app.services.synexia.verifier import (
            verify_execution,
            verify_with_llm,
            verify_grounding,
        )

        result = verify_execution(self.db, self.execution, plan=self.plan)
        # Stash for the re-plan decision in run(). ``result.checks`` is mutated
        # in place by the grounding/LLM extensions below, and this is a
        # reference, so critical_failed() reflects the final check set.
        self._last_verify_result = result

        # P2: load the agent's evaluation_profile and run grounding_checks
        # if configured. Best-effort: a missing/broken profile must never
        # break VERIFY (same convention as the rest of the FSM).
        evaluation_profile = self._load_evaluation_profile(request)
        if evaluation_profile:
            try:
                grounding_checks = verify_grounding(self.execution, evaluation_profile)
                if grounding_checks:
                    result.checks.extend(grounding_checks)
            except Exception as _grounding_err:
                logger.warning(
                    "VERIFY: grounding_checks raised (non-fatal): %s",
                    _grounding_err,
                )

        llm_checks = verify_with_llm(self.execution, result, endpoint=request.endpoint)
        if llm_checks:
            result.checks.extend(llm_checks)

        factors = dict(self.execution.confidence_factors or {})
        factors["verification"] = result.to_dict()
        self.execution.confidence_factors = factors
        self.db.commit()

        overall_ok = result.all_checks_passed()
        if not overall_ok:
            logger.warning(
                "VERIFY did not pass (execution=%s). Checks: %s",
                self.execution.id,
                [(c["check"], c["ok"]) for c in result.checks],
            )
        # Honest verdict for the SSE path: callers may surface
        # verify_passed / verify_failed to the user. Never raise.
        if on_verify is not None:
            try:
                on_verify(overall_ok, result)
            except Exception as _verify_cb_err:
                logger.warning("FSM on_verify callback raised (non-fatal): %s", _verify_cb_err)
        # Don't fail the whole execution — partial results are still useful
        # (preserved historical behavior of the stub).

    def _should_verify_replan(self) -> bool:
        """Decide whether VERIFY should drive a corrective re-plan.

        True only when ALL hold:
          - the latest VERIFY had a CRITICAL check failure,
          - the active plan has at least one failed tool/skill node
            (the recoverable kind) — data-pipeline-only failures are left
            alone per project scope,
          - the failed tool/skill node is not a policy denial (re-planning a
            denied action would just re-deny it).
        """
        vr = getattr(self, "_last_verify_result", None)
        if vr is None or not getattr(vr, "critical_failed", False):
            return False
        if not self.plan or not self.plan.nodes:
            return False
        return any(
            n.status == "failed"
            and n.node_type in ("tool", "skill")
            and not (n.error or "").startswith("Policy denied")
            for n in self.plan.nodes
        )

    def _build_verify_replan_context(self) -> list[dict]:
        """Build the failure_context for a VERIFY-driven corrective re-plan."""
        ctx: list[dict] = []
        for n in (self.plan.nodes or []):
            if (
                n.status == "failed"
                and n.node_type in ("tool", "skill")
                and not (n.error or "").startswith("Policy denied")
            ):
                ctx.append({
                    "name": n.name,
                    "node_type": n.node_type,
                    "error": n.error or "unknown error",
                })
        return ctx

    def _run_reflexion(self, request: ExecutionRequest, assistant_text: str) -> None:
        """Run reflexion self-critique on the FINALIZE response.

        Uses the heuristic fallback (sync, no LLM cost) from
        ``app.services.synexia.reflexion`` to detect failure markers in
        the generated response. Stores the verdict in
        ``confidence_factors["reflexion"]`` and penalizes the confidence
        score for non-accept verdicts so downstream quality gates can
        catch low-quality outputs. Never raises.
        """
        from app.services.synexia.reflexion import _fallback_verdict

        try:
            verdict = _fallback_verdict(assistant_text)
            factors = dict(self.execution.confidence_factors or {})
            factors["reflexion"] = {
                "verdict": verdict.verdict,
                "confidence": verdict.confidence,
                "issues": verdict.issues,
            }
            self.execution.confidence_factors = factors

            if not verdict.is_ok:
                penalty = 0.15 if verdict.verdict == "reject" else 0.08
                self.execution.confidence_score = max(
                    0.0, (self.execution.confidence_score or 0.5) - penalty
                )
                logger.info(
                    "Reflexion flagged %s verdict (penalty=%.2f): %s",
                    verdict.verdict, penalty, verdict.issues,
                )

            self.db.commit()
        except Exception as e:
            logger.warning("Reflexion failed (non-fatal): %s", e)

    def run_quality_eval(
        self,
        request: ExecutionRequest,
        assistant_text: str,
        *,
        on_state_change=None,
    ):
        """Run the QUALITY_EVAL phase (Tier 2 — Approach C).

        A combined completeness + reflexion LLM critique on the generated
        response, with a bounded corrective re-generation loop.  Re-generates
        the response **text only** (no tool re-execution) — mirroring how
        Claude Code / Manus revise the writing instead of re-running the
        investigation.

        Args:
            request: The execution request (for user_message + task_spec).
            assistant_text: The generated response to critique.
            on_state_change: Optional SSE callback.  When supplied (blocking
                path), transitions through the QUALITY_EVAL state so a
                watcher sees the phase.  When None (streaming post-stream
                path), no transition is emitted (run() already returned).

        Returns:
            A ``QualityEvalResult`` (with ``final_text`` possibly revised),
            or ``None`` when QUALITY_EVAL is disabled or failed (non-fatal —
            the caller falls back to the heuristic ``_run_reflexion``).

        Cost: 0 extra LLM calls on a clean accept; up to
        ``1 + max_iterations*2`` calls on a bad output.
        """
        from app.services.synexia.quality_eval import run_quality_loop

        if not getattr(settings, "SYNEXIA_QUALITY_EVAL_ENABLED", True):
            return None

        if on_state_change is not None:
            self._transition(FSMState.QUALITY_EVAL, on_state_change=on_state_change)

        task_spec = self.execution.task_spec or {}
        response_prompt = self._build_response_prompt(request)
        max_iter = getattr(settings, "SYNEXIA_QUALITY_EVAL_MAX_ITERATIONS", 2)

        # Bridge to the sync call_llm (which already has retry/backoff/
        # failover — P0 DONE 2026-07-29).  run_quality_loop injects this.
        # run_quality_eval is invoked via asyncio.to_thread() in the
        # streaming path (agents.py), so we are in a worker thread: a
        # fresh event loop is always safe.  (Fixed 2026-08-17 — the
        # previous version returned the coroutine object without awaiting,
        # so every FSM quality eval silently fell back to the heuristic.)
        def _llm_call(prompt, messages, **kwargs):
            import asyncio
            from app.services.llm_service import call_llm

            result = call_llm(
                prompt=prompt,
                messages=messages,
                temperature=kwargs.get("temperature", 0),
                endpoint=request.endpoint,
            )
            # Production call_llm is async → run it in a fresh loop (we are
            # in a worker thread via asyncio.to_thread).  Tests may inject a
            # plain sync stub returning a dict → pass it through untouched.
            if not asyncio.iscoroutine(result):
                return result
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(result)
            finally:
                try:
                    loop.close()
                except Exception:  # pragma: no cover — loop teardown best-effort
                    pass

        try:
            qer = run_quality_loop(
                user_message=request.user_message,
                initial_text=assistant_text,
                task_spec=task_spec,
                response_prompt=response_prompt,
                max_iterations=max_iter,
                llm_call=_llm_call,
            )
        except Exception as e:
            logger.warning("QUALITY_EVAL failed (non-fatal): %s", e)
            return None

        # Persist the verdict (parity with _run_reflexion's storage pattern).
        factors = dict(self.execution.confidence_factors or {})
        factors["quality_eval"] = qer.to_dict()
        self.execution.confidence_factors = factors

        # Penalize confidence for non-accept verdicts so the downstream
        # quality gate can catch low-quality outputs (same convention as
        # _run_reflexion).
        if not qer.is_ok:
            penalty = 0.15 if qer.verdict == "reject" else 0.08
            self.execution.confidence_score = max(
                0.0, (self.execution.confidence_score or 0.5) - penalty
            )

        self.db.commit()
        logger.info(
            "QUALITY_EVAL: verdict=%s completeness=%.2f iterations=%d",
            qer.verdict, qer.completeness_score, qer.iterations,
        )
        return qer

    def _run_finalize(
        self, request: ExecutionRequest, *, generate_response: bool = True,
        on_state_change=None,
    ) -> ExecutionResult:
        """FINALIZE state — compute confidence score, prepare result.

        For file-format requests, builds a ReportCardPayload from the
        observations, persists it as an Artifact via ``finalize_into_artifact``,
        and attaches the file exports + artifact_id to the result so the
        chat loop can propagate them to the frontend.

        When ``generate_response`` is False (used by the SSE streaming path),
        the FINALIZE response prompt is built and stashed on
        ``self._deferred_response_prompt`` and ``assistant_content`` is left
        empty — the caller streams the response token-by-token via
        :meth:`stream_final_response` after FINALIZE returns. Everything else
        (confidence, report card, file exports) is computed as usual.
        """
        from app.services.synexia.confidence_scorer import compute_confidence
        from app.services.synexia.finalize import fsm_finalize_into_artifact

        confidence, factors = compute_confidence(
            execution=self.execution,
            plan=self.plan,
        )
        self.execution.confidence_score = confidence
        self.execution.confidence_factors = factors

        # Generate assistant response from observations. When generate_response
        # is False (SSE path), the response prompt is built and stashed for the
        # caller to stream token-by-token, and assistant_content is left empty
        # here — it is filled in by the streaming path after FINALIZE returns.
        if generate_response:
            assistant_content = self._generate_response(request)
            # QUALITY_EVAL (Tier 2 — Approach C): combined completeness +
            # reflexion LLM critique with a bounded corrective
            # re-generation loop.  Supersedes the heuristic-only
            # _run_reflexion when enabled; falls back to it when disabled.
            _qer = self.run_quality_eval(
                request, assistant_content, on_state_change=on_state_change,
            )
            if _qer is not None:
                # The corrective loop may have revised the response text.
                if _qer.final_text:
                    assistant_content = _qer.final_text
            else:
                # Disabled — heuristic reflexion only (existing behaviour).
                self._run_reflexion(request, assistant_content)
            # Re-read confidence after reflexion / QUALITY_EVAL penalty.
            confidence = self.execution.confidence_score or confidence
        else:
            self._deferred_response_prompt = self._build_response_prompt(request)
            assistant_content = ""

        self.execution.assistant_content = assistant_content
        self.execution.current_state = FSMState.DONE.value
        self.execution.completed_at = datetime.now(timezone.utc)
        self.db.commit()

        # Collect tool calls for frontend compatibility
        tool_calls = []
        artifact_ids = []
        report_card_payload = None
        file_exports: dict[str, dict] = {}
        export_artifact_id = None

        observations = list(self.execution.observations or [])
        task_spec = self.execution.task_spec or {}

        for obs in observations:
            if obs.observation_type == "tool_call":
                tool_calls.append({
                    "name": obs.tool_name,
                    "args": obs.request_args,
                    "result": obs.result_data,
                    "success": obs.success,
                })
            if obs.artifact_ids:
                artifact_ids.extend(obs.artifact_ids)

        # ── Gate: suppress artifacts when user did not request a file ──
        # Inline data-analysis questions ("give me sales numbers") should
        # NOT produce artifact cards / Preview buttons.  Only ship artifacts
        # when the user explicitly asked for a file format (docx/pptx/…).
        _user_requested_file = bool(
            (task_spec.get("deliverable_format") if task_spec else None)
            or _contains_file_intent(request.user_message)
        )
        if not _user_requested_file and artifact_ids:
            logger.info(
                "FINALIZE: suppressing %d artifact(s) — user did not request "
                "a file (conv=%s, msg=%.60s)",
                len(artifact_ids), request.conversation_id,
                (request.user_message or "")[:60],
            )
            artifact_ids = []

        # ── File-format finalize: persist artifact + emit file_exports ──
        user_signal = task_spec.get("user_signal", "default")
        if user_signal.startswith("export_"):
            try:
                result = fsm_finalize_into_artifact(
                    self.db,
                    conversation_id=request.conversation_id,
                    agent_name=request.agent_name,
                    user_message=request.user_message,
                    observations=observations,
                    task_spec=task_spec,
                    message_id=None,  # FSM doesn't create a message_id yet
                )
                if result:
                    artifact, fe, rcp = result
                    if rcp:
                        report_card_payload = rcp.model_dump()
                    file_exports = fe or {}
                    if artifact:
                        artifact_ids.append(artifact.id)
                    if file_exports:
                        primary_fmt = next(iter(file_exports))
                        export_entry = file_exports[primary_fmt]
                        export_artifact_id = export_entry.get("artifact_id")
                        if export_artifact_id and export_artifact_id not in artifact_ids:
                            artifact_ids.append(export_artifact_id)

                logger.info(
                    "FINALIZE: artifact produced for conv=%s, file_exports=%s",
                    request.conversation_id, file_exports,
                )
            except Exception as e:
                logger.warning(
                    "FINALIZE: fsm_finalize_into_artifact failed (non-fatal): %s", e,
                )

        # ── Non-file data tasks: surface the synthesize ReportCardPayload ──
        # File-export tasks already set report_card_payload from the artifact
        # finalize above. For analyze-data tasks (no export), surface the
        # synthesize node's structured payload ONLY when the user explicitly
        # requested a file. Inline data analysis should produce a natural-
        # language markdown response, NOT a ReportCard/JSON card.
        if _user_requested_file:
            from app.services.synexia.capability_router import (
                _select_finalize_report_card_payload,
            )
            report_card_payload = _select_finalize_report_card_payload(
                observations, report_card_payload,
            )
        else:
            report_card_payload = None

        # ── Selected-skill validation: check whether the generated payload
        # still satisfies the structure/signals implied by the user-selected
        # runtime skill. This is a lightweight heuristic pass that lowers
        # confidence and warns the user when the output drifted away from the
        # selected skill's template requirements.
        if report_card_payload and (task_spec.get("selected_skill") or task_spec.get("selected_skill_name") or task_spec.get("selected_skill_id")):
            try:
                from app.services.synexia.quality_eval import validate_selected_skill_payload

                _selected_name = task_spec.get("selected_skill_name") or (task_spec.get("selected_skill") or {}).get("name") or "selected-skill"
                _skill_body = ""
                for _obs in reversed(observations):
                    if getattr(_obs, "observation_type", "") == "skill_call" and getattr(_obs, "success", False):
                        _rd = getattr(_obs, "result_data", None) or {}
                        if isinstance(_rd, dict) and _rd.get("body"):
                            _skill_body = _rd.get("body") or ""
                            break
                _artifact_type = "docx"
                _ais = task_spec.get("artifact_intents") or []
                if _ais:
                    _artifact_type = _ais[0]
                elif user_signal.startswith("export_"):
                    _artifact_type = user_signal.replace("export_", "") or "docx"

                _validation = validate_selected_skill_payload(
                    skill_name=_selected_name,
                    skill_body=_skill_body,
                    artifact_type=_artifact_type,
                    payload=report_card_payload,
                )
                factors = dict(factors or {})
                factors["selected_skill_validation"] = _validation
                if not _validation.get("is_ok", True):
                    confidence = min(confidence, 0.55)
                    notice = (
                        "\n\n---\n*Selected skill validation: the output may not fully match the chosen skill template. "
                        + "; ".join(_validation.get("issues") or ["template requirements missing"])
                        + "*"
                    )
                    assistant_content = (assistant_content or "") + notice
                    self.execution.assistant_content = assistant_content
                    self.execution.confidence_score = confidence
                    self.execution.confidence_factors = factors
                    self.db.commit()
            except Exception as e:
                logger.warning("Selected skill validation failed (non-fatal): %s", e)

        # Plan summary for ActivityRail
        plan_summary = None
        if self.plan:
            plan_summary = {
                "nodes": [
                    {
                        "seq": n.seq,
                        "name": n.name,
                        "node_type": n.node_type,
                        "status": n.status,
                    }
                    for n in self.plan.nodes
                ],
                "status": self.plan.status,
            }

        # ── Quality gate (Phase B): hold back artifacts below threshold ──
        # Artifacts remain in the DB; they are just not shipped in the
        # result. The user is told the output was held and why.
        # Automation runs (unattended) use a stricter threshold so a
        # silently-wrong shipped report is less likely.
        quality_gate = None
        if artifact_ids:
            from app.services.synexia.confidence_scorer import quality_gate_decision

            _gate_threshold = getattr(settings, "SYNEXIA_QUALITY_GATE_THRESHOLD", 0.4)
            if getattr(request, "is_automation", False):
                _gate_threshold = getattr(
                    settings, "SYNEXIA_QUALITY_GATE_THRESHOLD_AUTOMATION", 0.6,
                )

            quality_gate = quality_gate_decision(
                confidence,
                artifact_ids,
                enabled=getattr(settings, "SYNEXIA_QUALITY_GATE_ENABLED", True),
                threshold=_gate_threshold,
            )
            if not quality_gate.get("passed", True):
                artifact_ids = []
                file_exports = {}
                export_artifact_id = None
                factors = dict(factors or {})
                factors["quality_gate"] = quality_gate
                notice = (
                    "\n\n---\n*Quality gate: the generated file was held back "
                    f"because execution confidence ({confidence:.2f}) is below "
                    f"the shipping threshold ({quality_gate['threshold']:.2f}). "
                    "Please ask me to retry or refine the request.*"
                )
                assistant_content = (assistant_content or "") + notice
                self.execution.assistant_content = assistant_content
                self.db.commit()
                logger.warning(
                    "Quality gate held %d artifact(s): confidence=%.2f < %.2f",
                    quality_gate["artifact_count"], confidence,
                    quality_gate["threshold"],
                )

        logger.info("Execution %s finalized (confidence=%.2f)", self.execution.id, confidence)

        # Surface the QUALITY_EVAL verdict on the result (None when disabled
        # or when the streaming path defers it to post-stream).
        _quality_eval_out = None
        _cf = self.execution.confidence_factors or {}
        if isinstance(_cf, dict) and isinstance(_cf.get("quality_eval"), dict):
            _quality_eval_out = _cf["quality_eval"]

        return ExecutionResult(
            execution_id=self.execution.id,
            assistant_content=assistant_content,
            tool_calls=tool_calls,
            artifact_ids=artifact_ids,
            confidence=confidence,
            confidence_factors=factors,
            plan_summary=plan_summary,
            state=FSMState.DONE.value,
            report_card_payload=report_card_payload,
            file_exports=file_exports,
            export_artifact_id=export_artifact_id,
            quality_gate=quality_gate,
            quality_eval=_quality_eval_out,
        )

    def _build_response_prompt(self, request: ExecutionRequest) -> str:
        """Build the FINALIZE response prompt from the execution observations.

        Shared by the blocking :meth:`_generate_response` and the streaming
        :meth:`stream_final_response` so both paths use identical prompts.

        Includes the conversation transcript (from the context manifest) so
        follow-up turns ("make it better", "dark theme") are answered with
        full awareness of what was already discussed — instead of the
        context-blind clarifying-question loops this used to produce.
        """
        from app.services.synexia.grounding_extractor import extract_grounding
        from app.services.synexia.quality_eval import _format_acceptance_criteria

        grounding = extract_grounding(self.execution.observations)

        # Extract the compact transcript from the context manifest (assembled
        # in the CONTEXT state) so the response generator sees prior turns.
        transcript = ""
        task_spec = self.execution.task_spec or {}
        is_followup = bool(task_spec.get("is_followup"))
        try:
            cm = self.execution.context_manifest or {}
            conv_ctx = cm.get("conversation_context") or {}
            if not conv_ctx:
                for item in (cm.get("items") or []):
                    if isinstance(item, dict) and item.get("type") == "conversation_context":
                        conv_ctx = item.get("content") or {}
                        break
            transcript = (conv_ctx.get("transcript") or "").strip()
        except Exception:
            transcript = ""

        transcript_block = ""
        if transcript:
            transcript_block = (
                "\n\n=== Conversation so far ===\n"
                f"{transcript}\n"
            )

        followup_note = ""
        if is_followup:
            followup_note = (
                "\nThis is a follow-up turn — the user is refining something "
                "from a previous turn. Reference the prior artifact/result "
                "and act on the refinement; do NOT ask the user to restate "
                "what is already in the conversation.\n"
            )

        criteria_block = ""
        _criteria = _format_acceptance_criteria(task_spec) if task_spec else ""
        if _criteria and not _criteria.startswith("(none"):
            criteria_block = f"\n\n=== You must satisfy ===\n{_criteria}"

        return f"""You are {request.agent_name}, an AI assistant. Based on the user's request and the actions taken, provide a helpful response.

User request: {request.user_message}{transcript_block}{followup_note}
=== Findings (from executed actions) ===
{grounding}{criteria_block}

Provide a thorough response that reflects the depth and scope of the findings. Match your response depth to the user's request:
- **Data-rich queries** (multiple metrics, comparisons, tables): produce detailed analysis with sections, tables, key figures, and actionable insights. Present every significant number the findings contain — never omit data the user explicitly asked for.
- **Simple questions**: be direct and brief.
Reference the actual findings above by value (figures, names, counts) — do not narrate tool calls. Your response must satisfy every criterion listed under "You must satisfy" (if any). If actions were successful, summarize what was accomplished. If there were errors, explain what went wrong.

OUTPUT FORMAT — STRICT RULES:
- DO NOT output raw JSON, code fences, or structured payload objects (no `{{"title": ...}}` blocks in the answer).
- If findings include structured data (KPIs, metrics, rankings), render it as a markdown TABLE with prose, NOT as a JSON dump.
- Inline markdown only — never a code block containing a report-card JSON object. The user is a CEO, not an integration engineer.
- When findings are substantial, organize into sections (##) with bold labels. Typical sections for data-rich queries: Executive Summary, Key Metrics, Detailed Findings (with tables), Notable Patterns/Anomalies, Recommendations.
- NEVER fabricate data, metrics, or trends not present in the findings. If the data doesn't support a section (e.g., no clear anomalies), omit that section entirely rather than inventing content.

Clarification policy: ask at most ONE clarifying question, and ONLY if the conversation history above does not already answer it. Never re-ask information the user already provided in a previous turn (topic, data, audience, style, slide count, etc.). If the request is a follow-up refinement, just do it. If the user granted open latitude ("any data you can use", "use fake/demo data", "you choose", "whatever works"), proceed with sensible defaults — generate clearly-marked demo data (label it indicative) instead of asking. If a choice genuinely matters, offer at most 3 numbered options with your recommendation marked; never a questionnaire."""

    def _generate_response(self, request: ExecutionRequest) -> str:
        """Generate the assistant's text response from observations (blocking)."""
        from app.services.llm_service import call_llm

        system_prompt = self._build_response_prompt(request)
        try:
            result = call_llm(
                prompt=system_prompt,
                messages=[],
                temperature=0.7,
                endpoint=request.endpoint,
            )
            # Production call_llm is ASYNC — calling it without await returns
            # a coroutine object; ``.get(...)`` on it raises AttributeError and
            # every blocking FINALIZE silently fell back to the generic
            # "I've processed your request." (the LLM was NEVER called).
            # We are in a worker thread (run via asyncio.to_thread), so a
            # fresh event loop is always safe — same bridge pattern as
            # run_quality_eval (line ~694). Tests may inject a sync stub
            # returning a dict → pass it through untouched.
            import asyncio as _asyncio

            if _asyncio.iscoroutine(result):
                _loop = _asyncio.new_event_loop()
                try:
                    result = _loop.run_until_complete(result)
                finally:
                    try:
                        _loop.close()
                    except Exception:
                        pass
            return result.get("response", "I've processed your request.")
        except Exception as e:
            logger.warning("Response generation failed: %s", e)
            return "I've processed your request."

    async def stream_final_response(
        self, request: ExecutionRequest,
    ):
        """Stream the FINALIZE response token-by-token (SSE path).

        Requires :meth:`run` to have been called with
        ``generate_response=False`` so the deferred response prompt is built
        (and stashed) during FINALIZE. Yields ``("delta", text)`` per token
        and a final ``("done", full_text)``. If streaming is unavailable
        (no provider connects, or the stream raises), falls back to a
        blocking ``call_llm`` and yields its result as a single delta — so
        the response is always emitted.
        """
        from app.services.llm_service import stream_chat_completion, call_llm

        prompt = getattr(self, "_deferred_response_prompt", None)
        if prompt is None:
            prompt = self._build_response_prompt(request)

        full_text = ""
        try:
            async for delta in stream_chat_completion(
                prompt, temperature=0.7, endpoint=request.endpoint,
            ):
                full_text += delta
                yield ("delta", delta)
        except Exception as e:
            logger.warning("FSM response streaming raised (non-fatal): %s", e)

        if not full_text:
            # Streaming yielded nothing or failed — blocking fallback so the
            # user still gets a response.
            try:
                result = await call_llm(
                    prompt=prompt, messages=[], temperature=0.7,
                    endpoint=request.endpoint,
                )
                full_text = result.get("response", "I've processed your request.")
            except Exception as e:
                logger.warning("FSM response blocking fallback failed: %s", e)
                full_text = "I've processed your request."
            yield ("delta", full_text)

        yield ("done", full_text)


def is_fsm_enabled() -> bool:
    """Check if the Synexia FSM is enabled via feature flag."""
    return settings.SYNEXIA_FSM_ENABLED
