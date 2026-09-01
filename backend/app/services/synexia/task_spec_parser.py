"""TaskSpec parser — converts user message into a typed task specification.

The TaskSpec is the structured representation of what the user wants:
- task_kind: what type of task (create_artifact, answer_question, analyze_data, etc.)
- artifact_intents: what artifacts the user wants (pptx, docx, chart, etc.)
- entities: key entities mentioned (dates, products, metrics)
- kpis: success criteria
- user_signal: the UI signal for the frontend (default, export_docx, export_pptx, etc.)

Deterministic file-intent detection runs BEFORE the LLM so the format the
user explicitly asked for is ALWAYS baked into the TaskSpec, regardless of
what the LLM hallucinates.
"""

import json
import logging
from typing import Optional

from app.services.synexia.intent_router import (
    EXPORT_SIGNAL_BY_FORMAT,
    detect_file_intent,
    user_signal_for_format,
)
from app.services.synexia.default_skills import (
    pick_default_skill,
    is_override_skill,
)

logger = logging.getLogger(__name__)

# Refinement-verb patterns that signal a follow-up turn (case-insensitive).
# Phase 4: expanded with document-specific edit verbs and change-intent patterns.
_REFINEMENT_VERBS = (
    "better", "improve", "redo", "redesign", "change", "update", "modify",
    "adjust", "tweak", "fix", "dark", "light", "theme", "color", "colour",
    "style", "design", "add", "remove", "replace", "shorten", "expand",
    "simpler", "more", "less", "again", "instead",
    # Phase 4 additions: document-specific edit verbs
    "rename", "retitle", "edit", "revise", "rewrite", "reword",
    "restructure", "reorder", "reformat", "convert",
)

# Document-editing intent patterns (Phase 4): when the user mentions
# specific document elements, this is almost certainly an edit, not a
# new request.  These match at the message level.
_DOCUMENT_EDIT_PATTERNS = (
    "title slide", "summary slide", "cover page", "table of contents",
    "make it", "make the", "make this", "change it", "change the",
    "change this", "switch to", "convert to", "add a slide", "add slide",
    "remove slide", "delete slide", "move slide",
    "more professional", "more formal", "more concise", "more detailed",
    "less text", "more data", "bigger font", "smaller font",
    "this slide", "that slide", "the chart", "the table",
    "kpi section", "executive summary", "recommendations section",
    "can you make", "could you make", "please make", "please change",
)


def _detect_followup_hint(user_message: str, ctx: Optional[dict]) -> str:
    """Cheap deterministic pre-check that biases the LLM's is_followup.

    Returns a short hint string for the system prompt, or ``""`` when no
    signal is found.  Never forces the outcome — the LLM still decides.

    Phase 4: also checks document-specific edit patterns (e.g. "add a
    slide", "make it more professional").
    """
    if not ctx:
        return ""
    artifacts = ctx.get("recent_artifacts") or []
    if not artifacts:
        return ""
    msg_lower = (user_message or "").lower().strip()
    if not msg_lower:
        return ""

    # Check refinement verbs (original check)
    has_verb = any(verb in msg_lower for verb in _REFINEMENT_VERBS)
    # Phase 4: also check document-edit-specific patterns
    has_doc_pattern = any(pattern in msg_lower for pattern in _DOCUMENT_EDIT_PATTERNS)

    # Long messages with edit patterns are still edits (e.g. "can you
    # please make the summary slide more professional and also add my
    # name at the bottom")
    if has_doc_pattern:
        return (
            "\nHeuristic: the message contains document-editing language "
            "('add a slide', 'make it more professional', etc.) and the "
            "conversation already produced an artifact — this is a "
            "follow-up edit request. Set is_followup=true and "
            "refines_artifact_id to the most relevant prior artifact id.\n"
        )

    if len(msg_lower) > 300:
        return ""

    if has_verb:
        return (
            "\nHeuristic: the message is short, uses refinement language, "
            "and the conversation already produced an artifact — this is "
            "very likely a follow-up. Set is_followup=true and "
            "refines_artifact_id to the most relevant prior artifact id.\n"
        )
    return ""


