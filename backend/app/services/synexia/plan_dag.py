"""PlanDAG generator — creates an execution plan from a TaskSpec.

The plan is a directed acyclic graph (DAG) of nodes.  Each node represents
one step: a skill call, tool call, sandbox job, or NL2SQL query.

The plan is visible to the user (via PlanEditor) and can be approved or
modified before execution (at the GATE state).
"""

import json
import logging
from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.execution import Plan, PlanNode

logger = logging.getLogger(__name__)


def _parse_plan_text(text: str) -> list[dict]:
    """Parse a raw planner reply (possibly fenced) into a step list.

    Returns ``[]`` when the text is not JSON — the caller decides whether
    that is fatal or a fallback.
    """
    from app.services.llm_service import _array_schema_payload

    cleaned = text.strip()
    if "```json" in cleaned:
        cleaned = cleaned.split("```json")[1].split("```")[0]
    elif "```" in cleaned:
        cleaned = cleaned.split("```")[1].split("```")[0]
    try:
        return _array_schema_payload(json.loads(cleaned)) or []
    except (json.JSONDecodeError, TypeError):
        return []


def _extract_llm_plan_steps(result: dict, default_plan: list[dict]) -> list[dict]:
    """Return the planner's steps from an ``llm_service`` result dict.

    ``plan_dag`` is the only caller that asks for a TOP-LEVEL array schema.
    Providers cannot emit a bare array under ``response_format=json_object``,
    so the reply shows up in one of three shapes:

      * ``data`` is already the step list (provider honoured the array), or
      * ``data`` is the schema-echo envelope
        ``{"type": "array", "items": [steps]}`` (what DeepSeek returns), or
      * ``response`` is raw text — prose or a fenced JSON block.

    ``llm_service._array_schema_payload`` normalises all of the structural
    shapes; this function adds the text path and, crucially, LOGS the
    fallback.  A silent fallback here is what let the planner run the
    generic default plan indefinitely while the only visible symptom was a
    downstream "dependencies not met" warning.
    """
    from app.services.llm_service import _array_schema_payload

    if not isinstance(result, dict):
        logger.warning(
            "LLM planner returned %s instead of a result dict — using default plan",
            type(result).__name__,
        )
        return default_plan

    for key in ("data", "response"):
        value = result.get(key)
        steps = _array_schema_payload(value)
        if steps:
            return steps
        if isinstance(value, str) and value.strip():
            steps = _parse_plan_text(value)
            if steps:
                return steps

    logger.warning(
        "LLM planner returned no usable step list (keys=%s) — using default plan. "
        "Expected the parsed array under 'data' or 'response'.",
        sorted(result.keys()),
    )
    return default_plan


def _run_llm_sync(coro):
    """Run an async coroutine from this sync planner context.

    Same pattern as capability_router._run_coro_sync, duplicated here to
    avoid a circular import (capability_router imports plan_dag).
    """
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _sanitize_plan_step(step: dict) -> tuple[str | None, dict]:
    """Validate a plan step's artifact type and format BEFORE it becomes a node.

    ``output_artifact_type`` and ``inputs.format`` are free-form LLM output.
    Measured live 2026-09-22: "more details" → TaskSpec intent 'elaboration'
    → a sandbox node "Generate elaboration" with format 'elaboration' → the
    sandbox rejected the format / had no data, and the turn lost its
    deliverable AND its answer. A type the sandbox cannot render is dropped
    here (and the format key removed) so the planner's word-shapes cannot
    doom a node; known aliases ('dataframe' → xlsx) are normalised through
    the sandbox's own mapping so there is ONE source of truth.

    Returns ``(output_artifact_type, inputs)``.
    """
    inputs = dict(step.get("inputs") or {})
    raw_type = step.get("output_artifact_type") or inputs.get("format")
    if not raw_type:
        return None, inputs
    try:
        from app.services.tool_handlers.sandbox_tool import (
            _IMAGE_BY_FORMAT as _gen_formats,
            normalize_format,
        )

        normalized = normalize_format(str(raw_type))
        if normalized in _gen_formats:
            if "format" in inputs:
                inputs["format"] = normalized
            return normalized, inputs
        logger.warning(
            "plan_dag: dropped unrenderable artifact type %r from step %r — "
            "the sandbox can never produce it (invented intent?)",
            raw_type, step.get("name"),
        )
        inputs.pop("format", None)
        return None, inputs
    except Exception as e:  # sanitizer must never break planning
        logger.debug("plan_dag: step sanitization skipped: %s", e)
        return step.get("output_artifact_type"), inputs


def _intent_is_renderable(artifact_type) -> bool:
    """True when the sandbox can actually render this artifact type.

    An invented intent ('elaboration', 2026-09-22) used to become a doomed
    sandbox node: no data, invalid format, failed turn. A non-renderable
    intent means the user wants MORE of the answer, not a file — the
    nl2sql/answer steps serve that. Normalises through the sandbox's own
    mapping (dataframe → xlsx) so there is ONE source of truth.
    """
    try:
        from app.services.tool_handlers.sandbox_tool import (
            _IMAGE_BY_FORMAT as _gen_formats,
            normalize_format,
        )

        return normalize_format(str(artifact_type or "")) in _gen_formats
    except Exception:
        return False


