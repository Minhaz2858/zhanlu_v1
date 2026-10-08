"""Data Agent Planner — the OCI "Agent Planning" stage, made explicit.

The article: "a planning agent that explicitly decides how to solve each
request before invoking tools... classify intent, decompose a complex
question into substeps, decide when SQL is needed versus retrieval or
deterministic calculation." The data agent already HAS tools and a loop;
what it lacks is an explicit plan BEFORE the loop spends its iterations.

This module adds exactly ONE bounded planning call at the start of the
``ask_data_agent`` sub-loop:

* **intent**       — single_question | multi_part | deterministic | unclear
* **decomposition**— ordered sub-questions for a multi-part request
* **strategy**     — sql | deterministic (lookup/deterministic calculations
                     route to that, instead of a doomed SQL hunt)
* **clarification**— when uncertainty is genuinely high, the plan says so and
                     the loop never starts; the structured result flows into
                     the existing clarify machinery.

Contract (mirrors the whole nl2sql lineage):

* **Fail-soft = legacy.** Bad JSON, an exception, an empty question, or the
  flag being off all return ``None``, and the caller runs TODAY's behavior
  unchanged. The planner can only ADD decisions, never subtract the loop.
* **One call, bounded.** No tools, small output, temperature 0.1 — the
  planner must be cheaper than the iterations it saves.
* **No vocabulary.** No table names, no dialect, no business terms — the
  model sees the question and the bound-source count, nothing else.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.config import settings

logger = logging.getLogger(__name__)

#: Valid vocabulary the planner may emit — anything else is a parse failure.
_INTENTS = ("single_question", "multi_part", "deterministic", "unclear")
_STRATEGIES = ("sql", "deterministic", "lookup")
_MAX_SUB_QUESTIONS = 6

_PLANNER_SYSTEM = (
    "You are the planning step of a data-analysis agent. Classify the user's "
    "question and output STRICT JSON only (no prose, no markdown):\n"
    "{\n"
    '  "intent": "single_question" | "multi_part" | "deterministic" | "unclear",\n'
    '  "strategy": "sql" | "deterministic" | "lookup",\n'
    '  "sub_questions": ["..."],\n'
    '  "needs_clarification": true | false,\n'
    '  "clarification": {"question": "...", "options": ["..."]} | null\n'
    "}\n"
    "Rules:\n"
    "- multi_part: the question asks several DISTINCT things (e.g. a total AND "
    "a ranking) → list each as a sub_questions entry, in answer order.\n"
    "- deterministic: pure arithmetic/conversion the platform can compute "
    "without querying data → strategy 'deterministic'.\n"
    "- unclear: a key term (entity, metric, or period) has several plausible "
    "meanings AND the difference changes the answer → needs_clarification "
    "true with at most 4 options. Do NOT ask when a reasonable default "
    "exists.\n"
    "- Otherwise: single_question with strategy 'sql' and an empty list.\n"
    "- sub_questions: at most 6 entries. No explanations outside the JSON."
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class DataAgentPlan:
    """What the planner decided (or would have). Immutable-ish value object."""

    intent: str = "single_question"
    strategy: str = "sql"
    sub_questions: list[str] = field(default_factory=list)
    needs_clarification: bool = False
    clarification: dict | None = None
    raw: dict = field(default_factory=dict)

    def to_state(self) -> dict:
        """The shape the sub-loop carries in ``state`` and logs."""
        return {
            "intent": self.intent,
            "strategy": self.strategy,
            "sub_questions": list(self.sub_questions),
            "needs_clarification": bool(self.needs_clarification),
        }

    def prompt_block(self) -> str:
        """The decomposition as prompt text for the sub-loop's system prompt."""
        if not self.sub_questions:
            return ""
        lines = [f"{i + 1}. {q}" for i, q in enumerate(self.sub_questions[:_MAX_SUB_QUESTIONS])]
        return (
            "[Planner decomposition] The question asks several things. "
            "Answer them IN ORDER, one dataset each:\n" + "\n".join(lines)
        )


async def _default_llm_call(messages: list[dict], **kwargs) -> str:
    """One plain-text planning call through the reliability wrapper."""
    from app.services.sub_agent_reliability import call_llm_with_reliability

    resp = await call_llm_with_reliability(messages, [], temperature=0.1, **kwargs)
    return resp.get("content", "") or ""


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    m = _JSON_RE.search(text)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _plan_from_json(obj: dict) -> DataAgentPlan:
    intent = str(obj.get("intent") or "single_question").strip().lower()
    if intent not in _INTENTS:
        return None  # type: ignore[return-value]
    strategy = str(obj.get("strategy") or ("deterministic" if intent == "deterministic" else "sql"))
    strategy = strategy.strip().lower()
    if strategy not in _STRATEGIES:
        strategy = "sql"
    # LLM output is untrusted: a bare string would otherwise be iterated as
    # characters ("total and ranking" -> ['t','o','t','a','l']).
    _subs_raw = obj.get("sub_questions")
    if isinstance(_subs_raw, str):
        _subs_raw = [_subs_raw]
    elif not isinstance(_subs_raw, list):
        _subs_raw = []
    subs = [
        str(q).strip()
        for q in _subs_raw[:_MAX_SUB_QUESTIONS]
        if str(q or "").strip()
    ]
    if intent == "single_question":
        subs = []
    clarify = bool(obj.get("needs_clarification")) and intent == "unclear"
    cl = obj.get("clarification") if isinstance(obj.get("clarification"), dict) else None
    if clarify and not (cl or {}).get("question"):
        clarify = False
    return DataAgentPlan(
        intent=intent,
        strategy=strategy,
        sub_questions=subs,
        needs_clarification=clarify,
        clarification=cl if clarify else None,
        raw=obj,
    )


async def plan_data_question(
    question: str,
    *,
    bound_kb_ids: list[str] | None = None,
    endpoint: Any = None,
    llm_call: Callable[..., Awaitable[str]] | None = None,
) -> DataAgentPlan | None:
    """Plan ONE data question. ``None`` on ANY failure = legacy behavior."""
    if not getattr(settings, "DATA_AGENT_PLANNER_ENABLED", True):
        return None
    if not (question or "").strip():
        return None
    try:
        call = llm_call or _default_llm_call
        user = (
            f"User question: {question.strip()}\n"
            f"Bound data sources: {len(bound_kb_ids or [])}"
        )
        text = await call(
            [
                {"role": "system", "content": _PLANNER_SYSTEM},
                {"role": "user", "content": user},
            ]
        )
        obj = _extract_json(text)
        if obj is None:
            logger.debug("data_agent_planner: no parseable JSON — falling back")
            return None
        plan = _plan_from_json(obj)
        if plan is None:
            logger.debug("data_agent_planner: unknown intent in %r — falling back", obj.get("intent"))
            return None
        logger.info(
            "data_agent_planner: intent=%s strategy=%s subs=%d clarify=%s",
            plan.intent, plan.strategy, len(plan.sub_questions), plan.needs_clarification,
        )
        return plan
    except Exception as e:
        logger.debug("data_agent_planner: planning failed (fail-soft): %s", e)
        return None
