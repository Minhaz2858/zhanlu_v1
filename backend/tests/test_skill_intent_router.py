"""Tests for the always-on Skill Agent intent router.

Verifies that:
1. Keyword fast-path classifies create / use / edit / normal intents.
2. Empty or ambiguous messages without an LLM fallback resolve to normal_chat.
3. The async classify() signature is interface-compatible (db/history optional).
"""
import pytest

from app.services.skill_studio import SkillIntent, SkillIntentRouter


def test_fast_path_create_skill():
    router = SkillIntentRouter(llm_fallback=False)
    intent = router.classify_fast("create a skill for weekly sales reports")
    assert intent is not None
    assert intent.intent == "create_skill"
    assert intent.confidence == 0.9


def test_fast_path_create_skill_cjk():
    router = SkillIntentRouter(llm_fallback=False)
    intent = router.classify_fast("帮我做个技能，自动生成周报")
    assert intent is not None
    assert intent.intent == "create_skill"


def test_fast_path_use_skill():
    router = SkillIntentRouter(llm_fallback=False)
    intent = router.classify_fast("use my report skill to make a deck")
    assert intent is not None
    assert intent.intent == "use_skill"


def test_fast_path_edit_skill_before_create():
    router = SkillIntentRouter(llm_fallback=False)
    # "edit" must win over "create" for ambiguous phrasing
    intent = router.classify_fast("edit and add a section to my skill")
    assert intent is not None
    assert intent.intent == "edit_skill"


def test_fast_path_normal_chat_returns_none():
    router = SkillIntentRouter(llm_fallback=False)
    intent = router.classify_fast("what is the weather today")
    assert intent is None


def test_empty_message_is_normal_chat():
    router = SkillIntentRouter(llm_fallback=False)
    intent = router.classify_fast("")
    assert intent is None


@pytest.mark.asyncio
async def test_classify_async_uses_fast_path():
    router = SkillIntentRouter(llm_fallback=False)
    intent = await router.classify("build a skill that summarizes PDFs")
    assert intent.intent == "create_skill"
    assert intent.is_skill_related is True


@pytest.mark.asyncio
async def test_classify_async_ambiguous_without_llm_is_normal_chat():
    router = SkillIntentRouter(llm_fallback=False)
    # No keyword match and no LLM fallback -> normal_chat
    intent = await router.classify("tell me a joke")
    assert intent.intent == "normal_chat"
    assert intent.is_skill_related is False


def test_skill_intent_dataclass_defaults():
    intent = SkillIntent(intent="use_skill")
    assert intent.skill_name is None
    assert intent.confidence == 0.0