def _strip_sandbox_without_file_intent(steps: list, task_spec: dict) -> list:
    """Strip only ORPHAN sandbox steps — those no data step feeds.

    Measured live 2026-09-22: "上周按产品、客户及组织划分的销售报告" (no
    format words) was planned as an XLSX/DOCX file turn fed with TWO rows —
    the analysis itself never ran. That failure shape is a sandbox step with
    NO nl2sql/data dependencies: a file built from thin air.

    2026-10-10 goal-based amendment: the old rule stripped EVERY sandbox step
    whenever the user's message lacked a format keyword, overriding the
    planner's own deliverable decision. Now a sandbox step that a data step
    feeds IS the plan's deliberate deliverable and survives (the FINALIZE
    artifact gate ships it); only orphan sandbox steps are removed.
    """
    try:
        has_file_intent = bool(task_spec.get("artifact_intents")) or str(
            task_spec.get("user_signal") or ""
        ).startswith("export_")
        if has_file_intent or not isinstance(steps, list):
            return steps
        data_step_indices = {
            i for i, st in enumerate(steps)
            if isinstance(st, dict) and st.get("node_type") != "sandbox"
        }
        kept = []
        stripped = 0
        for i, st in enumerate(steps):
            if not isinstance(st, dict) or st.get("node_type") != "sandbox":
                kept.append(st)
                continue
            deps = st.get("dependencies") or []
            if any(d in data_step_indices for d in deps):
                # A data step feeds this sandbox — the planner deliberately
                # built a file deliverable on real query results. Keep it.
                kept.append(st)
                continue
            stripped += 1
        if stripped:
            logger.info(
                "plan: stripped %d orphan sandbox step(s) — no data step "
                "feeds them (file from thin air)",
                stripped,
            )
        return kept or steps
    except Exception as e:
        logger.debug("plan_dag: sandbox strip skipped: %s", e)
        return steps


def _build_report_decomposition_steps(task_spec: dict) -> list | None:
    """Decompose a multi-dimension report question into per-dimension queries.

    A question like "上周按产品、客户及组织划分的销售报告" cannot be answered
    by ONE SQL statement — the measured good answer (Hermes, 2026-09-22) runs
    ~10 queries: totals, one per requested dimension, the prior-period twin
    for WoW comparisons, then synthesizes. Generic across databases: the
    dimensions come from the TaskSpec LLM (which reads them off the question),
    never from per-database code.

    Emits: 1 totals node + 1 node per dimension + 1 prior-period twin +
    1 synthesize node depending on all of them. ``None`` when the question
    names no dimensions (the standard plan applies).
    """
    try:
        dimensions = [
            str(d).strip() for d in (task_spec.get("dimensions") or [])
            if str(d).strip()
        ]
        if not dimensions:
            return None
        question = (
            task_spec.get("user_message")
            or (task_spec.get("entities", {}) or {}).get("question")
            or ""
        ).strip()
        if not question:
            return None

        steps: list[dict] = []

        def _nl2sql(name: str, sub_question: str, deps: list[int]) -> dict:
            return {
                "node_type": "nl2sql",
                "name": name,
                "description": sub_question,
                "dependencies": deps,
                "expected_output": "DataSnapshot",
                "inputs": {"question": sub_question},
            }

        steps.append(_nl2sql(
            "Retrieve totals",
            f"{question} — overall totals only, no dimensional breakdown",
            [],
        ))
        for dim in dimensions:
            steps.append(_nl2sql(
                f"Break down by {dim}",
                f"{question} — break down by {dim} ONLY (one row per {dim}, "
                f"with amounts, quantities and completion rate where available)",
                [],
            ))
        steps.append(_nl2sql(
            "Retrieve prior period",
            f"{question} — the SAME metrics for the PREVIOUS period of the "
            f"same length (for week-over-week / period-over-period comparison)",
            [],
        ))
        steps.append({
            "node_type": "synthesize",
            "name": "Synthesize report",
            "description": (
                "Merge the totals, per-dimension and prior-period snapshots "
                "into one report. Disclose the caliber (period window "
                "semantics, filters, source, freshness), the WoW deltas, and "
                "any data-quality findings (NULL dimension keys)."
            ),
            "dependencies": list(range(len(steps))),
            "expected_output": "ReportCardPayload + executive summary text",
            "inputs": {},
        })
        return steps
    except Exception as e:
        logger.debug("plan_dag: report decomposition skipped: %s", e)
        return None


def _sanitize_plan_dependencies(steps: list) -> tuple[list, list]:
    """Make an LLM-authored dependency graph executable (2026-09-22).

    Measured live: the planner emitted a CYCLIC graph for a follow-up
    question and the executor refused it — "FSM execution failed: Dependency
    cycle" — the plan was authored, then the WHOLE turn died on a defect
    that is always salvageable. A plan with a cycle is fixed here, not
    refused:

    1. drop garbage deps (non-int, out of range, self);
    2. reorder steps topologically when deps merely point forward
       (legal in a DAG, fatal in a sequential renderer);
    3. when a TRUE cycle exists, drop one back-edge per pass (largest dep
       of the smallest stuck node) until acyclic — nodes are never removed.

    Returns ``(steps, dropped_labels)``; result always passes
    ``_validate_acyclic``. Fail-soft: on any internal error the input is
    returned untouched.
    """
    try:
        if not isinstance(steps, list) or not steps:
            return steps, []

        n = len(steps)
        dropped: list[str] = []

        def _deps(i: int) -> list:
            d = steps[i].get("dependencies")
            return d if isinstance(d, list) else []

        # 1. garbage / self deps
        for i in range(n):
            clean = [d for d in _deps(i)
                     if isinstance(d, int) and 0 <= d < n and d != i]
            if len(clean) != len(_deps(i)):
                for d in _deps(i):
                    if not (isinstance(d, int) and 0 <= d < n) or d == i:
                        dropped.append(f"dep {d!r} of step {i}")
                steps[i]["dependencies"] = clean

        # 2. topological reorder (stable) — legal DAGs stay legal, just ordered
        def _topo_order(cur: list) -> list[int] | None:
            m = len(cur)
            indeg = [0] * m
            for i in range(m):
                for d in cur[i].get("dependencies") or []:
                    if isinstance(d, int) and 0 <= d < m:
                        indeg[i] += 1
            queue = [i for i in range(m) if indeg[i] == 0]
            order: list[int] = []
            seen = list(indeg)
            deps_of = [
                [d for d in (cur[i].get("dependencies") or [])
                 if isinstance(d, int) and 0 <= d < m]
                for i in range(m)
            ]
            while queue:
                i = queue.pop(0)
                order.append(i)
                for j in range(m):
                    if i in deps_of[j]:
                        seen[j] -= 1
                        if seen[j] == 0:
                            queue.append(j)
            return order if len(order) == m else None

        order = _topo_order(steps)
        if order is not None:
            ordered = [steps[i] for i in order]
            # Re-index dependencies to the new order.
            pos = {old: new for new, old in enumerate(order)}
            for i, st in enumerate(ordered):
                st["dependencies"] = [
                    pos[d] for d in (st.get("dependencies") or [])
                    if isinstance(d, int) and d in pos
                ]
            if ordered != steps:
                steps[:] = ordered
            return steps, dropped

        # 3. true cycle: drop one back-edge per pass until acyclic.
        guard = 0
        while _topo_order(steps) is None and guard < n * n:
            guard += 1
            m = len(steps)
            # smallest-index node that is part of a cycle: stuck in Kahn
            order = _topo_order(steps)
            stuck = [i for i in range(m)
                     if i not in set(order or [])]
            if not stuck:
                break
            i = min(stuck)
            deps = [d for d in (steps[i].get("dependencies") or [])
                    if isinstance(d, int) and 0 <= d < m]
            if not deps:
                break
            victim = max(deps)
            steps[i]["dependencies"] = [d for d in deps if d != victim]
            dropped.append(f"dep {victim} of step {i}")
        return steps, dropped
    except Exception as e:
        logger.debug("plan_dag: dependency sanitization skipped: %s", e)
        return steps, []


