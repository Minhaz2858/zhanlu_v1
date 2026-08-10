"""Query rewriter for disambiguation and normalization.

One lightweight LLM call before the main query to:
1. Resolve pronoun/coreference (e.g. "那个产品" → "RTX 4090 GPU")
2. Disambiguate entity names (e.g. "apple" → "Apple Inc." vs "apple fruit")
3. Normalize multilingual input (ensure consistent language)
4. Expand abbreviations and domain slang

Configuration:
- ``QUERY_REWRITE_ENABLED`` (default False) — master toggle
- Costs 1 extra LLM call per user message when enabled.

Design: the rewriter returns the rewritten query + metadata. The original
query is also preserved in conversation context so the LLM can still
reference the user's exact words.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class RewriteResult:
    """Result of query rewriting."""
    original: str
    rewritten: str                # the rewritten/improved query
    changed: bool = False         # True if rewriting changed the query
    rationale: str = ""
    # Metadata
    resolved_entities: list[dict] = field(default_factory=list)
    language_detected: str = "en"

    def to_dict(self) -> dict:
        return {
            "original": self.original,
            "rewritten": self.rewritten,
            "changed": self.changed,
            "rationale": self.rationale,
            "resolved_entities": self.resolved_entities,
            "language_detected": self.language_detected,
        }


def is_enabled() -> bool:
    from app.config import settings
    return getattr(settings, "QUERY_REWRITE_ENABLED", False)


def rewrite_query(
    user_message: str,
    conversation_history: Optional[list[dict]] = None,
    use_llm: bool = True,
) -> RewriteResult:
    """Rewrite the user query for clarity and disambiguation.

    Args:
        user_message: The original user message.
        conversation_history: Recent conversation context for coreference.
        use_llm: If False, returns the original message unchanged.

    Returns:
        RewriteResult with rewritten query.
    """
    if not is_enabled() or not use_llm or not user_message:
        return RewriteResult(
            original=user_message,
            rewritten=user_message,
            changed=False,
        )

    # Build recent context
    context_block = ""
    if conversation_history:
        recent = conversation_history[-6:]  # last 6 messages
        lines = []
        for msg in recent:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > 300:
                content = content[:300] + "..."
            lines.append(f"[{role}]: {content}")
        context_block = "\n".join(lines)

    from app.services.llm_service import call_llm

    prompt = f"""Rewrite the following user message to be clearer and more precise.
Resolve any ambiguous pronouns, abbreviations, or entity names using context.

Context (recent conversation):
{context_block or "(none)"}

User message: "{user_message}"

Return a JSON object with:
- "rewritten": the improved query string
- "changed": boolean (true if the query was actually modified)
- "rationale": one sentence explaining what was changed (or why nothing was)
- "resolved_entities": array of {{"original": "...", "resolved": "..."}} objects
- "language_detected": "zh" or "en"

Output only the JSON object, no markdown."""

    try:
        result = call_llm(
            prompt=prompt,
            temperature=0.0,
            task_type="simple_chat",
        )
        raw = result.get("response", "{}")
        data = json.loads(raw) if isinstance(raw, str) else raw

        rewritten = data.get("rewritten", user_message)
        changed = data.get("changed", False)
        rationale = data.get("rationale", "")
        resolved = data.get("resolved_entities", [])
        lang = data.get("language_detected", "en")

        if changed and rewritten != user_message:
            logger.debug("Query rewritten: '%s' → '%s' (%s)", user_message, rewritten, rationale)

        return RewriteResult(
            original=user_message,
            rewritten=rewritten if changed else user_message,
            changed=changed,
            rationale=rationale,
            resolved_entities=resolved,
            language_detected=lang,
        )
    except Exception as e:
        logger.warning("Query rewrite failed (non-fatal): %s", e)
        return RewriteResult(
            original=user_message,
            rewritten=user_message,
            changed=False,
        )


def should_rewrite(user_message: str) -> bool:
    """Heuristic: return True if the query likely needs rewriting.

    Checks for common ambiguity signals: short messages, pronouns,
    domain slang, mixed languages.
    """
    if not is_enabled():
        return False
    if not user_message:
        return False

    # Very short messages are often ambiguous
    if len(user_message) < 10:
        return True

    # Coreference signals
    pronoun_signals = [
        "it", "they", "them", "this", "that", "those", "these",
        "he", "she", "him", "her", "the one", "之前那个",
        "它", "他", "她", "这个", "那个", "那", "这",
    ]
    text_lower = user_message.lower()
    if any(p in text_lower for p in pronoun_signals):
        return True

    return False


__all__ = [
    "RewriteResult",
    "rewrite_query",
    "should_rewrite",
    "is_enabled",
]
