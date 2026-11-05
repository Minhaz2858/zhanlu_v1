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
# Phase 2: instrument the server-authored plan so Phase 3 (delete the turn_planner
# heuristics) has divergence evidence — the same pattern as the other 12 rules.
from app.services.harness.rule_events import observe_rule  # noqa: E402

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
_EVIDENCE_VERIFY = ("verify", "evaluate", "run_verification", "check_", "validate_")

# ── P1.1/P1.2 (Phase 1): explicit intent taxonomy + plan enrichment ─────────
# Semantic classification is done by the PLANNER LLM inside the existing
# one-shot planning call — never a keyword router. The template path leaves
# intent_type empty (it cannot classify semantically) and only derives a
# conservative default execution strategy from the mechanical intent
# precedence it already implements.
# Phase 1.5: "unknown" is a FIRST-CLASS intent value — an unclassified turn
# is represented explicitly, never omitted (intent consistency).
INTENT_TYPES: tuple[str, ...] = (
    "conversational", "information_retrieval", "data_analysis", "research",
    "investigation", "decision_support", "report_generation",
    "operational_execution", "multi_step_task", "cross_department_task",
    "unknown",
)
RISK_LEVELS: tuple[str, ...] = ("low", "medium", "high", "critical")
# Phase 1.5: "plan_dag" — DAG-shaped execution (dependency-ordered agent
# dispatch) is an executable strategy, distinct from a flat multi_agent batch.
EXECUTION_STRATEGIES: tuple[str, ...] = (
    "direct_response",   # Strategy A — conversational, no tools
    "direct_tool",       # Strategy B — GA's own tools
    "single_specialist", # Strategy C — one domain specialist
    "multi_agent",       # Strategy D — supervisor batch / peer collab
    "plan_dag",          # Strategy E — dependency-ordered DAG dispatch
)


