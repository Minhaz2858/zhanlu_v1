"""Always-on Skill Agent intent router.

Classifies each Skill Agent turn into one of four intents:

- ``create_skill``  — user wants to build a new skill.
- ``use_skill``     — user wants to invoke an existing skill.
- ``edit_skill``    — user wants to modify an existing skill.
- ``normal_chat``   — anything else.

The router uses a fast keyword path first (deterministic, zero-latency). When
the keyword path is ambiguous, it falls back to a single LLM classification
call. It is designed to run at the top of every Skill Agent turn and never
blocks normal chat: any failure degrades gracefully to ``normal_chat``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@dataclass
class SkillIntent:
    intent: str  # "create_skill" | "use_skill" | "edit_skill" | "normal_chat"
    skill_name: str | None = None  # matched skill name for use_skill/edit_skill
    confidence: float = 0.0

    @property
    def is_skill_related(self) -> bool:
        return self.intent != "normal_chat"


# Keyword patterns for the fast path. Each tuple is (intent, [patterns]).
# Patterns are matched case-insensitively against the raw user message.
_CREATE_PATTERNS = [
    "create a skill",
    "make a skill",
    "build a skill",
    "new skill",
    "create skill",
    "make skill",
    "build skill",
    "add a skill",
    "write a skill",
    "author a skill",
    "create me a",
    "make me a skill",
    "帮我做个技能",
    "创建一个技能",
    "做一个技能",
    "新建技能",
    "写一个技能",
]
_USE_PATTERNS = [
    "use the skill",
    "use my skill",
    "use skill",
    "run the skill",
    "run my skill",
    "invoke the skill",
    "invoke skill",
    "use my",
    "用技能",
    "用我的技能",
    "使用技能",
    "调用技能",
]
_EDIT_PATTERNS = [
    "edit the skill",
    "edit my skill",
    "edit skill",
    "update the skill",
    "update my skill",
    "update skill",
    "fix the skill",
    "change the skill",
    "add to the skill",
    "add a section",
    "add a reference",
    "add a step",
    "modify the skill",
    "修改技能",
    "更新技能",
    "编辑技能",
]


class SkillIntentRouter:
    """Classify user messages into skill intents."""

    def __init__(self, *, llm_fallback: bool = True):
        self.llm_fallback = llm_fallback

    def classify_fast(self, user_message: str) -> SkillIntent | None:
        """Deterministic keyword classification. Returns None if ambiguous."""
        if not user_message or not user_message.strip():
            return None
        msg = user_message.strip().lower()

        # Order matters: check edit before create (a phrase like "edit and add"
        # should not be mistaken for pure create), and create before use.
        for intent, patterns in (
            ("edit_skill", _EDIT_PATTERNS),
            ("create_skill", _CREATE_PATTERNS),
            ("use_skill", _USE_PATTERNS),
        ):
            for pat in patterns:
                if pat in msg:
                    return SkillIntent(intent=intent, confidence=0.9)

        return None

    async def classify(
        self,
        user_message: str,
        conversation_history: list[dict] | None = None,
        db: Session | None = None,
    ) -> SkillIntent:
        """Classify a user message, using the keyword fast path then LLM.

        ``conversation_history`` and ``db`` are accepted for interface
        compatibility and future context-aware classification, but the v1
        implementation relies on the message text only.
        """
        fast = self.classify_fast(user_message)
        if fast is not None:
            return fast

        if self.llm_fallback:
            try:
                return await self._classify_with_llm(user_message)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("LLM intent classification failed: %s", exc)

        return SkillIntent(intent="normal_chat", confidence=0.5)

    async def _classify_with_llm(self, user_message: str) -> SkillIntent:
        from app.services.llm_service import call_llm

        prompt = _build_classification_prompt(user_message)
        result = await call_llm(
            prompt=prompt,
            temperature=0.0,
            response_json_schema={
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "enum": ["create_skill", "use_skill", "edit_skill", "normal_chat"],
                    },
                    "skill_name": {"type": ["string", "null"]},
                    "confidence": {"type": "number"},
                },
                "required": ["intent"],
            },
            task_type="skill_intent",
        )
        raw = (result.get("response") or "").strip()
        data = _safe_parse_json(raw)
        intent = data.get("intent") if isinstance(data, dict) else None
        if intent not in ("create_skill", "use_skill", "edit_skill", "normal_chat"):
            return SkillIntent(intent="normal_chat", confidence=0.5)
        return SkillIntent(
            intent=intent,
            skill_name=(data.get("skill_name") or None) if isinstance(data, dict) else None,
            confidence=float(data.get("confidence", 0.6)) if isinstance(data, dict) else 0.6,
        )


def _build_classification_prompt(user_message: str) -> str:
    return (
        "You are classifying a user message sent to a personal skill-building "
        "assistant. Determine the user's intent.\n\n"
        "Intents:\n"
        "- create_skill: the user wants to create/build a NEW personal skill "
        "(a reusable capability, workflow, or document generator).\n"
        "- use_skill: the user wants to USE/run an existing skill to produce "
        "output.\n"
        "- edit_skill: the user wants to modify/update an existing skill.\n"
        "- normal_chat: none of the above (general conversation or questions).\n\n"
        f"User message: \"{user_message}\"\n\n"
        "Respond with JSON only: {\"intent\": \"...\", \"skill_name\": null or "
        "string, \"confidence\": 0.0-1.0}."
    )


def _safe_parse_json(raw: str) -> dict | None:
    import json

    try:
        return json.loads(raw)
    except Exception:
        # Strip markdown code fences if present and retry once.
        stripped = raw.strip()
        if stripped.startswith("```"):
            stripped = stripped.strip("`")
            if stripped.lower().startswith("json"):
                stripped = stripped[4:]
            stripped = stripped.strip()
        try:
            return json.loads(stripped)
        except Exception:
            return None