def _format_context_block(ctx: Optional[dict]) -> str:
    """Render conversation_context as a compact prompt block, or ``""``.

    Phase 4: includes artifact_type for each recent artifact so the
    LLM can carry forward the format when editing.
    """
    if not ctx:
        return ""
    parts = []
    transcript = (ctx.get("transcript") or "").strip()
    if transcript:
        parts.append("=== Conversation so far ===\n" + transcript)
    artifacts = ctx.get("recent_artifacts") or []
    if artifacts:
        art_lines = []
        for a in artifacts[:5]:
            art_lines.append(
                f"- id={a.get('id', '?')}, type={a.get('artifact_type', '?')}, "
                f"title={a.get('title', '?')}"
            )
        parts.append("=== Recent artifacts in this conversation ===\n" + "\n".join(art_lines))
    prior_entities = ctx.get("prior_entities") or {}
    if prior_entities:
        parts.append("=== Entities from the previous turn (inherit unless overridden) ===\n"
                     + json.dumps(prior_entities, ensure_ascii=False))
    # Phase 4: carry forward previous_artifact_type for edit context
    prev_artifact_type = ctx.get("previous_artifact_type")
    if prev_artifact_type:
        parts.append(f"\nPrevious artifact type: {prev_artifact_type}")
    if not parts:
        return ""
    return "\n".join(parts) + "\n"


