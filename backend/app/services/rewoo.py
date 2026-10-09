"""ReWOO light mode — plan upfront, execute without observation loops
(IBM agentic-reasoning, P1-4).

ReWOO (Reasoning WithOut Observation) splits an agent turn into:

  PLANNER — one LLM call that decomposes the task into tool steps
            (arguments fixed upfront, no interleaved observations)
  WORKER  — deterministic execution of each step, collecting evidence
  SOLVER  — one LLM call that synthesizes the final answer from the
            task + all evidence

vs the interleaved ReAct loop (one LLM round-trip per tool call), ReWOO
cuts LLM calls from N+1 to 2 for multi-tool research tasks. Trade-off
(per the article): degrades with many tools and limited context — so it
is gated to research-style sub-agent tasks and always fails OPEN to the
production ReAct loop on any error.

Integration: ``_run_sub_agent_inner`` (delegate_tool) — the delegated
path used by delegate_task fan-out, golden eval and ART rollouts.

Flag: ``REWOO_ENABLED`` (default False — enable per deployment).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)

# Tool-name fragments that mark a task as research/data-retrieval shaped —
# the sweet spot for ReWOO (read-only, no observation-dependent branching).
_RESEARCH_FRAGMENTS = (
    "web_search", "web_extract", "search", "query", "database",
    "describe_schema", "list_data_sources", "web", "fetch", "report",
)

_PLANNER_SYSTEM = (
    "You are a meticulous planning module. Decompose the user's task into "
    "at most {max_steps} concrete tool steps. For each step pick ONE tool "
    "from the catalog and fix its arguments NOW (you cannot see tool "
    "results while planning). Prefer fewer steps; skip steps you don't "
    "need. Reply ONLY with JSON:\n"
    '{{"steps": [{{"tool": "<tool name>", "args": {{...}}, '
    '"purpose": "<one line>"}}]}}\n'
    "If NO tool is needed to answer, reply "
    '{{"steps": []}}.'
)

_EVIDENCE_BUDGET_CHARS = 2500


def rewoo_should_run(
    agent_name: Optional[str],
    task: str,
    tool_schemas: list[dict],
) -> bool:
    """Gate: flag + non-builder agent + research-shaped catalog + task."""
    from app.config import settings

    if not getattr(settings, "REWOO_ENABLED", False):
        return False
    if agent_name in ("agent_builder", "workflow_builder", "skill_agent"):
        return False
    task = (task or "").strip()
    if len(task) < 20 or not tool_schemas:
        return False
    names = [
        str((s.get("function", {}) or {}).get("name", "")).lower()
        for s in tool_schemas
    ]
    catalog = " ".join(names)
    return any(frag in catalog for frag in _RESEARCH_FRAGMENTS)


def _catalog_text(tool_schemas: list[dict]) -> str:
    lines = []
    for s in tool_schemas:
        fn = (s or {}).get("function", {})
        params = list((fn.get("parameters", {}) or {}).get("properties", {}).keys())
        lines.append(
            f"- {fn.get('name')}({', '.join(params)}): "
            f"{str(fn.get('description', ''))[:160]}"
        )
    return "\n".join(lines)


def _parse_plan(raw: str, valid_tools: set[str], max_steps: int) -> list[dict]:
    match = _JSON_OBJ_RE.search(raw or "")
    if not match:
        return []
    import json

    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return []
    steps = data.get("steps") if isinstance(data, dict) else None
    if not isinstance(steps, list):
        return []
    out: list[dict] = []
    for step in steps[:max_steps]:
        if not isinstance(step, dict):
            continue
        tool = str(step.get("tool", "")).strip()
        if tool not in valid_tools:
            continue
        args = step.get("args")
        out.append({
            "tool": tool,
            "args": args if isinstance(args, dict) else {},
            "purpose": str(step.get("purpose", ""))[:200],
        })
    return out


def _fmt_result(result: Any) -> str:
    import json as _json

    if isinstance(result, dict):
        text = _json.dumps(result, ensure_ascii=False, default=str)
    else:
        text = str(result)
    return text[:_EVIDENCE_BUDGET_CHARS]


async def run_rewoo(
    task: str,
    tool_schemas: list[dict],
    *,
    db,
    user_id: Optional[str] = None,
    endpoint=None,
    max_steps: int = 6,
) -> dict:
    """Plan → execute → solve. Returns the sub-agent result contract.

    On ANY failure returns ``{"success": False, "error": ...}`` so the
    caller falls back to the production ReAct loop.
    """
    from app.config import settings

    max_steps = int(
        getattr(settings, "REWOO_MAX_STEPS", max_steps) or max_steps)
    valid_tools = {
        str((s.get("function", {}) or {}).get("name", "")) for s in tool_schemas
    } - {"search_tools"}

    # ── PLANNER (one call, no tools exposed) ────────────────────────────
    from app.services.sub_agent_reliability import call_llm_with_reliability

    plan_resp = await call_llm_with_reliability(
        [
            {"role": "system", "content": _PLANNER_SYSTEM.format(max_steps=max_steps)},
            {"role": "user", "content":
                f"TOOL CATALOG:\n{_catalog_text(tool_schemas)}\n\nTASK: {task}"},
        ],
        tools=[],
        temperature=0.1,
        endpoint=endpoint,
    )
    plan = _parse_plan(
        plan_resp.get("content") or "", valid_tools, max_steps)
    if not plan:
        return {"success": False, "error": "rewoo: empty/invalid plan"}

    # ── WORKER (deterministic execution, no re-planning) ────────────────
    from app.services.agent_tools import execute_tool_with_retry

    evidence: list[str] = []
    executed: list[dict] = []
    for i, step in enumerate(plan, start=1):
        try:
            result = await execute_tool_with_retry(
                step["tool"], step.get("args", {}), db, user_id)
        except Exception as exc:  # noqa: BLE001 — record, don't re-plan
            result = {"error": str(exc)}
        evidence.append(
            f"[{i}] {step['tool']} — {step.get('purpose', '')}\n"
            f"result: {_fmt_result(result)}"
        )
        executed.append({
            "tool": step["tool"], "purpose": step.get("purpose", ""),
            "ok": not (isinstance(result, dict) and result.get("error")),
        })

    # ── SOLVER (one call, no tools) ─────────────────────────────────────
    solve_resp = await call_llm_with_reliability(
        [
            {"role": "system", "content":
                "You are the synthesis module. Answer the user's task using "
                "ONLY the collected tool evidence below. Cite which step "
                "supports each claim. If the evidence is insufficient, say "
                "exactly what is missing — do not fabricate."},
            {"role": "user", "content":
                f"TASK: {task}\n\nEVIDENCE:\n" + "\n\n".join(evidence)},
        ],
        tools=[],
        temperature=0.3,
        endpoint=endpoint,
    )
    final = (solve_resp.get("content") or "").strip()
    if not final:
        return {"success": False, "error": "rewoo: empty solver response"}

    return {
        "success": True,
        "task": task,
        "response": final,
        "iterations": len(plan) + 2,  # worker steps + planner + solver
        "mode": "rewoo",
        "steps": executed,
    }


__all__ = ["rewoo_should_run", "run_rewoo"]
