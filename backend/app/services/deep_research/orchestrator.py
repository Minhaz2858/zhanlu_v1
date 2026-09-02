"""Deep Research Mode (v1) — in-loop research protocol orchestrator.

Runs a depth-scaled research pipeline: plan → parallel gather rounds (with
optional critique/refine between rounds) → cited final synthesis.

The pipeline is intentionally dependency-light: ``planner``, ``tool_executor``,
``synthesizer`` and ``critic`` are injectable async callables so unit tests use
fakes and the agent loop (``agents.py``) wires real implementations backed by
``llm_service`` / ``llm_router`` and the tool registry. Tool calls are scoped by
``DeepResearchRequest.scope`` — web tools for ``web``, bound-data tools for
``internal``, both for ``both`` — so isolation follows the agent's existing
binding rules. All LLM traffic stays on the Synexia routing path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from app.services.citation_grounding import citation_instruction, parse_citations

logger = logging.getLogger(__name__)

VALID_DEPTHS = ("quick", "balanced", "deep")
VALID_SCOPES = ("web", "internal", "both")

# Depth presets drive the protocol shape: how many parallel gather rounds run
# and how many critique/refine passes happen between them.
DEPTH_PRESETS: dict[str, dict[str, int]] = {
    "quick": {"gather_rounds": 1, "critique_rounds": 0, "max_plan_steps": 4},
    "balanced": {"gather_rounds": 2, "critique_rounds": 1, "max_plan_steps": 6},
    "deep": {"gather_rounds": 3, "critique_rounds": 2, "max_plan_steps": 8},
}

# Which tools each scope may invoke. Web scope uses web search/extract;
# internal scope uses the bound-data tools (Ecisco BI / SQL / data agent).
SCOPE_TOOLS: dict[str, tuple[str, ...]] = {
    "web": ("web_search", "web_extract"),
    "internal": ("sql_query", "ask_data_agent", "comprehensive_data"),
}

_ALL_TOOLS: tuple[str, ...] = tuple(
    sorted(set(SCOPE_TOOLS["web"]) | set(SCOPE_TOOLS["internal"]))
)

EmitFn = Callable[[str, Optional[str], Optional[dict]], Awaitable[None]]


@dataclass(frozen=True)
class DeepResearchRequest:
    """Validated deep-research run parameters (from chat message metadata)."""

    question: str
    depth: str = "balanced"
    scope: str = "both"

    def __post_init__(self) -> None:
        if not self.question or not self.question.strip():
            raise ValueError("question must not be empty")
        if self.depth not in VALID_DEPTHS:
            raise ValueError(f"depth must be one of {VALID_DEPTHS}")
        if self.scope not in VALID_SCOPES:
            raise ValueError(f"scope must be one of {VALID_SCOPES}")


@dataclass(frozen=True)
class DeepResearchResult:
    """Final cited report plus extracted citation markers."""

    report: str
    citations: list[dict]


async def _noop_emit(
    event_type: str,
    label_key: Optional[str] = None,
    params: Optional[dict] = None,
) -> None:
    return None


async def run_deep_research(
    request: DeepResearchRequest,
    *,
    planner: Callable[[list[dict]], Awaitable[dict]],
    tool_executor: Callable[[str, dict], Awaitable[dict]],
    synthesizer: Callable[[list[dict]], Awaitable[str]],
    critic: Optional[Callable[[list[dict]], Awaitable[str]]] = None,
    emit: Optional[EmitFn] = None,
) -> DeepResearchResult:
    """Run the research protocol and return the cited report.

    ``emit(event_type, label_key, params)`` receives live-progress events in
    the shape the frontend ``LiveActivityStream`` already renders
    (``phase_enter.research_*`` + ``plan_step_added`` / ``plan_step_completed``).
    """
    emit = emit or _noop_emit
    preset = DEPTH_PRESETS[request.depth]
    scope_tools = set(_scope_tool_list(request.scope))

    # ── 1. Plan ────────────────────────────────────────────────────────
    await emit("phase_enter", "phase_enter.research_plan")
    plan = await planner(
        [{"role": "user", "content": f"Research question: {request.question}"}]
    )
    steps = _normalize_plan(plan, request, preset["max_plan_steps"])
    for i, step in enumerate(steps, 1):
        await emit(
            "plan_step_added",
            "plan_step_added",
            {"step_index": i, "title": step["question"]},
        )

    # ── 2. Gather rounds (critique/refine between rounds) ──────────────
    evidence: list[dict[str, Any]] = []
    for round_no in range(1, preset["gather_rounds"] + 1):
        await emit(
            "phase_enter",
            "phase_enter.research_gather",
            {"round": round_no},
        )
        for step in steps:
            for tool in step["tools"]:
                if tool not in scope_tools:
                    continue  # out-of-scope tool — never invoked
                try:
                    result = await tool_executor(tool, {"query": step["question"]})
                except Exception as e:
                    logger.warning(
                        "deep research tool %s failed (non-fatal): %s", tool, e
                    )
                    continue
                evidence.append(_evidence_from(tool, step["question"], result))
        await emit("plan_step_completed", "plan_step_completed", {"round": round_no})

        # Critique pass → gap step targeted in the next gather round.
        if (
            round_no < preset["gather_rounds"]
            and preset["critique_rounds"] > 0
            and critic is not None
        ):
            await emit(
                "phase_enter",
                "phase_enter.research_critique",
                {"round": round_no},
            )
            gap = await critic(
                [{"role": "user", "content": _critique_prompt(request.question, evidence)}]
            )
            gap_text = (gap or "").strip() or request.question
            steps = steps + [
                {
                    "id": f"gap-{round_no}",
                    "question": gap_text[:300],
                    "tools": _scope_tool_list(request.scope)[:1],
                    "rationale": "gap identified by critique pass",
                }
            ]

    # ── 3. Finalize — cited synthesis ──────────────────────────────────
    await emit("phase_enter", "phase_enter.research_finalize")
    report = await synthesizer(_finalize_messages(request.question, evidence))
    citations = [c.to_dict() for c in parse_citations(report)]
    return DeepResearchResult(report=report, citations=citations)


# ── Helpers ───────────────────────────────────────────────────────────

def _scope_tool_list(scope: str) -> list[str]:
    if scope == "both":
        return list(SCOPE_TOOLS["web"]) + list(SCOPE_TOOLS["internal"])
    return list(SCOPE_TOOLS[scope])


def _normalize_plan(
    plan: Any,
    request: DeepResearchRequest,
    max_steps: int,
) -> list[dict[str, Any]]:
    """Coerce planner output into a clean step list.

    Tolerates malformed planner JSON: any step without usable tools falls
    back to the scope's primary tool, and a completely empty plan falls back
    to a single generic step so the run still gathers something.
    """
    raw: list[Any] = []
    if isinstance(plan, dict):
        steps = plan.get("steps")
        if isinstance(steps, list):
            raw = steps

    normalized: list[dict[str, Any]] = []
    for i, s in enumerate(raw[:max_steps], 1):
        if not isinstance(s, dict):
            continue
        tools = [
            t for t in (s.get("tools") or []) if isinstance(t, str) and t in _ALL_TOOLS
        ]
        if not tools:
            tools = _scope_tool_list(request.scope)[:1]
        normalized.append(
            {
                "id": s.get("id") or f"step-{i}",
                "question": (s.get("question") or request.question).strip()[:300]
                or request.question,
                "tools": tools,
                "rationale": s.get("rationale") or "",
            }
        )

    if not normalized:
        normalized = [
            {
                "id": "step-1",
                "question": request.question,
                "tools": _scope_tool_list(request.scope)[:1],
                "rationale": "fallback",
            }
        ]
    return normalized


def _evidence_from(tool: str, question: str, result: Any) -> dict[str, Any]:
    """Normalize a tool result into a compact evidence record for synthesis."""
    if isinstance(result, dict):
        snippet = str(
            result.get("rows")
            or result.get("summary")
            or result.get("text")
            or result.get("content")
            or result
        )[:500]
        url = result.get("url") or ""
        source_name = (
            result.get("title")
            or result.get("source")
            or result.get("source_name")
            or tool
        )
    else:
        snippet = str(result)[:500]
        url = ""
        source_name = tool
    return {
        "source": f"{tool}: {question[:40]}",
        "source_name": str(source_name)[:120],
        "tool": tool,
        "snippet": snippet,
        "url": url,
    }


def _critique_prompt(question: str, evidence: list[dict[str, Any]]) -> str:
    lines = "\n".join(
        f"- [{e['source']}] {e['snippet']}" for e in evidence[-20:]
    ) or "(no evidence yet)"
    return (
        f"Research question: {question}\n\n"
        f"Evidence gathered so far:\n{lines}\n\n"
        "Critique the evidence: what is missing, stale, or unsupported? "
        "Return ONE concise gap statement that a follow-up search should target."
    )


def _finalize_messages(
    question: str, evidence: list[dict[str, Any]]
) -> list[dict[str, str]]:
    lines = "\n".join(
        f"- [{e['source']}] {e['snippet']}" for e in evidence
    ) or "(no evidence gathered)"
    system = (
        "You are a deep-research analyst. Write a long-form, cited report with "
        "sections: Executive Summary / Methodology / Findings & Evidence / "
        "Data tables / Risks & Caveats / Sources. Cite evidence inline using "
        "source markers.\n\n" + citation_instruction()
    )
    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": f"Research question: {question}\n\nEvidence:\n{lines}",
        },
    ]
