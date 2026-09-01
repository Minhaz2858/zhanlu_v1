"""LLM-based intent classifier for the global agent.

The classifier is the agent's *pre-LLM brain*: before the main LLM
call, a small fast LLM call classifies the user's intent and
recommends which tools to use.  This is far more accurate than the
keyword-based pattern it replaces — the LLM understands semantic
intent, not just regex matches.

Design:

* **Fast:** the classification call uses ``max_tokens=200`` and
  ``temperature=0`` so it's <200ms on a small model.
* **Total:** never raises.  On any failure the classifier returns
  ``Intent.UNCLASSIFIED`` with empty tool suggestions, and the
  chat runtime falls back to its existing behavior.
* **Cached:** results are cached per session to avoid re-classifying
  the same message on every retry.  Cache can be disabled for tests
  or strict-mode.

Returned data is a :class:`IntentResult` dataclass, not free-form
JSON, so the chat loop can use it as a typed object.

The classifier does NOT replace the existing keyword patterns in
:mod:`app.services.turn_action` — those remain as a fast fallback.
When the LLM is unavailable or slow, the keyword patterns catch
the obvious cases.  When the LLM is available, the classifier is
the primary detector.

Categories:

* ``research``        — user wants fresh / external content (news,
                        articles, prices, weather, etc.)
* ``file_generation`` — user wants a file produced (docx, pptx, xlsx)
* ``data_query``      — user wants data from the internal database
* ``chitchat``        — greeting / general conversation
* ``unclassified``    — fallback when the LLM is unsure
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


class Intent(str, Enum):
    """The user-intent categories the agent recognizes."""

    RESEARCH = "research"
    FILE_GENERATION = "file_generation"
    DATA_QUERY = "data_query"
    CHITCHAT = "chitchat"
    UNCLASSIFIED = "unclassified"


# The strict JSON schema the LLM is asked to return.  The classifier
# is permissive in parsing (handles code fences, extra prose) but
# strict in the prompt so the LLM is unambiguous.
_JSON_SCHEMA_DESCRIPTION = """\
Return ONLY a JSON object with EXACTLY these keys:
  "intent"         : one of "research" | "file_generation" | "data_query" | "chitchat"
  "confidence"     : number between 0 and 1
  "suggested_tools": array of tool names the user likely needs
                     (e.g. ["web_search"], ["create_artifact", "ask_data_agent"])
  "suggested_query": a clean, search-optimized version of the user's
                     request (e.g. "brent oil price today"), empty
                     string if not applicable
  "reasoning"      : one short sentence explaining the classification