def _norm_plan_meta(raw: dict | None) -> dict:
    """Validate + normalize planner-supplied plan meta (tolerant, never raises).

    Unknown/invalid values are DROPPED (not guessed) and scalar list fields
    are wrapped, so a sloppy LLM payload degrades to an un-enriched plan
    instead of breaking the turn.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict = {}

    def _s(v) -> str | None:
        return str(v).strip() or None if v is not None else None

    def _lst(v) -> tuple[str, ...]:
        if v is None:
            return ()
        if isinstance(v, str):
            return (v.strip(),) if v.strip() else ()
        if isinstance(v, (list, tuple)):
            return tuple(str(x).strip() for x in v if str(x).strip())
        return ()

    it = _s(raw.get("intent_type"))
    if it in INTENT_TYPES:
        out["intent_type"] = it
    rl = _s(raw.get("risk_level"))
    if rl in RISK_LEVELS:
        out["risk_level"] = rl
    es = _s(raw.get("execution_strategy"))
    if es in EXECUTION_STRATEGIES:
        out["execution_strategy"] = es
    for k in ("objective_statement", "expected_outcome"):
        v = _s(raw.get(k)) or _s(raw.get("objective") if k == "objective_statement" else None)
        if v:
            out[k] = v[:600]
    for k in ("constraints", "acceptance_criteria", "verification_requirements"):
        v = _lst(raw.get(k))
        if v:
            out[k] = v
    ra = raw.get("required_approvals")
    if ra is not None:
        out["required_approvals"] = bool(ra) if not isinstance(ra, str) else ra.strip().lower() in ("true", "yes", "1")
    return out


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
    # 2026-11-02 (dynamic orchestration timeline): stable task identity +
    # planner-provided user-friendly description + dependencies. task_id is
    # CONTENT-derived (sha1 of normalized key+title) so the same task keeps
    # its id across replans/reordering — the frontend matches on it to
    # preserve completed rows and mark superseded ones. Empty for legacy
    # constructions until assign_task_ids runs.
    task_id: str = ""
    description: str | None = None
    depends_on: tuple[int, ...] = field(default_factory=tuple)


@dataclass
class TurnPlan:
    """A deterministic, intent-derived todo list for one turn.

    P1.1/P1.2 (Phase 1): optional enrichment meta. All fields default empty —
    the template path and legacy LLM payloads stay byte-for-byte backward
    compatible. The ORIGINAL user request is never replaced: the meta fields
    carry the planner's structured understanding ALONGSIDE it (the loop keeps
    the raw user message as the authoritative objective).
    """

    kind: str = KIND_NONE
    steps: list[TurnStep] = field(default_factory=list)
    intent_type: str | None = None
    objective_statement: str | None = None
    expected_outcome: str | None = None
    constraints: tuple[str, ...] = field(default_factory=tuple)
    acceptance_criteria: tuple[str, ...] = field(default_factory=tuple)
    verification_requirements: tuple[str, ...] = field(default_factory=tuple)
    risk_level: str | None = None
    required_approvals: bool = False
    execution_strategy: str | None = None

    def step_by_index(self, step_index: int) -> TurnStep | None:
        for s in self.steps:
            if s.step_index == step_index:
                return s
        return None

    def plan_meta(self) -> dict:
        """Non-empty enrichment fields, for SSE emission / prompt context."""
        out: dict = {}
        if self.intent_type:
            out["intent_type"] = self.intent_type
        if self.objective_statement:
            out["objective_statement"] = self.objective_statement
        if self.expected_outcome:
            out["expected_outcome"] = self.expected_outcome
        if self.constraints:
            out["constraints"] = list(self.constraints)
        if self.acceptance_criteria:
            out["acceptance_criteria"] = list(self.acceptance_criteria)
        if self.verification_requirements:
            out["verification_requirements"] = list(self.verification_requirements)
        if self.risk_level:
            out["risk_level"] = self.risk_level
        if self.required_approvals:
            out["required_approvals"] = True
        if self.execution_strategy:
            out["execution_strategy"] = self.execution_strategy
        return out

    def apply_meta(self, meta: dict | None) -> "TurnPlan":
        """Merge validated meta into this plan (mutates, returns self)."""
        for k, v in _norm_plan_meta(meta).items():
            setattr(self, k, v)
        return self


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


@observe_rule(
    "turn_planner.build_turn_plan",
    forced="turn_plan",
    extract=lambda plan: {
        "kind": getattr(plan, "kind", None),
        "steps": len(getattr(plan, "steps", ()) or ()),
        # A plan of kind "none" is a deliberate no-op (greeting/smalltalk), not a
        # plan — recorded so "authored nothing" is distinguishable from "not run".
        "empty": getattr(plan, "kind", None) in (None, KIND_NONE),
    },
)
def build_turn_plan(
    user_content: str | None,
    *,
    is_dashboard_build: bool = False,
    is_report_request: bool = False,
    tool_names: list[str] | None = None,
    plan_meta: dict | None = None,
) -> TurnPlan:
    """Build the deterministic turn plan from the user's input.

    Intent precedence (mirrors the orchestrator's own routing):
    dashboard build > file/report deliverable > data question > generic.
    A greeting / empty request yields an empty plan (``kind == "none"``) —
    the loop behaves exactly as before for chitchat.
    """
    text = (user_content or "").strip()
    if not text or is_greeting(text) or _SMALLTALK_RE.match(text):
        # Phase 1.5: conversational turns are classified explicitly, not omitted.
        return TurnPlan(
            kind=KIND_NONE,
            intent_type="conversational",
            execution_strategy="direct_response",
        )

    if is_dashboard_build:
        kind = KIND_DASHBOARD
    elif is_report_request:
        kind = KIND_REPORT
    elif _looks_like_data_question(text, tool_names or []):
        kind = KIND_DATA
    else:
        kind = KIND_GENERIC
    plan = TurnPlan(kind=kind, steps=assign_task_ids(_steps(kind)))
    # P1.3: conservative default strategy from the mechanical intent
    # precedence (the planner LLM may override with a richer classification).
    plan.execution_strategy = "direct_tool"
    # Phase 1.5: the template path cannot classify semantically — say so
    # explicitly. Explicit plan_meta (validated in apply_meta) still wins.
    plan.intent_type = "unknown"
    plan.apply_meta(plan_meta)
    return plan


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
    """The plan block injected into the LLM context before the loop.

    Phase 1.5 (Gap 2): the plan's enrichment meta is part of the CONTRACT the
    loop executes against — acceptance criteria, verification requirements,
    risk and strategy are injected so the model can be held to them (and so
    reviewer findings have something concrete to check against). Compact
    key: value rows — no chain-of-thought, nothing fabricated: only fields
    the planner actually supplied appear.
    """
    if not plan or not plan.steps:
        return ""
    lines = [f"{i}. {s.title_en}" for i, s in enumerate(plan.steps, start=1)]
    block = (
        "TURN PLAN — the user's request has been decomposed into the following "
        "steps. Execute them IN ORDER. Do not skip ahead, do not repeat a step "
        "that is already done, and do not narrate the plan — execute it.\n"
        + "\n".join(lines)
        + "\nWhen all steps are complete, write the final answer. If a step "
          "cannot be completed, state that plainly and continue with the next "
          "achievable step."
    )
    meta = plan.plan_meta()
    if not meta:
        return block
    rows: list[str] = []
    if meta.get("intent_type"):
        rows.append(f"- Intent: {meta['intent_type']}")
    if meta.get("execution_strategy"):
        rows.append(f"- Execution strategy: {meta['execution_strategy']}")
    if meta.get("objective_statement"):
        rows.append(f"- Objective: {meta['objective_statement']}")
    if meta.get("expected_outcome"):
        rows.append(f"- Expected outcome: {meta['expected_outcome']}")
    for c in meta.get("constraints") or ():
        rows.append(f"- Constraint: {c}")
    criteria = meta.get("acceptance_criteria") or ()
    if criteria:
        rows.append("- Acceptance criteria (completion is judged against these):")
        rows.extend(f"  - {c}" for c in criteria)
    verifications = meta.get("verification_requirements") or ()
    if verifications:
        rows.append("- Verification requirements:")
        rows.extend(f"  - {v}" for v in verifications)
    if meta.get("risk_level"):
        rows.append(f"- Risk level: {meta['risk_level']}")
    if meta.get("required_approvals"):
        rows.append("- Required approvals: yes")
    if rows:
        block += (
            "\n\nPLAN CONTEXT — structured requirements for this turn "
            "(do not contradict them; if one cannot be met, say so plainly):\n"
            + "\n".join(rows)
        )
    return block


# ── Dynamic (per-request) plan parsing ─────────────────────────────────────

DYNAMIC_PLAN_MAX_STEPS = 8
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


def _mint_task_id(key: str, title: str) -> str:
    """Content-derived stable task identity (2026-11-02).

    The same logical task keeps the same id across replans and reordering:
    the id is a short hash of the normalized key+title, NOT the position.
    A task whose title is rewritten by a replan legitimately gets a new id
    (it is a new task to the user); preserved titles keep theirs.
    """
    import hashlib

    basis = f"{(key or '').strip().lower()}|{(title or '').strip().lower()}"
    return "task-" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:10]


def assign_task_ids(steps: list[TurnStep]) -> list[TurnStep]:
    """In-place: mint stable task ids for steps that lack one."""
    for s in steps:
        if not s.task_id:
            s.task_id = _mint_task_id(s.key, s.title_en or s.title_zh)
    return steps


def parse_dynamic_plan(raw: str | None, kind: str) -> TurnPlan | None:
    """Parse the planner LLM's output into a TurnPlan.

    Accepts ``{"steps": [{"title": ..., "description": ...,
    "depends_on": [1,2]}]}``, a bare list of objects or strings, or a plain
    numbered/bullet list ("1. title"). Returns None when the output cannot
    be parsed into 2..DYNAMIC_PLAN_MAX_STEPS valid steps — the caller then
    falls back to the fixed intent template.

    2026-11-02: every parsed step gets a stable content-derived ``task_id``
    (see ``_mint_task_id``); ``description``/``depends_on`` are parsed
    tolerantly (invalid shapes are dropped, never guessed).
    """
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    # Strip markdown code fences if the model wrapped the JSON.
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    titles: list[str] = []
    descriptions: dict[int, str] = {}
    depends: dict[int, tuple[int, ...]] = {}
    plan_meta_raw: dict | None = None
    # 1) JSON: {"steps": [...]} | {"plan": [...]} | [...]
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            steps_raw = data.get("steps") or data.get("plan") or []
            # P1.1/P1.2: enriched planner payloads carry structured intent /
            # acceptance / risk fields alongside the steps. Tolerant parse —
            # invalid values are dropped by _norm_plan_meta, never guessed.
            plan_meta_raw = {
                k: v for k, v in data.items()
                if k not in ("steps", "plan")
            } or None
        elif isinstance(data, list):
            steps_raw = data
        else:
            steps_raw = None
        if isinstance(steps_raw, list):
            for pos, item in enumerate(steps_raw):
                if isinstance(item, dict) and item.get("title"):
                    titles.append(str(item["title"]))
                    d = str(item.get("description") or "").strip()
                    if d:
                        descriptions[pos] = d[:300]
                    dep = item.get("depends_on")
                    if isinstance(dep, list):
                        vals = tuple(
                            int(x) for x in dep
                            if isinstance(x, (int, float, str)) and str(x).strip().lstrip("+-").isdigit()
                        )
                        if vals:
                            depends[pos] = vals
                elif isinstance(item, str) and item.strip():
                    titles.append(item)
    except (ValueError, TypeError):
        titles = []
        descriptions = {}
        depends = {}
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
    steps = []
    for i, t in enumerate(titles, start=1):
        key = infer_step_key(t, i)
        s = TurnStep(
            step_index=i,
            key=key,
            phase="verify" if key == "verify" else "act",
            title_en=t,
            title_zh=t,  # LLM emits in the user's language; keep as-is
            evidence=infer_evidence_from_title(t),
        )
        # positions were captured pre-dedup; best-effort match by order
        pos = i - 1
        if pos in descriptions:
            s.description = descriptions[pos]
        if pos in depends:
            s.depends_on = depends[pos]
        steps.append(s)
    assign_task_ids(steps)
    plan = TurnPlan(kind=kind, steps=steps)
    plan.apply_meta(plan_meta_raw)
    if not plan.intent_type:
        # Phase 1.5: an LLM plan that omits intent is explicitly "unknown".
        plan.intent_type = "unknown"
    if not plan.execution_strategy:
        # P1.3: conservative default — the planner LLM classifies richer
        # strategies; without one, the turn executes with the GA's own tools.
        plan.execution_strategy = "direct_tool"
    return plan


def dynamic_plan_prompt(
    user_content: str,
    kind: str,
    tool_names: list[str] | None = None,
) -> tuple[str, list[dict]]:
    """System + user messages for the one-shot planning LLM call."""
    system = (
        "You are a strict task planner for an autonomous agent. "
        "Given the user's request, output a concise step-by-step execution "
        'plan as JSON ONLY: {"steps": [{"title": "...", "description": '
        '"...", "depends_on": [step_numbers]}]}. Rules: '
        "2-8 steps sized to the ACTUAL request (a simple request may need "
        "only 2; a complex enterprise analysis 6 or more — never pad with "
        "generic filler); each title is a concrete action in the imperative "
        "mood, under 60 characters; each description is one short "
        "user-friendly sentence about what that step does for THIS request "
        "(no internal jargon, no tool names, no SQL); depends_on lists the "
        "1-based step numbers that must finish first (empty for the first "
        "step); no commentary, no markdown fences; the first step analyzes "
        "the request; include data-gathering and a final build or verify "
        "step when relevant. "
        # P1.1/P1.2: structured intent + enrichment — semantic classification
        # by the planner LLM, NOT keyword routing. All meta fields optional;
        # unknown values are dropped server-side.
        "Also include, when determinable: "
        'intent_type (one of: conversational, information_retrieval, '
        "data_analysis, research, investigation, decision_support, "
        "report_generation, operational_execution, multi_step_task, "
        "cross_department_task; use \"unknown\" only when none fits), "
        'objective_statement (what the user wants to accomplish, one '
        "sentence), expected_outcome, execution_strategy (direct_response | "
        "direct_tool | single_specialist | multi_agent | plan_dag), "
        "risk_level (low | "
        "medium | high | critical), required_approvals (bool), constraints "
        "(list), acceptance_criteria (list — how completion will be judged), "
        "verification_requirements (list). Never invent evidence or figures."
    )
    user = (
        f"User request: {user_content}\n"
        f"Request type: {kind}\n"
        f"Available tools: {', '.join(tool_names or [])}\n"
        "Respond with ONLY the JSON plan."
    )
    return system, [{"role": "user", "content": user}]


# ── SSE frames ────────────────────────────────────────────────────────────

def _scrub_step_title(title: str) -> str:
    """Content invariant for plan-step titles.

    Titles may be LLM-AUTHORED (parse_dynamic_plan) — they are raw model text,
    not server-template metadata like tool labels. A planner echoing a table
    or view name ("Summarize v_sale_order by region") must not reach the
    persisted live feed. Ordinary titles pass through verbatim (the sanitizer
    only replaces strings matching the forbidden patterns). Reuses the SAME
    sanitizer that gates all live events — no second framework.
    """
    from app.services.agent_loop.sse_builders import _sanitize_live_event_params

    return _sanitize_live_event_params({"title": title})["title"]


def plan_meta_frame(plan: TurnPlan | None) -> str | None:
    """P1.5: SSE frame carrying the plan's structured meta (intent, objective,
    strategy, acceptance criteria, risk) — same content-invariant gate as the
    step frames. None when the plan carries no enrichment (greetings, legacy
    payloads) so no empty/fake event is ever emitted."""
    meta = plan.plan_meta() if plan is not None else {}
    if not meta:
        return None
    from app.services.agent_loop.sse_builders import _sanitize_live_event_params

    safe = _sanitize_live_event_params({"m": meta})["m"]
    return 'data: ' + json.dumps({"type": "plan_meta", **safe}, ensure_ascii=False) + '\n\n'


def plan_step_added_frame(step: TurnStep) -> str:
    """SSE frame announcing a plan step (rendered as a pending checklist row).

    2026-11-02: carries ``task_id`` (stable content-derived identity) and
    ``description`` so the frontend can preserve task identity across plan
    updates and show a user-friendly line under the title."""
    return f'data: {json.dumps({"type": "plan_step_added", "step_index": step.step_index, "task_id": step.task_id or None, "description": step.description, "title": _scrub_step_title(step.title_en)}, ensure_ascii=False)}\n\n'


def plan_step_completed_frame(step: TurnStep) -> str:
    """SSE frame marking a plan step complete (checklist row ticks off)."""
    return f'data: {json.dumps({"type": "plan_step_completed", "step_index": step.step_index, "task_id": step.task_id or None, "title": _scrub_step_title(step.title_en)}, ensure_ascii=False)}\n\n'


def plan_step_updated_frame(step: TurnStep, *, status: str) -> str:
    """SSE frame for a plan REVISION of one step (2026-11-02, Phase 4).

    Emitted when the supervisor supersedes, fails, or re-opens a step
    mid-turn. ``status`` is one of: superseded | failed | pending. The
    frontend merges by ``task_id``: completed tasks stay completed, the
    affected row updates its status in place, never silently erased.
    """
    return f'data: {json.dumps({"type": "plan_updated", "step_index": step.step_index, "task_id": step.task_id or None, "title": _scrub_step_title(step.title_en), "status": status}, ensure_ascii=False)}\n\n'


def plan_replan_started_frame(reason: str = "") -> str:
    """SSE frame announcing the supervisor is revising its plan (Phase 4)."""
    safe_reason = (reason or "").strip() or "Adjusting the plan based on progress"
    return f'data: {json.dumps({"type": "replanning_started", "reason": safe_reason[:200]}, ensure_ascii=False)}\n\n'
