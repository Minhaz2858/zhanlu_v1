"""Deterministic turn planning for the v3 agent loop (2026-08-27).

ROOT CAUSE FIXED
----------------
The agent loop previously had NO plan phase. ``plan_step_added`` SSE events
were only parsed from *voluntary* model prose ("1. ... 2. ..." written by the
LLM in its visible text) — and the local LLM (qwen3.6-27b) almost never
produces that prose. The loop therefore freewheeled: it skipped straight to
repeated ``ask_data_agent`` calls, never built the deliverable, and the user
saw "Verification failed — some steps did not complete" with no dashboard.

This module derives a DETERMINISTIC plan (todo list) from the user message
BEFORE the loop runs:

  1. ``build_turn_plan``     — intent-derived step list (dashboard / report /
                               data / generic templates), model-free.
  2. ``plan_step_added_frame`` — SSE frame emitted before the loop so the UI
                               shows the todo list immediately ("Laying out
                               the plan").
  3. ``plan_to_system_block`` — the plan injected into the LLM context so the
                               model FOLLOWS the plan instead of freewheeling.
  4. ``plan_completed_steps`` — deterministic mapping from executed tool names
                               → completed steps (evidence-based), so the loop
                               can tick steps off (``plan_step_completed``)
                               without trusting model prose.

Design rules:
- The plan is derived from the USER'S INPUT, never from the model.
- Evidence is tool-name based and prefix-matched (safe for sub-agent tools
  like ``ask_*`` and sandbox tools like ``run_sandbox_skill``).
- Every function is pure/stateless except the dataclass — easy to test.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from app.services.goal_contract import is_greeting  # noqa: E402

# Conversational smalltalk that the strict goal-contract greeting regex
# misses ("Hello, how are you?", "What can you do?", …). These turns need no
# plan — the loop answers directly, exactly as before.
_SMALLTALK_RE = re.compile(
    r"^(?:hello|hi|hey|yo)[,.\s]*(?:how are you|how do you do|what'?s up)[\s!?.,。！？]*$"
    r"|^how are you[\s!?？]*$"
    r"|^what can you do[\s!?？]*$"
    r"|^who are you[\s!?？]*$"
    r"|^what are you[\s!?？]*$",
    re.IGNORECASE,
)


# ── Plan kinds ────────────────────────────────────────────────────────────

KIND_NONE = "none"
KIND_GENERIC = "generic"
KIND_DATA = "data"
KIND_REPORT = "report"
KIND_DASHBOARD = "dashboard"

# Tool-name prefixes that EVIDENCE a plan step. ``_match`` is prefix-based so
# sub-agent tools (ask_data_agent, ask_rag_research), sandbox tools
# (run_sandbox_skill), and pipeline tools all map correctly.
_EVIDENCE_SCHEMA = ("describe_schema", "catalog", "list_tables", "get_schema", "query_catalog", "schema_")
_EVIDENCE_DATA = (
    "ask_data_agent", "execute_query", "execute_sql", "run_sql", "query_database",
    "fetch_data_batch", "run_query", "query_", "data_agent",
)
_EVIDENCE_BUILD = (
    "create_artifact", "create_dashboard", "create_fullstack_dashboard",
    "create_fullstack", "create_report", "generate_report",
    "run_sandbox_skill", "create_automation", "create_cad", "build_", "generate_artifact",
)
_EVIDENCE_VERIFY = ("verify", "self_eval", "evaluate", "run_verification", "check_", "validate_")


@dataclass
class TurnStep:
    """One step of the turn plan.

    ``key`` is the machine-readable step id (analyze / schema / data / build /
    verify / draft / deliver / understand / query / answer / execute /
    respond). ``evidence`` holds tool-name prefixes; when any executed tool
    matches, the step is considered complete (see ``plan_completed_steps``).
    Steps with no evidence (analyze / understand / answer / respond) complete
    deterministically: analyze+understand at plan build, answer+respond when
    the loop exits with content.
    """

    step_index: int
    key: str
    phase: str
    title_en: str
    title_zh: str
    evidence: tuple[str, ...] = field(default_factory=tuple)


@dataclass
class TurnPlan:
    """A deterministic, intent-derived todo list for one turn."""

    kind: str = KIND_NONE
    steps: list[TurnStep] = field(default_factory=list)

    def step_by_index(self, step_index: int) -> TurnStep | None:
        for s in self.steps:
            if s.step_index == step_index:
                return s
        return None


# ── Step templates ────────────────────────────────────────────────────────

def _steps(kind: str) -> list[TurnStep]:
    if kind == KIND_DASHBOARD:
        return [
            TurnStep(1, "analyze", "act", "Analyze the request", "分析需求"),
            TurnStep(2, "schema", "act", "Inspect the bound data source", "检查已绑定的数据源", _EVIDENCE_SCHEMA),
            TurnStep(3, "data", "act", "Gather revenue, order, region & product datasets", "采集营收、订单、区域与产品数据集", _EVIDENCE_DATA),
            TurnStep(4, "build", "act", "Build the live dashboard", "构建实时看板", _EVIDENCE_BUILD),
            TurnStep(5, "verify", "verify", "Verify the deliverable", "校验交付物", _EVIDENCE_VERIFY),
        ]
    if kind == KIND_REPORT:
        return [
            TurnStep(1, "analyze", "act", "Analyze the request", "分析需求"),
            TurnStep(2, "data", "act", "Gather the required data", "采集所需数据", _EVIDENCE_DATA),
            TurnStep(3, "draft", "act", "Draft the report", "撰写报告"),
            TurnStep(4, "deliver", "act", "Deliver the document", "交付文档", _EVIDENCE_BUILD),
        ]
    if kind == KIND_DATA:
        return [
            TurnStep(1, "understand", "act", "Understand the question", "理解问题"),
            TurnStep(2, "query", "act", "Query the bound data source", "查询已绑定的数据源", _EVIDENCE_DATA + _EVIDENCE_SCHEMA),
            TurnStep(3, "analyze", "act", "Analyze the results", "分析结果"),
            TurnStep(4, "answer", "act", "Write the answer", "撰写答复"),
        ]
    # generic
    return [
        TurnStep(1, "understand", "act", "Understand the request", "理解请求"),
        TurnStep(2, "execute", "act", "Execute the work", "执行任务", _EVIDENCE_SCHEMA + _EVIDENCE_DATA + _EVIDENCE_BUILD),
        TurnStep(3, "verify", "verify", "Verify the result", "校验结果", _EVIDENCE_VERIFY),
        TurnStep(4, "respond", "act", "Respond", "回复用户"),
    ]


def build_turn_plan(
    user_content: str | None,
    *,
    is_dashboard_build: bool = False,
    is_report_request: bool = False,
    tool_names: list[str] | None = None,
) -> TurnPlan:
    """Build the deterministic turn plan from the user's input.

    Intent precedence (mirrors the orchestrator's own routing):
    dashboard build > file/report deliverable > data question > generic.
    A greeting / empty request yields an empty plan (``kind == "none"``) —
    the loop behaves exactly as before for chitchat.
    """
    text = (user_content or "").strip()
    if not text or is_greeting(text) or _SMALLTALK_RE.match(text):
        return TurnPlan(kind=KIND_NONE)

    if is_dashboard_build:
        kind = KIND_DASHBOARD
    elif is_report_request:
        kind = KIND_REPORT
    elif _looks_like_data_question(text, tool_names or []):
        kind = KIND_DATA
    else:
        kind = KIND_GENERIC
    return TurnPlan(kind=kind, steps=_steps(kind))


def _looks_like_data_question(text: str, tool_names: list[str]) -> bool:
    """Heuristic: does this look like a data/business question?

    Explicit data markers win outright. Otherwise a bound query tool
    (ask_data_agent / execute_query / run_sql / query_database /
    fetch_data_batch) plus an interrogative phrasing ("?", "how", "show",
    "list", …) classifies as a data question. Plain statements ("write a
    poem") stay generic even when query tools are available.
    """
    lowered = text.lower()
    markers = (
        "sales", "revenue", "order", "volume", "profit", "customer", "trend",
        "summar", "how many", "how much", "compare", "report",
        "销售", "收入", "订单", "利润", "客户", "数据", "报表", "趋势", "统计",
    )
    if any(m in lowered for m in markers):
        return True
    has_query_tool = any(
        t.startswith(("ask_data_agent", "execute_query", "execute_sql", "run_sql",
                      "query_database", "fetch_data_batch", "query_"))
        for t in tool_names
    )
    if not has_query_tool:
        return False
    interrogatives = (
        "?", "？", "how", "what", "which", "show", "list", "find",
        "analy", "多少", "是什么", "怎么样", "展示", "查询", "哪些",
    )
    return any(q in lowered for q in interrogatives)


# ── Evidence mapping ──────────────────────────────────────────────────────

def plan_completed_steps(plan: TurnPlan, executed_tool_names: list[str]) -> set[int]:
    """Which plan steps are complete given the tools executed so far.

    Deterministic, model-free: a step is complete when ANY executed tool name
    starts with one of the step's evidence prefixes. Steps with no evidence
    are never marked here (they are handled by the callers: analyze/
    understand at plan build, answer/respond at loop exit with content).
    """
    if not plan or not plan.steps or not executed_tool_names:
        return set()
    done: set[int] = set()
    for step in plan.steps:
        if not step.evidence:
            continue
        for tool in executed_tool_names:
            if any(tool.startswith(prefix) for prefix in step.evidence):
                done.add(step.step_index)
                break
    return done


def mark_final_step_completed(plan: TurnPlan, completed: set[int], has_content: bool) -> set[int]:
    """Post-loop: answer/respond steps complete when content exists."""
    if not has_content or not plan or not plan.steps:
        return set()
    return {
        s.step_index
        for s in plan.steps
        if s.key in ("answer", "respond") and s.step_index not in completed
    }


# ── Prompt injection ──────────────────────────────────────────────────────

def plan_to_system_block(plan: TurnPlan) -> str:
    """The plan block injected into the LLM context before the loop."""
    if not plan or not plan.steps:
        return ""
    lines = [f"{i}. {s.title_en}" for i, s in enumerate(plan.steps, start=1)]
    return (
        "TURN PLAN — the user's request has been decomposed into the following "
        "steps. Execute them IN ORDER. Do not skip ahead, do not repeat a step "
        "that is already done, and do not narrate the plan — execute it.\n"
        + "\n".join(lines)
        + "\nWhen all steps are complete, write the final answer. If a step "
          "cannot be completed, state that plainly and continue with the next "
          "achievable step."
    )


# ── Dynamic (per-request) plan parsing ─────────────────────────────────────

DYNAMIC_PLAN_MAX_STEPS = 6
_DYNAMIC_TITLE_MAX = 80

# Keyword → evidence/step-key inference for LLM-generated step titles, so the
# same deterministic tick-off machinery works on dynamic plans.
_KEYWORD_SCHEMA = ("schema", "table", "column", "inspect", "structure", "field",
                   "layout", "catalog", "表结构", "字段", "数据源", "查看表")
_KEYWORD_DATA = ("data", "query", "fetch", "gather", "collect", "revenue", "sales",
                 "order", "metric", "kpi", "数据", "查询", "采集", "营收", "销售", "订单")
_KEYWORD_BUILD = ("build", "create", "generate", "construct", "make", "dashboard",
                  "report", "artifact", "render", "构建", "创建", "生成", "制作",
                  "看板", "报表", "交付")
_KEYWORD_VERIFY = ("verify", "check", "validate", "review", "test", "confirm",
                   "校验", "检查", "确认", "验证")
_KEYWORD_ANALYZE = ("analy", "understand", "examin", "read", "解析", "理解", "分析")
_KEYWORD_ANSWER = ("answer", "respond", "write", "summar", "explain", "总结",
                   "答复", "回复", "撰写", "解释")

_PLAN_LINE_RE = re.compile(
    r"(?m)^[ \t]*(?:"
    r"(?:\d+)[.、)）][ \t]+(?P<t1>[^\n]+)"                       # 1. Title
    r"|[-*•][ \t]+(?P<t2>[^\n]+)"                                # - Title / * Title
    r"|(?:Step|步骤)\s*\d+\s*[:：][ \t]*(?P<t3>[^\n]+)"          # Step 1: Title
    r")"
)


def infer_step_key(title: str, step_index: int) -> str:
    """Best-effort machine key for an LLM-generated step title.

    Priority order reflects the ACTION verb strength: analyze > verify >
    build > schema > data > answer > execute. "Create fullstack dashboard
    with live data bindings" is a BUILD step (dashboard wins over data);
    "Verify dashboard functionality and data accuracy" is VERIFY.
    """
    t = (title or "").lower()
    if any(k in t for k in _KEYWORD_ANALYZE):
        return "analyze"
    if any(k in t for k in _KEYWORD_VERIFY):
        return "verify"
    if any(k in t for k in _KEYWORD_BUILD):
        return "build"
    if any(k in t for k in _KEYWORD_SCHEMA):
        return "schema"
    if any(k in t for k in _KEYWORD_DATA):
        return "data"
    if any(k in t for k in _KEYWORD_ANSWER):
        return "answer"
    return "execute"


def infer_evidence_from_title(title: str) -> tuple[str, ...]:
    """Map an LLM-generated step title to ONE tool-evidence group.

    The dynamic plan keeps the same deterministic tick-off contract: a step
    completes when a SUCCESSFUL tool matches one of its prefixes. Evidence
    follows the step's PRIMARY key only — a "Create ... dashboard ... data"
    step gets build evidence, not data evidence; an analyze step gets none.
    """
    key = infer_step_key(title, 1)
    if key == "schema":
        return _EVIDENCE_SCHEMA
    if key == "data":
        return _EVIDENCE_DATA
    if key == "build":
        return _EVIDENCE_BUILD
    if key == "verify":
        return _EVIDENCE_VERIFY
    return ()  # analyze / answer / execute — no tool evidence


def _clean_llm_title(raw: str) -> str:
    title = re.sub(r"^[\-\d.、)）\s]+", "", (raw or "").strip())
    title = title.strip().strip('"').strip("'").strip()
    if len(title) > _DYNAMIC_TITLE_MAX:
        title = title[:_DYNAMIC_TITLE_MAX].rstrip() + "…"
    return title


def parse_dynamic_plan(raw: str | None, kind: str) -> TurnPlan | None:
    """Parse the planner LLM's output into a TurnPlan.

    Accepts ``{"steps": [{"title": ...}]}``, a bare list of objects or
    strings, or a plain numbered/bullet list ("1. title"). Returns None when
    the output cannot be parsed into 2..DYNAMIC_PLAN_MAX_STEPS valid steps —
    the caller then falls back to the fixed intent template.
    """
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    # Strip markdown code fences if the model wrapped the JSON.
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    titles: list[str] = []
    # 1) JSON: {"steps": [...]} | {"plan": [...]} | [...]
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            steps_raw = data.get("steps") or data.get("plan") or []
        elif isinstance(data, list):
            steps_raw = data
        else:
            steps_raw = None
        if isinstance(steps_raw, list):
            for item in steps_raw:
                if isinstance(item, dict) and item.get("title"):
                    titles.append(str(item["title"]))
                elif isinstance(item, str) and item.strip():
                    titles.append(item)
    except (ValueError, TypeError):
        titles = []
    # 2) Plain numbered / bullet list fallback.
    if not titles:
        for m in _PLAN_LINE_RE.finditer(text):
            t = m.group("t1") or m.group("t2") or m.group("t3")
            if t:
                titles.append(t)
    if not titles:
        return None
    titles = [_clean_llm_title(t) for t in titles if _clean_llm_title(t)]
    # Dedupe (case-insensitive), preserve order.
    seen: set[str] = set()
    uniq: list[str] = []
    for t in titles:
        k = t.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(t)
    titles = uniq
    if len(titles) < 2 or len(titles) > DYNAMIC_PLAN_MAX_STEPS:
        return None
    steps = [
        TurnStep(
            step_index=i,
            key=infer_step_key(t, i),
            phase="verify" if infer_step_key(t, i) == "verify" else "act",
            title_en=t,
            title_zh=t,  # LLM emits in the user's language; keep as-is
            evidence=infer_evidence_from_title(t),
        )
        for i, t in enumerate(titles, start=1)
    ]
    return TurnPlan(kind=kind, steps=steps)


def dynamic_plan_prompt(
    user_content: str,
    kind: str,
    tool_names: list[str] | None = None,
) -> tuple[str, list[dict]]:
    """System + user messages for the one-shot planning LLM call."""
    system = (
        "You are a strict task planner for an autonomous agent. "
        "Given the user's request, output a concise step-by-step execution "
        'plan as JSON ONLY: {"steps": [{"title": "..."}]}. Rules: '
        "2-6 steps; each title is a concrete action in the imperative mood, "
        "under 60 characters; no commentary, no markdown fences, no extra "
        "fields; the first step analyzes the request; include data-gathering "
        "and a final build or verify step when relevant."
    )
    user = (
        f"User request: {user_content}\n"
        f"Request type: {kind}\n"
        f"Available tools: {', '.join(tool_names or [])}\n"
        "Respond with ONLY the JSON plan."
    )
    return system, [{"role": "user", "content": user}]


# ── SSE frames ────────────────────────────────────────────────────────────

def plan_step_added_frame(step: TurnStep) -> str:
    """SSE frame announcing a plan step (rendered as a pending checklist row)."""
    return f'data: {json.dumps({"type": "plan_step_added", "step_index": step.step_index, "title": step.title_en}, ensure_ascii=False)}\n\n'


def plan_step_completed_frame(step: TurnStep) -> str:
    """SSE frame marking a plan step complete (checklist row ticks off)."""
    return f'data: {json.dumps({"type": "plan_step_completed", "step_index": step.step_index, "title": step.title_en}, ensure_ascii=False)}\n\n'