def generate_plan(
    db: Session,
    execution_id: str,
    task_spec: dict,
    context_manifest: dict,
    agent_name: str,
    *,
    failure_context: list[dict] | None = None,
    plan_version: int = 1,
    endpoint=None,
) -> Plan:
    """Generate a PlanDAG from a TaskSpec using the LLM.

    The LLM proposes a sequence of steps.  The plan is validated for
    acyclicity before being stored.

    Args:
        failure_context: Optional list of ``{name, node_type, error}`` dicts
            describing plan nodes that failed in a previous attempt. When
            provided, the planner is asked for a *corrective* plan that
            recovers from those failures instead of planning from scratch.
            Used by the dynamic re-planning paths (capability_router ACT
            re-plan and the FSM VERIFY re-plan).
        plan_version: Version number to stamp on the Plan row (1 for the
            initial plan, >1 for corrective re-plans).
        endpoint: Optional hierarchical LLMEndpoint. When set, the planner
            LLM call targets that exact provider+model instead of the
            legacy .env defaults.
    """
    from app.services.llm_service import call_llm
    import json

    # Build the prompt
    artifact_intents = task_spec.get("artifact_intents", [])
    task_kind = task_spec.get("task_kind", "general")
    requires_data = task_spec.get("requires_data", False)
    entities = task_spec.get("entities", {})
    skill_override = task_spec.get("skill_override", False)
    auto_picked_default = task_spec.get("auto_picked_default", None)

    # ── Default-skill hint: when auto_picked_default is set and no
    #     override is active, tell the planner which skill to invoke.
    #     When ``forced_skill`` is set (from the post-router hook), upgrade
    #     the hint to a HARD directive so the planner MUST emit a skill
    #     step rather than merely including one. ────────────────────────
    if task_spec.get("selected_skill") or task_spec.get("selected_skill_id") or task_spec.get("selected_skill_name"):
        default_skill_hint = _build_selected_skill_directive_block(task_spec)
    elif task_spec.get("forced_skill"):
        default_skill_hint = _build_skill_directive_block(task_spec)
    else:
        default_skill_hint = ""
        if not skill_override and auto_picked_default:
            default_skill_hint = f"\nAuto-picked default skill: {auto_picked_default} — the plan should include a step to invoke this skill.\n"

    # ── Conversation context (follow-up awareness) ────────────────────
    # Extract the compact transcript + recent artifacts + prior entities
    # from the context manifest so the planner can refine a prior
    # artifact instead of re-querying data from scratch on follow-up
    # turns ("make it better", "dark theme").
    conv_ctx = {}
    if isinstance(context_manifest, dict):
        # Prefer the structured "conversation_context" item; fall back to
        # scanning the items list for backward compatibility.
        conv_ctx = context_manifest.get("conversation_context") or {}
        if not conv_ctx:
            for item in (context_manifest.get("items") or []):
                if isinstance(item, dict) and item.get("type") == "conversation_context":
                    conv_ctx = item.get("content") or {}
                    break
    context_block = _format_plan_context_block(conv_ctx, task_spec)
    is_followup = bool(task_spec.get("is_followup"))
    refines_id = task_spec.get("refines_artifact_id")

    # User attachments. The CONTEXT state folds each upload's extracted text
    # into the manifest as a ``user_attachments`` item. This planner used to
    # read only ``conversation_context``, so the file never reached the planner
    # either — it planned a generic data-fetch for a question that the attached
    # rows could answer outright.
    attachments_block = ""
    try:
        from app.services.synexia.context_assembler import render_attachment_items
        _attach_text = render_attachment_items(context_manifest)
        if _attach_text:
            attachments_block = (
                "\n=== User attachments (already extracted — the plan can "
                "answer from this text, no file read step needed) ===\n"
                f"{_attach_text}\n"
            )
    except Exception:
        attachments_block = ""

    # Real tool inventory. The planner previously saw ONLY the conversation
    # transcript, so it invented tool and skill names (`read_attached_deck`,
    # `attached_deck_parser`, `compose_answer`, `extract_deck_facts`) and every
    # ACT node then failed on an unknown name. Tell it what actually exists.
    inventory_block = ""
    try:
        _cm = context_manifest if isinstance(context_manifest, dict) else {}
        _tools = list(_cm.get("available_tools") or [])
        if not _tools:
            for _item in (_cm.get("items") or []):
                if isinstance(_item, dict) and _item.get("type") == "available_tools":
                    _tools = list(_item.get("content") or [])
                    break
        if _tools:
            _shown = sorted(_tools)[:400]
            _more = "" if len(_tools) <= 400 else f" (+{len(_tools) - 400} more)"
            inventory_block = (
                "\n=== Tools that ACTUALLY EXIST (callable right now) ===\n"
                f"{', '.join(_shown)}{_more}\n"
                "Hard rule: reference ONLY names from this list, and only skill "
                "names that appear in the skill catalog below. NEVER invent a "
                "tool or skill name — a node naming something absent here fails "
                "at execution and wastes the whole turn. If no listed tool fits "
                "a step, omit the step rather than inventing one.\n"
            )
    except Exception:
        inventory_block = ""

    # Skill catalog (progressive disclosure Layer A) so the planner can
    # emit load_skill nodes for ANY skill, not just the format defaults.
    # Pure string builder — no LLM call, no DB write, no side effects.
    skill_catalog_block = ""
    try:
        from app.services.skills_loader.skill_planner_hook import get_skill_planner_hook
        skill_catalog_block = get_skill_planner_hook().build_plan_prompt_extra()
    except Exception as exc:
        logger.debug("Could not build skill catalog for planner (non-fatal): %s", exc)

    # Default plan based on task kind (fallback if LLM fails)
    default_plan = _build_default_plan(task_spec, agent_name)

    if failure_context:
        # ── Corrective re-plan: ask the LLM to recover from failures ────
        failures_text = "\n".join(
            f"- Step '{f.get('name')}' ({f.get('node_type', 'tool')}): {f.get('error', 'unknown error')}"
            for f in failure_context
        )
        system_prompt = f"""The previous execution plan failed. Generate a CORRECTIVE plan that still accomplishes the original task despite these failures.

Original task kind: {task_kind}
Entities: {json.dumps(entities)}
Failed steps:
{failures_text}
{context_block}
{skill_catalog_block}
Generate a revised plan as a JSON array of steps. Each step has:
- node_type: "skill" | "tool" | "nl2sql" | "sandbox" | "agent"
- name: short name for the step
- description: what the step does
- dependencies: list of step indices (0-based) this step depends on
- expected_output: what the step produces
- output_artifact_type: artifact type if this step produces an artifact
- inputs: a JSON object of concrete arguments for this step (e.g. {{"question": "..."}} for nl2sql, {{"title": "...", "format": "docx"}} for sandbox, {{"skill_name": "..."}} for skill). Use {{}} if none.

Rules:
- Do NOT repeat the exact failed action with the same arguments — try a different approach or corrected inputs.
- Keep the corrective plan minimal: only the steps needed to recover and produce the final output.
- The last step must produce the final output.

Respond with ONLY a JSON array."""
    else:
        followup_rules = ""
        if is_followup:
            followup_rules = (
                "\nFollow-up rules (this turn refines a prior artifact):\n"
                f"- Refine artifact id={refines_id} from the conversation using the "
                "``edit_artifact`` tool (NOT create_artifact).\n"
                "- Do NOT re-query data unless the user explicitly asks for "
                "new/different data.\n"
                "- Inherit entities from the prior turn (already merged into the task spec).\n"
                "- Keep the plan minimal: only the steps needed to update the artifact.\n"
                "- The edit_artifact tool accepts: artifact_id, instructions, "
                "and optional title/format/payload overrides.\n"
            )
        system_prompt = f"""Create an execution plan for the following task.

Task kind: {task_kind}
Artifact intents: {artifact_intents}
Requires data: {requires_data}
Entities: {json.dumps(entities)}
Agent: {agent_name}{default_skill_hint}{context_block}{attachments_block}{followup_rules}
{inventory_block}
{skill_catalog_block}
Create a plan as a JSON array of steps. Each step has:
- node_type: "skill" | "tool" | "nl2sql" | "sandbox" | "agent"
- name: short name for the step
- description: what the step does
- dependencies: list of step indices (0-based) this step depends on
- expected_output: what the step produces
- output_artifact_type: artifact type if this step produces an artifact
- inputs: a JSON object of concrete arguments for this step (e.g. {{"question": "..."}} for nl2sql, {{"title": "...", "format": "docx"}} for sandbox, {{"skill_name": "..."}} for skill). Use {{}} if none.

Rules:
- Keep plans simple (2-5 steps for most tasks)
- Data retrieval (nl2sql) must come before artifact generation
- Sandbox steps are for PPTX/DOCX/XLSX generation
- Tool steps are for simple actions (web search, file ops)
- The last step should always produce the final output
- VISUALISATION: when the user asks to SEE the data as a chart, graph or plot
  (图表 / 可视化 / 画个图) and did NOT ask for a downloadable file, the plan MUST
  include BOTH:
    1. the data step that yields the numbers to draw, and
    2. a skill step loading the `inline-chart` skill —
       {{"node_type": "skill", "name": "inline-chart",
        "inputs": {{"skill_name": "inline-chart"}}}} —
       which carries the exact rendering contract the agent then follows.
  The chart is rendered by the `create_artifact` tool with type="html" and a
  self-contained document in payload.html_content. `type` has NO "chart" value
  (enum: docx|pdf|pptx|html) and there is NO `RenderBarChart`, `render_chart`,
  `create_chart` or `plot_chart` tool — do not put those names in a plan, and
  do not invent a chart skill. A chart the user asked for is a deliverable: it
  is NOT satisfied by describing the numbers in prose, and never by drawing
  bars out of text characters.
- Sandbox `format` must be one of: xlsx | pptx | html | pdf | docx | md.
  Never pass format "table", "dataframe", "chart" or "csv" — the sandbox
  rejects the step and the whole turn loses its deliverable.
- REPORT / MULTI-DIMENSION questions (the task_spec lists `dimensions`,
  e.g. 按产品、客户及组织) MUST be planned as MULTIPLE nl2sql nodes — ONE per
  requested dimension, plus a totals node, plus a prior-period twin node for
  comparison — all feeding ONE final synthesize step. A single nl2sql node
  cannot answer a multi-dimension report; a sandbox file built from one
  query is NOT the deliverable unless the user asked for a downloadable file.
- Include a sandbox (file) step ONLY when a file deliverable clearly serves
  the user's goal, and ONLY after the data steps it depends on — a file must
  be built from real query results, never from thin air. When the goal is an
  analytical answer, respond in chat from the merged nl2sql snapshots.

Respond with ONLY a JSON array."""

    from app.config import settings

    # Phase 3a: the LLM planner is opt-in (SYNEXIA_LLM_PLANNER_ENABLED,
    # default OFF). The curated default plan is the proven path; the LLM
    # planner is gated for staging validation. This also fixes the prior
    # un-awaited call_llm (call_llm is async — it was bare-called, silently
    # always falling through to default_plan).
    _llm_planner_enabled = getattr(settings, "SYNEXIA_LLM_PLANNER_ENABLED", False)
    plan_steps = default_plan
    if _llm_planner_enabled:
        try:
            # JSON schema forces the provider into json_object mode
            # (build_llm_payload injects the schema hint + sets
            # response_format).  DeepSeek and some local models ignore
            # prose "respond with ONLY JSON" and emit prose instead,
            # which silently degrades every plan to the generic default
            # (whose "Process request" tool node then fails at runtime).
            _plan_schema = {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "node_type": {
                            "type": "string",
                            "enum": ["skill", "tool", "nl2sql", "sandbox", "agent"],
                        },
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "dependencies": {"type": "array", "items": {"type": "integer"}},
                        "expected_output": {"type": "string"},
                        "output_artifact_type": {"type": "string"},
                        "inputs": {"type": "object"},
                    },
                    "required": ["node_type", "name", "dependencies", "inputs"],
                },
            }
            result = _run_llm_sync(call_llm(
                prompt=system_prompt,
                messages=[{"role": "user", "content": f"Plan this task: {task_spec}"}],
                temperature=0,
                response_json_schema=_plan_schema,
                endpoint=endpoint,
            ))

            # Normalise the reply into a step list.  The provider does not
            # return a bare top-level array under json_object mode, so the
            # steps arrive as a parsed list, a schema-echo envelope, or raw
            # (possibly fenced) text — see _extract_llm_plan_steps, which
            # also logs loudly when nothing usable comes back.
            plan_steps = _extract_llm_plan_steps(result, default_plan)
        except Exception as e:
            logger.warning("LLM plan generation failed, using default: %s", e)
            plan_steps = default_plan

    # ── Deterministic guard: a prompt rule is not a guarantee ───────────
    # The planner is now told which tools exist, but an invented name must not
    # be able to burn the turn. Observed live before the inventory fix:
    # tool:read_attached_deck, tool:attached_deck_parser, agent:compose_answer
    # and skill:extract_deck_facts / compose_final_answer — all unknown, every
    # node failed, and their failure text reached the user as an execution
    # narrative. Drop such steps here, where the plan is still ours to fix.
    plan_steps, _dropped_steps = _drop_unknown_steps(plan_steps)
    if _dropped_steps:
        logger.warning(
            "plan: dropped %d step(s) naming tools/skills that do NOT exist "
            "(hallucinated names): %s",
            len(_dropped_steps), "; ".join(_dropped_steps),
        )
    if not plan_steps:
        logger.warning(
            "plan: every authored step was invalid — falling back to the default plan"
        )
        plan_steps = default_plan

    # Create Plan record
    summary = f"Plan for {task_kind}: {', '.join(artifact_intents) or 'general task'}"
    if failure_context:
        summary = f"{summary} (corrective re-plan v{plan_version})"
    plan = Plan(
        id=str(uuid4()),
        execution_id=execution_id,
        version=plan_version,
        status="draft",
        summary=summary,
        is_acyclic=True,
    )
    db.add(plan)
    db.flush()  # Get plan ID

    # Create PlanNode records
    for i, step in enumerate(plan_steps):
        _out_type, _step_inputs = _sanitize_plan_step(step)
        node = PlanNode(
            id=str(uuid4()),
            plan_id=plan.id,
            seq=i,
            node_type=step.get("node_type", "tool"),
            name=step.get("name", f"Step {i+1}"),
            description=step.get("description", ""),
            dependencies=step.get("dependencies", []),
            inputs=_step_inputs,
            expected_output=step.get("expected_output"),
            output_artifact_type=_out_type,
            status="pending",
        )
        db.add(node)

    # Validate acyclicity. A cycle is SANITIZED (back-edges dropped), not
    # marked-invalid — the executor refuses cyclic graphs and the whole turn
    # died on a salvageable defect (measured live 2026-09-22).
    plan_steps, _dropped_deps = _sanitize_plan_dependencies(plan_steps)
    if _dropped_deps:
        logger.warning(
            "plan: sanitized dependency graph — dropped %s", _dropped_deps
        )
    plan_steps = _strip_sandbox_without_file_intent(plan_steps, task_spec)
    if not plan_steps:
        logger.warning("plan: empty after file-intent strip — default plan")
        plan_steps = default_plan

    # ── Enforce report decomposition on EVERY plan author (2026-09-22) ──
    # Measured live: the LLM planner authored ONE nl2sql node for a
    # three-dimension report ("sales by product, customer, organization")
    # — the 30-row grouped result was narrated as "top 3" and the report
    # shape was lost. When the TaskSpec names dimensions, the plan MUST
    # carry at least one nl2sql node per dimension (+ totals + prior
    # period); otherwise the deterministic decomposition replaces it.
    _dims = [str(d).strip() for d in (task_spec.get("dimensions") or []) if str(d).strip()]
    if (
        _dims
        and task_spec.get("requires_data")
        and not str(task_spec.get("user_signal") or "").startswith("export_")
    ):
        _nl_nodes = [st for st in plan_steps if st.get("node_type") == "nl2sql"]
        if len(_nl_nodes) < len(_dims) + 1:
            _dec = _build_report_decomposition_steps(task_spec)
            if _dec:
                logger.info(
                    "plan: report decomposition enforced (%d nl2sql nodes "
                    "replaces %d) for dimensions %s",
                    len([x for x in _dec if x["node_type"] == "nl2sql"]),
                    len(_nl_nodes), _dims,
                )
                plan_steps = _dec
    plan.is_acyclic = _validate_acyclic(plan_steps)

    db.commit()
    db.refresh(plan)
    return plan


