"""PlanDAG generator — creates an execution plan from a TaskSpec.

The plan is a directed acyclic graph (DAG) of nodes.  Each node represents
one step: a skill call, tool call, sandbox job, or NL2SQL query.

The plan is visible to the user (via PlanEditor) and can be approved or
modified before execution (at the GATE state).
"""

import logging
from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.execution import Plan, PlanNode

logger = logging.getLogger(__name__)


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
Agent: {agent_name}{default_skill_hint}{context_block}{followup_rules}
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

            # With response_json_schema, call_llm returns the parsed
            # value under ``data`` (list for an array schema) — NOT under
            # ``response``.  Handle both the schema path (list) and the
            # plain-text path (JSON string).
            _parsed_plan = result.get("data")
            if isinstance(_parsed_plan, list) and len(_parsed_plan) > 0:
                plan_steps = _parsed_plan
            else:
                response_text = (result.get("response") or "[]").strip()
                if "```json" in response_text:
                    response_text = response_text.split("```json")[1].split("```")[0]
                elif "```" in response_text:
                    response_text = response_text.split("```")[1].split("```")[0]

                parsed = json.loads(response_text)
                if isinstance(parsed, list) and len(parsed) > 0:
                    plan_steps = parsed
        except Exception as e:
            logger.warning("LLM plan generation failed, using default: %s", e)
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
        node = PlanNode(
            id=str(uuid4()),
            plan_id=plan.id,
            seq=i,
            node_type=step.get("node_type", "tool"),
            name=step.get("name", f"Step {i+1}"),
            description=step.get("description", ""),
            dependencies=step.get("dependencies", []),
            inputs=step.get("inputs") or {},
            expected_output=step.get("expected_output"),
            output_artifact_type=step.get("output_artifact_type"),
            status="pending",
        )
        db.add(node)

    # Validate acyclicity
    plan.is_acyclic = _validate_acyclic(plan_steps)
    if not plan.is_acyclic:
        logger.warning("Plan has cycles — marking as invalid")
        plan.summary += " (WARNING: cycle detected)"

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

    For non-file requests, emits the existing analysis-only plan
    (nl2sql if data needed → optional sandbox → fallback tool).
    """
    task_kind = task_spec.get("task_kind", "general")
    artifact_intents = task_spec.get("artifact_intents", [])
    requires_data = task_spec.get("requires_data", False)
    user_signal = task_spec.get("user_signal", "default")

    steps = []

    # Question the nl2sql node should ask the data agent (Phase 3a: concrete
    # node inputs so tool nodes don't execute with empty args — G4).
    _question = (
        task_spec.get("user_message")
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

    if artifact_intents:
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

    if not steps:
        steps.append({
            "node_type": "tool",
            "name": "Process request",
            "description": "Handle the user request with available tools",
            "dependencies": [],
            "expected_output": "Text response",
            "inputs": {},
        })

    return steps


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
