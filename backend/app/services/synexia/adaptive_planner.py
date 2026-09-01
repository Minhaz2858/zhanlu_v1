"""Adaptive mid-execution re-planning (Phase 3c, spec §5.3).

After each checkpoint-eligible node (nl2sql / synthesize / sandbox), the
adaptive planner reviews accumulated observations and decides whether to
proceed, insert nodes, modify the remaining tail, or complete early.

Opt-in via ``SYNEXIA_ADAPTIVE_PLANNING_ENABLED`` (default OFF). Bounded by
``SYNEXIA_ADAPTIVE_MAX_REVISIONS`` (default 2). Fail-safe: any error or
unparseable LLM reply → ``proceed`` (the run continues on the original plan).

``call_llm_fn`` may be sync or async; coroutines are detected and awaited
via a local bridge (no circular import on capability_router, which imports
this module for the checkpoint hook).
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_VALID_ACTIONS = {"proceed", "insert_nodes", "modify_remaining", "complete_early"}


@dataclass
class AdaptiveDecision:
    action: str = "proceed"  # proceed | insert_nodes | modify_remaining | complete_early
    nodes: list[dict] = field(default_factory=list)
    reason: str = ""


def _run_coro_sync(coro):
    """Run an async coroutine from this sync context (local bridge)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def decide_adaptive_revision(
    *,
    user_message: str,
    task_spec: dict,
    observations: list[Any],
    remaining_nodes: list[dict],
    call_llm_fn: Callable,
    context_manifest: Optional[dict] = None,
) -> AdaptiveDecision:
    """Decide whether to adapt the remaining plan after a checkpoint node.

    Never raises. Returns ``AdaptiveDecision(action="proceed")`` on any error
    or when there are no remaining nodes to revise.
    """
    if not remaining_nodes:
        return AdaptiveDecision(action="proceed", reason="no remaining nodes")

    obs_summary = _summarize_observations(observations)
    remaining_summary = json.dumps(remaining_nodes, ensure_ascii=False, default=str)[:1500]

    system_prompt = (
        "You are the ADAPTIVE PLANNER for a data-driven agent. A checkpoint "
        "node just completed. Review what was learned and decide whether the "
        "REMAINING plan should change.\n\n"
        f"User request: {user_message}\n"
        f"Observations so far:\n{obs_summary}\n\n"
        f"Remaining planned steps:\n{remaining_summary}\n\n"
        "Decide ONE action:\n"
        "- proceed: the remaining plan is still right — continue as-is\n"
        "- insert_nodes: add steps BEFORE the remaining ones (e.g. a refining "
        "query) — return them in `nodes`\n"
        "- modify_remaining: replace the remaining steps with a better tail — "
        "return the full new tail in `nodes`\n"
        "- complete_early: the goal is already met — stop and finalize\n\n"
        'Respond with ONLY a JSON object: {"action":"<one>","nodes":[<step dicts>],"reason":"..."}'
        "  (nodes is [] for proceed/complete_early)."
    )

    try:
        llm_response = call_llm_fn(system_prompt, [{"role": "user", "content": "Decide."}])
        if asyncio.iscoroutine(llm_response):
            llm_response = _run_coro_sync(llm_response)
    except Exception as e:
        logger.warning("Adaptive planner LLM call failed (proceed): %s", e)
        return AdaptiveDecision(action="proceed", reason=f"llm error: {e}")

    reply = _resolve_reply(llm_response)
    parsed = _parse_decision(reply)
    if parsed is None:
        logger.warning("Adaptive planner unparseable reply (proceed): %.200s", reply)
        return AdaptiveDecision(action="proceed", reason="unparseable")

    action = parsed.get("action")
    if action not in _VALID_ACTIONS:
        return AdaptiveDecision(action="proceed", reason=f"bad action: {action}")
    nodes = parsed.get("nodes") or []
    if not isinstance(nodes, list):
        nodes = []
    return AdaptiveDecision(action=action, nodes=nodes, reason=parsed.get("reason", ""))


def _summarize_observations(observations: list[Any]) -> str:
    lines = []
    for obs in (observations or [])[-8:]:  # last 8 to bound the prompt
        otype = getattr(obs, "observation_type", "?")
        ok = getattr(obs, "success", False)
        txt = (getattr(obs, "result_text", "") or "")[:200]
        lines.append(f"- [{otype}] {'ok' if ok else 'FAILED'}: {txt}")
    return "\n".join(lines) or "(none)"


def _resolve_reply(llm_response: Any) -> str:
    """Normalize the call_llm_fn return into a string."""
    if isinstance(llm_response, dict):
        return str(llm_response.get("content", "") or "")
    return str(llm_response or "")


def _parse_decision(reply: str) -> Optional[dict]:
    text = (reply or "").strip()
    if not text:
        return None
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0]
    elif "```" in text:
        text = text.split("```")[1].split("```")[0]
    try:
        obj = json.loads(text)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None