def _build_skill_directive_block(task_spec: dict) -> str:
    """Return a hard-directive prompt block when the post-router hook fired.

    Empty string when no forced skill is present.  When a forced skill is
    set (carried from ``pick_default_skill`` → ``task_spec_parser`` →
    ``forced_skill_name``), returns a HARD instruction for the planner to
    emit a ``node_type: "skill"`` step that invokes the named skill as
    its very first action.  Both the planner-prompt consumer (in
    ``generate_plan``) and the fallback ``_build_default_plan`` consult
    this helper so the directive stays consistent across paths.

    Args:
        task_spec: The TaskSpec dict that may carry ``forced_skill``,
            ``forced_skill_name``, ``forced_skill_score``.

    Returns:
        A multi-line string to be embedded in the planner prompt, or
        an empty string when no forced skill applies.
    """
    if not task_spec.get("forced_skill"):
        return ""
    skill_name = task_spec.get("forced_skill_name")
    if not skill_name:
        return ""
    score = task_spec.get("forced_skill_score")
    score_str = f" (confidence={score:.2f})" if isinstance(score, (int, float)) else ""
    return (
        "\n[HARD DIRECTIVE — MUST FOLLOW]\n"
        f"The post-router skill auto-selector matched this request to skill "
        f"'{skill_name}'{score_str} with strong confidence. You MUST begin the "
        f"plan with a `node_type: \"skill\"` step that invokes '{skill_name}' "
        f"(e.g. via the Skill meta-tool with command `\"execute {skill_name}\"`). "
        f"All subsequent steps must depend on that skill node. Do NOT skip it "
        f"or substitute a different skill.\n"
    )


