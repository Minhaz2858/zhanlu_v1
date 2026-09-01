"""Intent pipeline — ported from the legacy reference implementation.

Maps user queries to the classic decision pipeline stages:
    1. perception — observe market/entity state
    2. resolve_product — disambiguate product/entity identity
    3. diagnosis — root-cause / what's happening
    4. forecast — predict future values
    5. pricing — recommend prices / actions
    6. decision — final decision recommendation

Each stage has a prompt template that guides the LLM to classify the
user's intent into one (or more) stages, which then feeds into the
existing DAG planner (``capability_router.py``) to determine which
agent tools to dispatch.

Configuration:
- ``INTENT_PLANNER_ENABLED`` (default False) — master toggle
- When disabled, falls back to existing ``should_trigger_planning()``.

Reference:
- ``Intent-Driven Query Planner.md``
- ``EDIA_5.1.2/backend/src/engine/decision_graph.py``
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ── Stage definitions ──────────────────────────────────────────────────

STAGES: dict[str, dict] = {
    "perception": {
        "order": 1,
        "name": "Perception",
        "description": "Observe market conditions, entity state, or external events",
        "prompt_hint": "The user is asking about current conditions, data, or status.",
        "matching_keywords": ["what is", "current", "status", "observe", "data", "info", "查询", "当前"],
    },
    "resolve_product": {
        "order": 2,
        "name": "Resolve Product",
        "description": "Disambiguate which product, entity, or market the user refers to",
        "prompt_hint": "The user mentions a product, entity, or market that needs identification.",
        "matching_keywords": ["which", "product", "compare", "entity", "sku", "产品", "型号"],
    },
    "diagnosis": {
        "order": 3,
        "name": "Diagnosis",
        "description": "Root cause analysis — why is something happening",
        "prompt_hint": "The user is asking why something happened or is happening.",
        "matching_keywords": ["why", "cause", "reason", "explain", "diagnose", "analysis", "为什么会", "原因"],
    },
    "forecast": {
        "order": 4,
        "name": "Forecast",
        "description": "Predict future values, trends, or outcomes",
        "prompt_hint": "The user is asking about predictions, forecasts, or future values.",
        "matching_keywords": ["predict", "forecast", "future", "will be", "trend", "outlook", "预测", "预估", "将会"],
    },
    "what_if": {
        "order": 5,
        "name": "What-If",
        "description": "Scenario simulation — hypothetical changes to upstream prices (market index / root feedstock)",
        "prompt_hint": "The user wants to simulate a hypothetical scenario or upstream price shock.",
        "matching_keywords": ["what if", "scenario", "simulate", "simulation", "shock", "hypothetical", "假设", "情景", "模拟", "如果"],
    },
    "pricing": {
        "order": 6,
        "name": "Pricing",
        "description": "Recommend prices, margins, or pricing strategy",
        "prompt_hint": "The user is asking about pricing, margins, or pricing strategy.",
        "matching_keywords": ["price", "pricing", "margin", "discount", "cost", "价格", "定价", "折扣"],
    },
    "decision": {
        "order": 7,
        "name": "Decision",
        "description": "Final decision recommendation or action plan",
        "prompt_hint": "The user wants a recommendation, decision, or action plan.",
        "matching_keywords": ["recommend", "decision", "should I", "action", "plan", "建议", "决策", "怎么办"],
    },
}


@dataclass
class IntentResult:
    """Result of intent classification."""
    stages: list[str] = field(default_factory=list)
    primary_stage: str = "perception"  # primary intent
    confidence: float = 0.5
    rationale: str = ""
    needs_disambiguation: bool = False
    disambiguation_question: str = ""

    def to_dict(self) -> dict:
        return {
            "stages": self.stages,
            "primary_stage": self.primary_stage,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "needs_disambiguation": self.needs_disambiguation,
            "disambiguation_question": self.disambiguation_question,
        }


def is_enabled() -> bool:
    from app.config import settings
    return getattr(settings, "INTENT_PLANNER_ENABLED", False)


# ── Fast heuristic classifier (no LLM call) ───────────────────────────


def _keyword_classify(user_message: str) -> Optional[list[str]]:
    """Fast keyword-based intent classification. Returns stages or None."""
    if not user_message:
        return None

    text_lower = user_message.lower()
    matched: list[tuple[int, str]] = []

    for stage_name, stage_def in STAGES.items():
        keywords = stage_def.get("matching_keywords", [])
        score = sum(1 for kw in keywords if kw.lower() in text_lower)
        if score > 0:
            matched.append((score, stage_name))

    if not matched:
        return None

    # Sort by score descending
    matched.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in matched]


def _llm_classify(user_message: str, conversation_context: str = "") -> IntentResult:
    """Use LLM to classify user intent into pipeline stages.

    Returns an IntentResult with categorized stages and disambiguation info.
    """
    from app.services.llm_service import call_llm

    # Build classification prompt
    stage_list = "\n".join(
        f"{i+1}. **{sd['name']}** (stage: {name}) — {sd['description']}"
        for i, (name, sd) in enumerate(STAGES.items())
    )

    prompt = f"""Classify the user's intent into one or more of these stages:

{stage_list}

Conversation context: {conversation_context or "(none)"}
User message: "{user_message}"

Return a JSON object with:
- "primary_stage": the main intent stage (one of {list(STAGES.keys())})
- "additional_stages": additional stages if applicable (array)
- "confidence": float between 0.0 and 1.0
- "rationale": one sentence explaining the classification
- "needs_disambiguation": boolean (true if the query is ambiguous)
- "disambiguation_question": follow-up question if needs_disambiguation is true

Output only the JSON object, no markdown."""

    try:
        result = call_llm(
            prompt=prompt,
            temperature=0.0,
            task_type="reasoning",
        )
        raw = result.get("response", "{}")
        data = json.loads(raw) if isinstance(raw, str) else raw

        primary = data.get("primary_stage", "perception")
        if primary not in STAGES:
            primary = "perception"

        return IntentResult(
            stages=[primary] + data.get("additional_stages", []),
            primary_stage=primary,
            confidence=min(1.0, max(0.0, float(data.get("confidence", 0.5)))),
            rationale=data.get("rationale", ""),
            needs_disambiguation=data.get("needs_disambiguation", False),
            disambiguation_question=data.get("disambiguation_question", ""),
        )
    except Exception as e:
        logger.warning("Intent LLM classification failed: %s — using heuristics", e)
        # Fall back to keyword-based
        stages = _keyword_classify(user_message) or ["perception"]
        return IntentResult(
            stages=stages,
            primary_stage=stages[0],
            confidence=0.3,
        )


def classify_intent(
    user_message: str,
    conversation_context: str = "",
    use_llm: bool = False,
) -> IntentResult:
    """Classify user intent into pipeline stages.

    Args:
        user_message: The user's query text.
        conversation_context: Optional conversation history (compacted).
        use_llm: If True, uses LLM for classification (costs 1 API call).
                 If False, uses fast keyword heuristic.

    Returns:
        IntentResult with stage classification.
    """
    if not is_enabled():
        # Disabled → return "perception" as a neutral default
        return IntentResult(primary_stage="perception", stages=["perception"])

    if use_llm:
        return _llm_classify(user_message, conversation_context)

    stages = _keyword_classify(user_message)
    if stages:
        return IntentResult(
            stages=stages,
            primary_stage=stages[0],
            confidence=min(0.7, 0.3 + 0.1 * len(stages)),
        )
    return IntentResult(primary_stage="perception", stages=["perception"], confidence=0.2)


def stage_to_tools(stage: str) -> list[str]:
    """Map a pipeline stage to recommended agent tools.

    Returns a list of tool names the DAG planner should include for this stage.
    """
    mapping = {
        "perception": ["ask_perception", "ask_intelligence", "web_search"],
        "resolve_product": ["resolve_product", "product_lookup", "knowledge_graph"],
        "diagnosis": ["ask_diagnosis", "ask_rag", "analyze_data"],
        "forecast": ["ask_forecast", "forecast_tool", "analyze_data"],
        "what_if": ["forecast_what_if", "ask_forecast", "forecast_tool"],
        "pricing": ["ask_pricing", "ask_decision", "pricing_calculator"],
        "decision": ["ask_decision", "ask_report", "generate_report"],
    }
    return mapping.get(stage, mapping["perception"])


def integrate_with_planning_trigger(user_message: str) -> dict:
    """Bridge function: runs intent classification and merges with
    the existing planning trigger (should_trigger_planning).

    Returns a dict with:
        - should_plan: bool (same as original trigger)
        - intent: IntentResult.to_dict()
        - recommended_tools: list[str] from stage_to_tools()
    """
    intent = classify_intent(user_message)

    # Existing trigger
    from app.services.planning_trigger import should_trigger_planning
    plan_trigger = should_trigger_planning(user_message)

    return {
        "should_plan": plan_trigger.should_plan if hasattr(plan_trigger, "should_plan") else False,
        "intent": intent.to_dict(),
        "recommended_tools": stage_to_tools(intent.primary_stage),
    }


__all__ = [
    "STAGES",
    "IntentResult",
    "classify_intent",
    "stage_to_tools",
    "integrate_with_planning_trigger",
    "is_enabled",
]