Do NOT include any text before or after the JSON."""


_SYSTEM_PROMPT = (
    "You are an intent-classifier for an AI agent. Given the user's "
    "message, decide what they want and which tools are needed. "
    + _JSON_SCHEMA_DESCRIPTION
)


def _build_user_message(user_message: str) -> str:
    return (
        "User message:\n"
        "<<<" + user_message + ">>>\n\n"
        "Classify this message. Return only the JSON object."
    )


@dataclass
class IntentResult:
    """Structured output of the classifier."""

    intent: Intent = Intent.UNCLASSIFIED
    confidence: float = 0.0
    suggested_tools: list[str] = field(default_factory=list)
    suggested_query: str = ""
    reasoning: str = ""
    # Optional resource-route passthrough (database / document / memory /
    # report / multi_resource) — populated only when the LLM returns the
    # key; consumed as an assist hint by knowledge_graph.resource_router.
    resource_route: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["intent"] = self.intent.value
        return d


def _strip_code_fences(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```\s*$", "", text)
    return text.strip()


def _extract_json(text: str) -> Optional[dict]:
    cleaned = _strip_code_fences(text)
    if not cleaned:
        return None
    try:
        obj = json.loads(cleaned)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < 0 or end <= start:
        return None
    try:
        obj = json.loads(cleaned[start : end + 1])
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _coerce_intent(value: Any) -> Intent:
    if not isinstance(value, str):
        return Intent.UNCLASSIFIED
    s = value.strip().lower()
    for intent in Intent:
        if intent.value == s:
            return intent
    # Common aliases
    aliases = {
        "search": Intent.RESEARCH,
        "lookup": Intent.RESEARCH,
        "browse": Intent.RESEARCH,
        "fetch": Intent.RESEARCH,
        "file": Intent.FILE_GENERATION,
        "document": Intent.FILE_GENERATION,
        "report": Intent.FILE_GENERATION,
        "database": Intent.DATA_QUERY,
        "sql": Intent.DATA_QUERY,
        "chat": Intent.CHITCHAT,
        "greeting": Intent.CHITCHAT,
    }
    return aliases.get(s, Intent.UNCLASSIFIED)


def _coerce_confidence(value: Any) -> float:
    try:
        v = float(value)
    except Exception:
        return 0.0
    return max(0.0, min(1.0, v))


def _coerce_tools(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for v in value:
        if isinstance(v, str):
            s = v.strip()
            if s:
                out.append(s)
    return out


class IntentClassifier:
    """LLM-driven intent classifier with optional per-session cache."""

    def __init__(
        self,
        llm_call: Optional[Callable[[list[dict]], Awaitable[str]]] = None,
        *,
        enable_cache: bool = True,
        max_tokens: int = 200,
        temperature: float = 0.0,
    ) -> None:
        self._llm_call = llm_call
        self._enable_cache = enable_cache
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._lock = threading.Lock()
        self._cache: dict[str, IntentResult] = {}

    def _cache_key(self, message: str, session_id: Optional[str]) -> str:
        h = hashlib.sha256(((session_id or "") + "\n" + (message or "")).encode("utf-8")).hexdigest()[:32]
        return h

    async def classify(
        self,
        user_message: str,
        *,
        session_id: Optional[str] = None,
    ) -> IntentResult:
        """Classify ``user_message``. Never raises."""
        msg = (user_message or "").strip()
        if not msg:
            return IntentResult()

        # Cache hit.
        if self._enable_cache:
            key = self._cache_key(msg, session_id)
            with self._lock:
                cached = self._cache.get(key)
            if cached is not None:
                return cached

        if self._llm_call is None:
            # No LLM wired — return unclassified and let the chat
            # runtime fall back to the keyword pattern.
            return IntentResult()

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_message(msg)},
        ]
        try:
            raw = await self._llm_call(
                messages,
                max_tokens=self._max_tokens,
                temperature=self._temperature,
            )
        except Exception as exc:
            logger.debug("intent_classifier: LLM call failed (%s)", exc)
            return IntentResult()

        parsed = _extract_json(raw)
        if not parsed:
            logger.debug("intent_classifier: LLM returned non-JSON (%r)", raw[:100])
            return IntentResult()

        result = IntentResult(
            intent=_coerce_intent(parsed.get("intent")),
            confidence=_coerce_confidence(parsed.get("confidence")),
            suggested_tools=_coerce_tools(parsed.get("suggested_tools")),
            suggested_query=str(parsed.get("suggested_query") or "").strip(),
            reasoning=str(parsed.get("reasoning") or "").strip(),
            resource_route=str(parsed.get("resource_route") or "").strip().lower(),
        )

        if self._enable_cache:
            with self._lock:
                self._cache[self._cache_key(msg, session_id)] = result

        return result


async def classify_intent(
    user_message: str,
    *,
    llm_call: Optional[Callable] = None,
    session_id: Optional[str] = None,
    enable_cache: bool = True,
) -> IntentResult:
    """Module-level convenience wrapper around :class:`IntentClassifier`."""
    classifier = IntentClassifier(llm_call=llm_call, enable_cache=enable_cache)
    return await classifier.classify(user_message, session_id=session_id)


__all__ = [
    "Intent",
    "IntentResult",
    "IntentClassifier",
    "classify_intent",
]