def _build_selected_skill_directive_block(task_spec: dict) -> str:
    """Return a hard-directive prompt block for an explicit user-selected skill.

    FIX 2026-08-23: now includes the skill's SKILL.md methodology body so
    the planner sees what the skill actually does, not just its name.
    """
    selected = task_spec.get("selected_skill") or {}
    skill_name = task_spec.get("selected_skill_name") or selected.get("name")
    skill_id = task_spec.get("selected_skill_id") or selected.get("id")
    skill_methodology = task_spec.get("selected_skill_methodology")
    if not skill_name and not skill_id:
        return ""
    id_hint = f" (id={skill_id})" if skill_id else ""
    block = (
        "\n[HARD DIRECTIVE — EXPLICIT USER SELECTION]\n"
        f"The user explicitly selected the runtime skill '{skill_name or skill_id}'{id_hint}. "
        "You MUST begin the plan with a `node_type: \"skill\"` step that loads this exact skill. "
        "Preserve the selected skill id/name in the step inputs so execution can load the exact skill body. "
        "Do NOT substitute a default skill, post-router skill, or generic format skill.\n"
    )
    if skill_methodology:
        block += (
            f"\n<selected_skill_methodology>\n{skill_methodology}\n</selected_skill_methodology>\n"
            "The plan steps MUST follow this methodology. Read each section above and "
            "emit a plan step for each major phase the skill defines.\n"
        )
    return block