def parse_task_spec(
    user_message: str,
    agent_name: str = "general_assistant",
    active_skill: Optional[dict] = None,
    conversation_context: Optional[dict] = None,
    db=None,
    endpoint=None,
) -> dict:
    """Parse a user message into a TaskSpec using the LLM.

    Returns a dict with:
        task_kind: str — "create_artifact" | "answer_question" | "analyze_data" | "configure_system" | "general"
        artifact_intents: list[str] — ["pptx", "docx", "chart", ...]
        entities: dict — {"date_range": "Q2 2025", "metric": "revenue", ...}
        kpis: list[str] — ["accuracy", "completeness", "timeliness"]
        acceptance_criteria: list[str] — concrete, checkable success criteria
            (e.g. "report must include revenue figures", "must cite data
            sources"). Consumed by the QUALITY_EVAL phase to judge whether
            the generated response actually satisfies the task.
        complexity: str — "simple" | "moderate" | "complex"
        requires_data: bool — whether data retrieval is needed
        user_signal: str — "default" | "export_docx" | "export_pptx" | etc.
        is_followup: bool — True when this turn refines a prior artifact/turn
        refines_artifact_id: str | None — the prior artifact being refined

    Deterministic file-intent detection runs before the LLM so the
    artifact_intents and user_signal fields are never left to chance.
    The detected format is also baked into the LLM's system prompt so the
    narrative (title, summary column names) matches the expected format.

    When ``conversation_context`` is provided (transcript + recent
    artifacts + prior entities), it is injected into the system prompt so
    the LLM can detect follow-up/refinement intent and inherit entities
    from the previous turn instead of treating every message as a
    brand-new, context-free request.
    """
    from app.services.llm_service import call_llm

    # ── Default-skill override/auto-pick (BEFORE the LLM) ─────────────
    # Defense-in-depth layer 2: determine whether the user picked a
    # custom skill (override → skip defaults) or left it blank
    # (auto-pick the right default skill).
    skill_override = is_override_skill(active_skill)
    auto_picked_default = None
    forced_skill = False
    forced_skill_name = None
    forced_skill_score = None
    if not skill_override:
        default_skill = pick_default_skill(user_message, active_skill=None)
        if default_skill:
            auto_picked_default = default_skill["skill_name"]
            if default_skill.get("forced"):
                forced_skill = True
                forced_skill_name = default_skill["skill_name"]
                forced_skill_score = default_skill.get("score")

    # ── Deterministic file-intent detection (BEFORE the LLM) ──────────
    requested_fmt = detect_file_intent(user_message)
    user_signal = (
        user_signal_for_format(requested_fmt)
        if requested_fmt
        else "default"
    )
    override_artifact_intents = [requested_fmt] if requested_fmt else None

    # ── Follow-up heuristic (BEFORE the LLM) ──────────────────────────
    # Cheap deterministic pre-check: a short message that uses refinement
    # verbs while the conversation already produced an artifact is very
    # likely a follow-up.  This biases the LLM's is_followup output and
    # is never forced — the LLM still makes the final call.
    followup_hint = _detect_followup_hint(user_message, conversation_context)

    # ── Build the context block from conversation_context ─────────────
    context_block = _format_context_block(conversation_context)

    # ── Build the system prompt, injecting the detected format ────────
    format_hint = (
        f"\nThe user explicitly asked for a '{requested_fmt}' file. "
        f"Set user_signal='{user_signal}' and include '{requested_fmt}' "
        "in artifact_intents."
        if requested_fmt
        else ""
    )

    system_prompt = f"""Analyze the user's message and produce a TaskSpec JSON object.

Agent: {agent_name}

Classify the task:
- task_kind: "create_artifact" (make a PPT/DOCX/chart/dashboard), "answer_question" (factual response), "analyze_data" (data analysis), "configure_system" (create/modify agents/skills/automations), "general" (other)
- artifact_intents: list of artifact types the user wants (pptx, docx, pdf, md, html, chart, dashboard, xlsx, image, mini_app). Empty if none.
- entities: dict of key entities (date_range, metric, product, department, etc.)
- kpis: list of success criteria (abstract, e.g. "accuracy", "completeness")
- acceptance_criteria: list of CONCRETE, checkable criteria the final reply must satisfy (e.g. "report must include revenue figures", "must cite the data source", "must contain a chart of Q2 sales"). These are judged by the QUALITY_EVAL phase after the response is generated.
- complexity: "simple" | "moderate" | "complex"
- requires_data: true if data retrieval/analysis is needed
- user_signal: "default" | "export_docx" | "export_pptx" | "export_xlsx" | "export_pdf" | "export_md"{format_hint}
- is_followup: true if this message refines or updates an artifact/result from a previous turn in this conversation (e.g. "make it better", "dark theme", "add a chart"). false for a brand-new request.
- refines_artifact_id: if is_followup is true and the conversation lists a prior artifact being refined, set this to that artifact's id; otherwise null.

{followup_hint}{context_block}
Respond with ONLY a JSON object, no explanation."""

    # ── TaskSpec JSON schema ─────────────────────────────────────────
    # DeepSeek (the default routing model for background FSM calls)
    # ignores prose "respond with ONLY JSON" instructions and role-plays
    # a refusal instead — which silently breaks TaskSpec parsing and
    # degrades every request to the generic default plan (the agent then
    # reports "tool Process request is not available" and claims it has
    # no database access).  Passing a real JSON schema makes the
    # provider enforce json_object output (build_llm_payload sets
    # response_format={"type":"json_object"} and injects the schema
    # hint), so the parse always succeeds regardless of which model the
    # router picked.
    _task_spec_schema = {
        "type": "object",
        "properties": {
            "task_kind": {
                "type": "string",
                "enum": ["create_artifact", "answer_question", "analyze_data", "configure_system", "general"],
            },
            "artifact_intents": {"type": "array", "items": {"type": "string"}},
            "entities": {"type": "object"},
            "kpis": {"type": "array", "items": {"type": "string"}},
            "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
            "complexity": {"type": "string", "enum": ["simple", "moderate", "complex"]},
            "requires_data": {"type": "boolean"},
            "user_signal": {"type": "string"},
            "is_followup": {"type": "boolean"},
            "refines_artifact_id": {"type": ["string", "null"]},
        },
        "required": ["task_kind", "artifact_intents", "entities", "requires_data", "is_followup"],
    }

    try:
        # call_llm is async; this parser runs in a sync context (FSM GOAL
        # state).  Bare-calling it returns a coroutine and `.get()` blows
        # up with "coroutine object has no attribute 'get'", silently
        # falling back to the generic default TaskSpec (which then plans
        # a useless "Process request" tool step and the agent claims it
        # has no data access).  Reuse plan_dag's sync runner so the LLM
        # parse actually happens.
        from app.services.synexia.plan_dag import _run_llm_sync

        result = _run_llm_sync(call_llm(
            prompt=system_prompt,
            messages=[{"role": "user", "content": user_message}],
            temperature=0,
            response_json_schema=_task_spec_schema,
            endpoint=endpoint,
        ))

        # When response_json_schema is used, call_llm parses the JSON and
        # returns it under ``data`` (plus top-level key merge) — NOT under
        # ``response`` (that stays None).  Fall back gracefully so the
        # schema path and the plain-text path both work.
        _parsed = result.get("data")
        if isinstance(_parsed, dict):
            task_spec = _parsed
        else:
            response_text = (result.get("response") or "{}").strip()

            # Extract JSON from response
            if "```json" in response_text:
                response_text = response_text.split("```json")[1].split("```")[0]
            elif "```" in response_text:
                response_text = response_text.split("```")[1].split("```")[0]

            task_spec = json.loads(response_text)

        # Ensure required fields
        task_spec.setdefault("task_kind", "general")
        task_spec.setdefault("artifact_intents", [])
        task_spec.setdefault("entities", {})
        task_spec.setdefault("kpis", [])
        task_spec.setdefault("acceptance_criteria", [])
        task_spec.setdefault("complexity", "simple")
        task_spec.setdefault("requires_data", False)
        task_spec.setdefault("user_signal", "default")
        task_spec.setdefault("is_followup", False)
        task_spec.setdefault("refines_artifact_id", None)
        task_spec.setdefault("previous_artifact_type", "")
        task_spec["skill_override"] = skill_override
        task_spec["auto_picked_default"] = auto_picked_default
        task_spec["forced_skill"] = forced_skill
        task_spec["forced_skill_name"] = forced_skill_name
        task_spec["forced_skill_score"] = forced_skill_score
        if active_skill:
            task_spec["selected_skill"] = active_skill
            task_spec["selected_skill_id"] = active_skill.get("id")
            task_spec["selected_skill_name"] = active_skill.get("name")

            # FIX 2026-08-23: resolve skill methodology from DB so the
            # planner sees the full body and the plan follows it.
            _skill_id = active_skill.get("id")
            if _skill_id and db:
                try:
                    from app.models.tool import Tool
                    _tool_row = db.query(Tool).filter(Tool.id == _skill_id).first()
                    if _tool_row and _tool_row.skill_md:
                        task_spec["selected_skill_methodology"] = _tool_row.skill_md[:5000]
                except Exception:
                    pass  # Best-effort

        # ── Follow-up entity inheritance ──────────────────────────────
        # On a follow-up turn, merge prior entities so refinement
        # requests ("dark theme") inherit the topic/metric/date_range
        # from the prior turn instead of losing them.  Explicitly set
        # values in the current TaskSpec always win.
        if task_spec.get("is_followup") and conversation_context:
            prior = conversation_context.get("prior_entities") or {}
            merged = {**prior, **task_spec.get("entities", {})}
            task_spec["entities"] = merged

            # If the LLM didn't pin a refines_artifact_id but we have
            # recent artifacts, default to the most recent one.
            arts = conversation_context.get("recent_artifacts") or []
            if not task_spec.get("refines_artifact_id") and arts:
                task_spec["refines_artifact_id"] = arts[0].get("id")
                task_spec["previous_artifact_type"] = arts[0].get("artifact_type")
            # Phase 4: also carry forward previous_artifact_type from context
            if not task_spec.get("previous_artifact_type"):
                prev_at = conversation_context.get("previous_artifact_type")
                if prev_at:
                    task_spec["previous_artifact_type"] = prev_at

        # ── OVERRIDE: deterministic file-intent wins over the LLM ─────
        if override_artifact_intents:
            # Force the detected format into artifact_intents (dedupe)
            existing = task_spec.get("artifact_intents", [])
            merged = list(dict.fromkeys(override_artifact_intents + existing))
            task_spec["artifact_intents"] = merged
            task_spec["user_signal"] = user_signal
            # If a file format is requested, force requires_data=True so
            # the planner emits data-retrieval + synthesize + sandbox
            if requested_fmt and not task_spec.get("requires_data"):
                task_spec["requires_data"] = True
                # Override task_kind to create_artifact so the planner
                # classifies it correctly
                if task_spec["task_kind"] == "general":
                    task_spec["task_kind"] = "create_artifact"

        logger.info(
            "TaskSpec: kind=%s, artifacts=%s, user_signal=%s, complexity=%s",
            task_spec["task_kind"], task_spec["artifact_intents"],
            task_spec["user_signal"], task_spec["complexity"],
        )
        return task_spec

    except Exception as e:
        logger.warning("TaskSpec parsing failed, using default: %s", e)
        default = {
            "task_kind": "general",
            "artifact_intents": override_artifact_intents or [],
            "entities": {},
            "kpis": [],
            "acceptance_criteria": [],
            "complexity": "simple",
            "requires_data": bool(requested_fmt),
            "user_signal": user_signal,
            "is_followup": False,
            "refines_artifact_id": None,
            "previous_artifact_type": (
                conversation_context.get("previous_artifact_type", "")
                if conversation_context else ""
            ),
            "skill_override": skill_override,
            "auto_picked_default": auto_picked_default,
            "forced_skill": forced_skill,
            "forced_skill_name": forced_skill_name,
            "forced_skill_score": forced_skill_score,
            "selected_skill": active_skill,
            "selected_skill_id": active_skill.get("id") if active_skill else None,
            "selected_skill_name": active_skill.get("name") if active_skill else None,
            "selected_skill_methodology": None,  # resolved lazily by planner if needed
        }
        return default
