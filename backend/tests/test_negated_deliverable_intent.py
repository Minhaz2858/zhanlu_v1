"""Explicit refusal of a file deliverable must not be read as a request for one.

Live defect (2026-09-18): the user asked
``本周销售情况 ，只统计 示例工厂数据 ，直接输出，不要输出 文档`` — the phrasing
FORBIDS a document. ``normalize_deliverable_intent`` returned ``'docx'``, so
``should_force_create_file`` injected the create_artifact nudge near the iteration
cap; the turn ended with a docx the user had refused, and the model explained the
conflict instead of answering. These tests pin the refusal AND the requests that
must still be honoured.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.services.goal_contract import normalize_deliverable_intent
from app.services.file_turn_guard import is_file_deliverable_request, should_force_create_file
from app.services.synexia.intent_router import detect_file_intent, is_deliverable_negated

# The exact prompt from the failing live turn.
LIVE_QUESTION = "本周销售情况 ，只统计 示例工厂数据 ，直接输出，不要输出 文档"


@pytest.mark.parametrize(
    "text",
    [
        LIVE_QUESTION,
        "不要输出文档",
        "不要输出 文档",
        "不用生成文档，直接说",
        "不需要导出文件",
        "本周销售数据不要文档",
        "不要给我文件，直接回答",
        "不用做表格文件",
        "no document needed",
        "don't create a file, just tell me",
        "do not generate a document",
        "without a file please",
    ],
)
def test_refusals_are_recognised(text):
    assert is_deliverable_negated(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "make me a DOCX sales report",
        "export to xlsx please",
        # A negation of something ELSE in the sentence must not suppress the ask.
        "不要包括文档中未列出的列，生成一份文档",
        "don't include columns the document omits, make a docx",
        "生成一份报告，不要包含图表",
        "how many customers do we have",
    ],
)
def test_negation_of_other_things_is_not_a_refusal(text):
    assert is_deliverable_negated(text) is False


def test_live_question_yields_no_deliverable_intent(monkeypatch):
    """The exact failing prompt: with the goal-contract path armed."""
    monkeypatch.setattr(settings, "GOAL_CONTRACT_ENABLED", True, raising=False)
    assert normalize_deliverable_intent(LIVE_QUESTION) is None
    assert detect_file_intent(LIVE_QUESTION) is None


def test_live_question_yields_no_deliverable_intent_legacy_path(monkeypatch):
    """Same prompt with the goal-contract normalizer disabled (legacy regex path)."""
    monkeypatch.setattr(settings, "GOAL_CONTRACT_ENABLED", False, raising=False)
    assert detect_file_intent(LIVE_QUESTION) is None


def test_live_question_is_not_a_file_deliverable_request(monkeypatch):
    monkeypatch.setattr(settings, "GOAL_CONTRACT_ENABLED", True, raising=False)
    assert is_file_deliverable_request(LIVE_QUESTION) == (False, None)


def test_force_guard_does_not_fire_for_a_refusal(monkeypatch):
    """The guard that forced the unwanted docx must stay silent."""
    monkeypatch.setattr(settings, "GOAL_CONTRACT_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "FILE_TURN_GUARD_ENABLED", True, raising=False)

    assert (
        should_force_create_file(
            LIVE_QUESTION,
            [],
            iteration=9,
            max_iterations=10,
        )
        is False
    )


def test_force_guard_still_fires_for_a_real_request(monkeypatch):
    """No over-correction: a genuine file request still forces the deliverable."""
    monkeypatch.setattr(settings, "GOAL_CONTRACT_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "FILE_TURN_GUARD_ENABLED", True, raising=False)

    assert (
        should_force_create_file(
            "give me a docx of this week's sales",
            [],
            iteration=9,
            max_iterations=10,
        )
        is True
    )


@pytest.mark.parametrize(
    "text,expected",
    [
        ("make me a DOCX sales report", "docx"),
        ("export to xlsx please", "xlsx"),
        ("生成一份文档", "docx"),
        ("can I have it as a PowerPoint?", "pptx"),
    ],
)
def test_genuine_requests_are_unchanged(text, expected, monkeypatch):
    monkeypatch.setattr(settings, "GOAL_CONTRACT_ENABLED", True, raising=False)
    assert detect_file_intent(text) == expected
    assert normalize_deliverable_intent(text) == expected


def test_conversion_target_still_wins_over_the_source_format(monkeypatch):
    """Division of labour: detect_file_intent resolves conversions to the TARGET.

    ``normalize_deliverable_intent`` is the format-keyword normalizer and returns
    the first mention ("docx"); ``detect_file_intent`` runs the convert-target
    step on top and returns "pdf". The negation guard must not disturb either.
    """
    monkeypatch.setattr(settings, "GOAL_CONTRACT_ENABLED", True, raising=False)
    assert detect_file_intent("convert this docx to pdf") == "pdf"
    assert normalize_deliverable_intent("convert this docx to pdf") == "docx"