def _build_default_plan(task_spec: dict, agent_name: str) -> list[dict]:
    """Build a default plan based on task kind (fallback).

    When the user explicitly asked for a file format (user_signal starts
    with 'export_'), emits a 3-node multi-agent DAG:

        1. nl2sql (data_analyst)  — fetch data from the datasource
        2. synthesize (synthesizer) — LLM writes the executive summary
        3. sandbox (presenter)    — Presentation Designer builds the file

    For non-file requests, emits the analysis-only plan (nl2sql if data
    needed → optional sandbox). When neither applies, the plan is EMPTY —
    zero tool nodes is a deliberate no-tool turn answered directly in
    FINALIZE; it must NOT fall back to a placeholder tool node.
    """
    task_kind = task_spec.get("task_kind", "general")
    artifact_intents = task_spec.get("artifact_intents", [])
    requires_data = task_spec.get("requires_data", False)
    user_signal = task_spec.get("user_signal", "default")

    steps = []

    # Question the nl2sql node should ask the data agent (Phase 3a: concrete
    # node inputs so tool nodes don't execute with empty args — G4).
    _question = (
        task_spec.get("standalone_question")
        or task_spec.get("user_message")
        or (task_spec.get("entities", {}) or {}).get("question")
        or ""
    )
    _entities = task_spec.get("entities", {}) or {}
    _report_title = _entities.get("report_title")

    # ── Follow-up artifact edit (Phase 4: chat-based refinement) ──────────
    # When the user is refining an existing artifact (is_followup +
    # refines_artifact_id), emit an edit_artifact tool node.  The planner
    # prompt also receives the follow-up rules block (see generate_plan)
    # so the LLM planner can do the same when enabled, but the curated
    # default path must handle this explicitly.
    is_followup = task_spec.get("is_followup", False)
    refines_artifact_id = task_spec.get("refines_artifact_id")
    if is_followup and refines_artifact_id:
        # Determine the artifact type from context
        artifact_type = (
            (artifact_intents[0] if artifact_intents else None)
            or task_spec.get("previous_artifact_type")
            or "pptx"
        )
        # If user is also asking for fresh data, prepend a data step
        if requires_data:
            steps.append({
                "node_type": "nl2sql",
                "name": "Retrieve updated data",
                "description": "Query the datasource for updated data to feed into the edit",
                "dependencies": [],
                "expected_output": "DataSnapshot",
                "inputs": {"question": _question},
            })
        # Emit the edit step
        steps.append({
            "node_type": "tool",
            "name": f"Edit {artifact_type} artifact",
            "description": (
                f"Refine artifact id={refines_artifact_id} "
                f"based on user instruction"
            ),
            "tool_name": "edit_artifact",
            "dependencies": [len(steps) - 1] if steps else [],
            "expected_output": f"Updated {artifact_type} artifact",
            "output_artifact_type": artifact_type,
            "inputs": {
                "artifact_id": str(refines_artifact_id),
                "instructions": _question,
            },
        })
        return steps

    # ── Explicitly selected runtime skill ────────────────────────────────
    selected_skill = task_spec.get("selected_skill") or {}
    selected_skill_name = task_spec.get("selected_skill_name") or selected_skill.get("name")
    selected_skill_id = task_spec.get("selected_skill_id") or selected_skill.get("id")
    if selected_skill_name or selected_skill_id:
        steps.append({
            "node_type": "skill",
            "name": f"Load selected skill: {selected_skill_name or selected_skill_id}",
            "description": "Load the exact runtime-selected skill before synthesis or artifact generation.",
            "skill": selected_skill_name or selected_skill_id,
            "dependencies": [],
            "expected_output": "Skill activation context",
            "inputs": {
                "skill_name": selected_skill_name or "",
                "skill_id": selected_skill_id or "",
            },
        })

    # ── Forced skill (post-router hook strong match) ──────────────────────
    # When the post-router hook fires, the plan MUST begin with a skill
    # node that invokes the forced skill. Subsequent steps depend on it.
    if not steps and task_spec.get("forced_skill") and task_spec.get("forced_skill_name"):
        steps.append({
            "node_type": "skill",
            "name": f"Load skill: {task_spec['forced_skill_name']}",
            "description": (
                f"Invoke the '{task_spec['forced_skill_name']}' skill "
                f"(post-router-hook forced match; see <forced_skill> directive)."
            ),
            "skill": task_spec["forced_skill_name"],
            "dependencies": [],
            "expected_output": "Skill activation context",
            "inputs": {"skill_name": task_spec["forced_skill_name"]},
        })

    # ── Multi-agent DAG for file-format requests ──────────────────────
    if user_signal.startswith("export_"):
        if requires_data:
            steps.append({
                "node_type": "nl2sql",
                "name": "Retrieve data",
                "description": "Query the datasource for required data",
                "agent_role": "data_analyst",
                "dependencies": [],
                "expected_output": "DataSnapshot",
                "inputs": {"question": _question},
            })

        # Step 2: Synthesizer — LLM writes executive summary as
        # natural-language instructions for the Presentation Designer
        steps.append({
            "node_type": "synthesize",
            "name": "Write report summary",
            "description": "Synthesize data into an executive summary with KPIs and chart data",
            "agent_role": "synthesizer",
            "dependencies": [len(steps) - 1] if steps else [],
            "expected_output": "ReportCardPayload + executive summary text",
            "inputs": {},
        })

        # Step 3: Sandbox — Presentation Designer builds the file
        artifact_type = artifact_intents[0] if artifact_intents else "docx"
        steps.append({
            "node_type": "sandbox",
            "name": f"Generate {artifact_type}",
            "description": f"Create {artifact_type} artifact in Presentation Designer sandbox",
            "agent_role": "presenter",
            "dependencies": [len(steps) - 1],
            "expected_output": f"{artifact_type} file",
            "output_artifact_type": artifact_type,
            "inputs": {"title": _report_title or f"{artifact_type} report", "format": artifact_type},
        })

        return steps

    # ── Report decomposition (2026-09-22): multi-dimension report
    # questions are answered by MULTIPLE nl2sql nodes (totals + per
    # dimension + prior period) feeding one synthesize — one SQL statement
    # cannot produce a three-dimension report, and the measured failure
    # routed them into a 2-row file instead of an analysis.
    if (
        not steps
        and task_kind in ("analyze_data", "answer_question", "general")
        and requires_data
        and not user_signal.startswith("export_")
    ):
        _report_steps = _build_report_decomposition_steps(task_spec)
        if _report_steps:
            return _report_steps

    # ── Standard (non-file) plan ───────────────────────────────────────
    if requires_data:
        steps.append({
            "node_type": "nl2sql",
            "name": "Retrieve data",
            "description": "Query the datasource for required data",
            "dependencies": [],
            "expected_output": "DataSnapshot",
            "inputs": {"question": _question},
        })

    if artifact_intents and _intent_is_renderable(artifact_intents[0]):
        artifact_type = artifact_intents[0]
        steps.append({
            "node_type": "sandbox",
            "name": f"Generate {artifact_type}",
            "description": f"Create {artifact_type} artifact in sandbox",
            "dependencies": [len(steps) - 1] if steps else [],
            "expected_output": f"{artifact_type} file",
            "output_artifact_type": artifact_type,
            "inputs": {"title": _report_title or f"{artifact_type} report", "format": artifact_type},
        })

    # NOTE (2026-10-08): the old fallback appended a tool node literally
    # named "Process request" whenever the builder produced nothing — a node
    # NAME, not a real tool. _execute_tool_node dispatched it via
    # execute_tool → "Unknown tool: Process request" (agent_tools.py), the
    # corrective re-plan retried and failed identically, and FINALIZE
    # narrated the whole plumbing failure to the user ("tool Process request
    # is not available" / "no data source is connected"). An EMPTY plan is
    # the honest fallback: execute_plan_nodes returns zero observations and
    # the FINALIZE prompt answers the user directly (see fsm.py
    # _build_response_prompt, _no_tool_turn branch). Verified: every
    # consumer of plan.nodes (execute_plan_nodes, confidence_scorer,
    # policy_evaluator, replan collect) handles an empty node list.
    if not steps:
        logger.info(
            "plan: default plan is EMPTY (task_kind=%s requires_data=%s "
            "artifacts=%s) — zero tool nodes; FINALIZE will answer directly",
            task_kind, requires_data, artifact_intents,
        )

    return steps


