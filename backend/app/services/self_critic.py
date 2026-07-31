"""LLM-based self-critic for refusal detection + corrective planning.

The self-critic is the agent's *post-LLM brain*: after the main LLM
generates a response, the critic asks a small LLM call:

    Did the assistant refuse to do something the user explicitly
    asked for? If yes, which tool should the agent call to fix
    the response, and with what arguments?

This replaces the previous keyword-based ``WEB_BROWSE_REFUSAL_PATTERN``
+ hardcoded ``web_search`` fallback.  The LLM is the source of truth
for "is this a refusal?" and "what should we do instead?".

The critic is:

* **Semantic** — the LLM understands context, not just regex
  matches, so it catches refusals the keyword pattern misses.
* **Adaptive** — the corrective action is decided by the LLM, not
  hardcoded.  The critic can suggest ``web_search``, ``web_extract``,
  ``ask_data_agent``, or any other tool, with the right arguments.
* **Total** — never raises.  On any failure (LLM down, bad JSON,
  timeout), the critic returns ``refused=False`` and the chat
  runtime proceeds as if nothing happened.
* **Cached** — per-session caching avoids re-criticizing the same
  pair (user_msg, assistant_msg) on every retry.

The keyword patterns in :mod:`app.services.turn_action` remain as a
fast pre-filter so the happy path (LLM responds correctly) doesn't
incur any LLM cost.  The critic is the slow-but-smart fallback.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """\
You are a self-critic for an AI agent. Given the user's message and
the assistant's reply, decide:

  1. Did the assistant refuse to do something the user explicitly
     asked for, when the agent actually has the tools to do it?
  2. If yes, which tool should the agent call to fix the response,
     and with what arguments?

A "refusal" is a response where the assistant claims it CANNOT do
something (e.g. "I cannot browse the internet", "I don't have
access to live data", "I am not able to fetch real-time prices")
when in fact tools like web_search / web_extract / ask_data_agent
are available to do the job.

If the assistant answered normally, refused is false.
If the assistant asked a clarifying question, refused is false.
If the assistant gave a partial answer, refused is false (it tried).

Return ONLY a JSON object with EXACTLY these keys:
  "refused"         : boolean
  "confidence"      : number between 0 and 1
  "reasoning"       : one short sentence
  "corrective_tool" : string (tool name) or null
  "corrective_args" : object (tool arguments) or empty {}

If refused is false, set corrective_tool to null and corrective_args to {}.
"""


@dataclass
class CriticVerdict:
    """The LLM's structured verdict on a single assistant response."""

    refused: bool = False
    confidence: float = 0.0
    reasoning: str = ""
    corrective_tool: Optional[str] = None
    corrective_args: dict = field(default_factory=dict)


@dataclass
class SelfCriticDecision:
    """A decision is a verdict + bookkeeping (session_id, etc.)."""

    refused: bool = False
    confidence: float = 0.0
    reasoning: str = ""
    corrective_tool: Optional[str] = None
    corrective_args: dict = field(default_factory=dict)
    session_id: Optional[str] = None
    cached: bool = False
    raw: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
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


def _coerce_confidence(value: Any) -> float:
    try:
        v = float(value)
    except Exception:
        return 0.0
    return max(0.0, min(1.0, v))


def _coerce_args(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    return {str(k): v for k, v in value.items()}


def _coerce_tool(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    s = value.strip()
    return s or None


class SelfCritic:
    """LLM-based self-critic with optional per-session cache."""

    def __init__(
        self,
        llm_call: Optional[Callable[[list[dict]], Awaitable[str]]] = None,
        *,
        enable_cache: bool = True,
        max_tokens: int = 300,
        temperature: float = 0.0,
    ) -> None:
        self._llm_call = llm_call
        self._enable_cache = enable_cache
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._lock = threading.Lock()
        self._cache: dict[str, SelfCriticDecision] = {}

    def _cache_key(
        self,
        user_message: str,
        assistant_text: str,
        session_id: Optional[str],
    ) -> str:
        h = hashlib.sha256(
            ((session_id or "") + "\n" + (user_message or "") + "\n---\n" + (assistant_text or "")).encode("utf-8")
        ).hexdigest()[:32]
        return h

    async def critique(
        self,
        user_message: str,
        assistant_text: str,
        *,
        session_id: Optional[str] = None,
    ) -> SelfCriticDecision:
        """Critique ``assistant_text`` against ``user_message``. Never raises."""
        u = (user_message or "").strip()
        a = (assistant_text or "").strip()
        if not u or not a:
            return SelfCriticDecision(session_id=session_id)

        if self._enable_cache:
            key = self._cache_key(u, a, session_id)
            with self._lock:
                cached = self._cache.get(key)
            if cached is not None:
                cached.cached = True
                return cached

        if self._llm_call is None:
            return SelfCriticDecision(session_id=session_id)

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "User message:\n"
                    "<<<" + u + ">>>\n\n"
                    "Assistant reply:\n"
                    "<<<" + a + ">>>\n\n"
                    "Decide if the assistant refused. Return only the JSON object."
                ),
            },
        ]
        try:
            raw = await self._llm_call(
                messages,
                max_tokens=self._max_tokens,
                temperature=self._temperature,
            )
        except Exception as exc:
            logger.debug("self_critic: LLM call failed (%s)", exc)
            return SelfCriticDecision(session_id=session_id)

        parsed = _extract_json(raw)
        if not parsed:
            logger.debug("self_critic: LLM returned non-JSON (%r)", raw[:100])
            return SelfCriticDecision(session_id=session_id, raw=raw)

        refused = bool(parsed.get("refused"))
        decision = SelfCriticDecision(
            refused=refused,
            confidence=_coerce_confidence(parsed.get("confidence")) if refused else _coerce_confidence(parsed.get("confidence", 0.0)),
            reasoning=str(parsed.get("reasoning") or "").strip(),
            corrective_tool=_coerce_tool(parsed.get("corrective_tool")) if refused else None,
            corrective_args=_coerce_args(parsed.get("corrective_args")) if refused else {},
            session_id=session_id,
            raw=raw,
        )

        if self._enable_cache:
            with self._lock:
                self._cache[self._cache_key(u, a, session_id)] = decision

        return decision


async def critique_response(
    user_message: str,
    assistant_text: str,
    *,
    llm_call: Optional[Callable] = None,
    session_id: Optional[str] = None,
    enable_cache: bool = True,
) -> SelfCriticDecision:
    """Module-level convenience wrapper around :class:`SelfCritic`."""
    critic = SelfCritic(llm_call=llm_call, enable_cache=enable_cache)
    return await critic.critique(user_message, assistant_text, session_id=session_id)


__all__ = [
    "CriticVerdict",
    "SelfCriticDecision",
    "SelfCritic",
    "critique_response",
]