def _drop_unknown_steps(steps: list) -> tuple[list, list[str]]:
    """Drop plan steps that name a tool or skill which does not exist.

    Returns ``(kept_steps, dropped_labels)``. Fail-open: if a registry cannot be
    read the plan is returned untouched, because silently emptying a plan is
    worse than executing one bad step.

    Why this exists: the LLM planner invented names it had no way to verify
    (``read_attached_deck``, ``attached_deck_parser``, ``compose_answer``,
    ``extract_deck_facts``, ``compose_final_answer``). Every such node failed at
    execution, and the answer generator then reported those failures to the user
    as an execution narrative — a plumbing leak produced entirely by names that
    were never real. Instructions in the prompt are the first line of defence;
    this is the second.
    """
    if not isinstance(steps, list) or not steps:
        return (steps if isinstance(steps, list) else []), []

    try:
        from app.services.tool_registry import registry

        known_tools = set(registry.list_names() or [])
    except Exception as e:
        logger.debug("plan validation: tool registry unavailable (%s) — skipping", e)
        return steps, []

    if not known_tools:
        # An EMPTY registry means tool discovery has not run in this process —
        # NOT that no tool exists (verified: the registry reads 0 at import time
        # but 44 during a real request). Dropping every tool step on an empty
        # registry would silently replace a real plan with the generic default,
        # so treat it as "cannot validate" and leave the plan untouched.
        logger.debug("plan validation: tool registry EMPTY — skipping validation")
        return steps, []

    known_skills: set = set()
    try:
        from app.services.skills_loader import get_skills_registry

        _skills_reg = get_skills_registry()
        # SkillsRegistry exposes list_skills() -> [SkillMetadata], NOT
        # list_names(). The old hasattr(list_names) probe was therefore always
        # False, so known_skills stayed empty and THIS BRANCH NEVER VALIDATED
        # ANYTHING: an invented skill name passed straight through and was only
        # discovered at execution, where it was marked "skipped".
        # Observed live: the planner invented skills `render_bar_chart` and
        # `create_bar_chart_spec` (neither exists) for a chart request, every
        # node skipped, and the turn delivered no chart at all. Read the real
        # names from whichever accessor this registry actually provides.
        if hasattr(_skills_reg, "list_skills"):
            for _s in _skills_reg.list_skills() or []:
                _n = getattr(_s, "name", None) or (
                    _s.get("name") if isinstance(_s, dict) else None
                )
                if _n:
                    known_skills.add(str(_n))
        elif hasattr(_skills_reg, "list_names"):
            known_skills = set(_skills_reg.list_names() or [])
        logger.debug("plan validation: %d skill names loaded", len(known_skills))
    except Exception as e:
        logger.debug("plan validation: skill registry unavailable (%s)", e)

    kept: list = []
    dropped: list[str] = []
    for step in steps:
        if not isinstance(step, dict):
            kept.append(step)
            continue

        node_type = str(step.get("node_type") or "tool").lower()
        inputs = step.get("inputs") if isinstance(step.get("inputs"), dict) else {}
        # _execute_tool_node prefers inputs.tool_name over the human-readable
        # node name, so validate whichever name will actually be dispatched.
        name = str(inputs.get("tool_name") or step.get("name") or "").strip()

        if node_type in ("tool", "agent"):
            # Unknown node types are dispatched as tools too, so an "agent"
            # step whose name is not a registered tool cannot succeed either.
            if not name or name in known_tools:
                kept.append(step)
            else:
                dropped.append(f"{node_type}:{name}")
        elif node_type == "skill":
            # Only enforce skill names when the registry actually loaded —
            # otherwise every skill step would be dropped on a transient failure.
            if not name or not known_skills or name in known_skills:
                kept.append(step)
            else:
                dropped.append(f"skill:{name}")
        else:
            # nl2sql / sandbox / synthesize are engine nodes, not registry lookups.
            kept.append(step)

    return kept, dropped


def _validate_acyclic(steps: list[dict]) -> bool:
    """Validate that the plan DAG has no cycles (topological sort check)."""
    # Build adjacency list
    n = len(steps)
    graph = {i: [] for i in range(n)}
    in_degree = {i: 0 for i in range(n)}

    for i, step in enumerate(steps):
        for dep in step.get("dependencies", []):
            if isinstance(dep, int) and 0 <= dep < n:
                graph[dep].append(i)
                in_degree[i] += 1

    # Kahn's algorithm
    queue = [i for i in range(n) if in_degree[i] == 0]
    visited = 0

    while queue:
        node = queue.pop(0)
        visited += 1
        for neighbor in graph[node]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    return visited == n


def _format_plan_context_block(conv_ctx: dict, task_spec: dict) -> str:
    """Render conversation context as a bounded block for the planner prompt.

    Returns ``""`` when there is nothing useful to surface.  Kept compact
    so it never crowds out the core plan instructions.
    """
    if not conv_ctx:
        return ""
    parts = ["\n=== Conversation context (use for follow-up turns) ==="]
    transcript = (conv_ctx.get("transcript") or "").strip()
    if transcript:
        parts.append("Recent turns:\n" + transcript)
    artifacts = conv_ctx.get("recent_artifacts") or []
    if artifacts:
        art_lines = [
            f"  - id={a.get('id', '?')}, type={a.get('artifact_type', '?')}, "
            f"title={a.get('title', '?')}"
            for a in artifacts[:5]
        ]
        parts.append("Recent artifacts:\n" + "\n".join(art_lines))
    # Prior datasets: when reuse_prior_data is set, the planner should
    # generate a synthesis-only plan (no data_query nodes needed).
    prior_datasets = conv_ctx.get("prior_datasets") or task_spec.get("prior_datasets")
    reuse = task_spec.get("reuse_prior_data", False)
    if prior_datasets:
        ds_summary = []
        for i, ds in enumerate(prior_datasets[:3]):
            nrows = len(ds.get("rows", []))
            src = ds.get("source_name") or ds.get("source_id") or "?"
            ds_summary.append(f"  - Dataset {i+1}: {nrows} rows from {src}")
        parts.append("Prior datasets (already collected):\n" + "\n".join(ds_summary))
        if reuse:
            parts.append(
                "IMPORTANT: This request is a follow-up refinement (e.g. 'a summary', "
                "'break it down'). Prior datasets cover the scope. Do NOT generate "
                "data_query nodes — generate a synthesis-only plan that answers the "
                "user's question using the prior datasets."
            )
    if len(parts) == 1:
        return ""
    return "\n".join(parts) + "\n"
