"""READ / ANALYZE intent guard for file-format routing (2026-08-31).

Regression test for the E2E failure: "read this docx, summarize it"
returned ``"docx"`` from ``detect_file_intent``, the skill resolver
auto-picked the docx creation skill, and the deliverable machinery
fabricated a docx artifact (with hallucinated warehouse data) instead
of reading the uploaded file.

The guard (``is_file_read_request``) must suppress format intent for
READ requests while preserving CREATE and CONVERT routing:
  READ    "read this docx, summarize it"  -> None (no creation skill)
  CREATE  "make me a DOCX sales report"   -> "docx"
  CONVERT "convert this docx to pdf"      -> "pdf" (target, not source)
"""

import pytest

from app.services.synexia.intent_router import (
    detect_convert_target,
    detect_file_intent,
    is_file_read_request,
)
from app.services.synexia.default_skills import detect_soft_intent


# ── is_file_read_request ────────────────────────────────────────────────

@pytest.mark.parametrize(
    "msg",
    [
        "read this docx, summarize it",
        "summarize the attached pptx",
        "what's in this xlsx",
        "give me a summary of this docx",
        "please read the PDF file I uploaded",
        "can you analyze this excel file?",
        "explain the attached powerpoint",
        "extract the key points from this pdf",
        "translate this docx into English",
        "帮我读取这个PDF文件",
        "阅读这个docx并总结",
        "总结一下这个附件的内容",
        "这个excel里有什么数据",
        "look at the report I sent",
        "tell me about the pptx above",
        "open the attached xlsx",
    ],
)
def test_read_requests_are_detected(msg):
    assert is_file_read_request(msg) is True


@pytest.mark.parametrize(
    "msg",
    [
        "make me a DOCX sales report",
        "can I have it as a PowerPoint?",
        "export to xlsx please",
        "make a docx summarizing Q3 sales",
        "create a pdf of the quarterly results",
        "generate a markdown summary of the data",
        "write a pptx with the new numbers",
        "build a dashboard for me",
        "make a report",
        "send me the deck as a pptx",
        "写一份docx报告",
        "把这个数据做成excel",
    ],
)
def test_create_requests_are_not_reads(msg):
    assert is_file_read_request(msg) is False


# ── detect_file_intent ──────────────────────────────────────────────────

@pytest.mark.parametrize(
    "msg",
    [
        "read this docx, summarize it",
        "summarize the attached pptx",
        "what's in this xlsx",
        "give me a summary of this docx",
        "帮我读取这个PDF文件",
        "阅读这个docx并总结",
    ],
)
def test_detect_file_intent_read_returns_none(msg):
    assert detect_file_intent(msg) is None


@pytest.mark.parametrize(
    "msg,expected",
    [
        ("make me a DOCX sales report", "docx"),
        ("can I have it as a PowerPoint?", "pptx"),
        ("export to xlsx please", "xlsx"),
        ("make a docx summarizing Q3 sales", "docx"),
        ("build a dashboard for me", "dashboard"),
    ],
)
def test_detect_file_intent_create_unchanged(msg, expected):
    assert detect_file_intent(msg) == expected


@pytest.mark.parametrize(
    "msg,expected",
    [
        ("convert this docx to pdf", "pdf"),
        ("把docx转成pdf", "pdf"),
        ("turn this xlsx into a pptx", "pptx"),
        ("convert the report to xlsx please", "xlsx"),
    ],
)
def test_detect_file_intent_convert_returns_target(msg, expected):
    assert detect_file_intent(msg) == expected
    assert detect_convert_target(msg) == expected


@pytest.mark.parametrize(
    "msg",
    [
        "convert this docx to something weird",
        "turn this into csv",
    ],
)
def test_detect_convert_target_unknown_returns_none(msg):
    assert detect_convert_target(msg) is None


# ── detect_soft_intent guard ────────────────────────────────────────────

@pytest.mark.parametrize(
    "msg",
    [
        "summarize this report",
        "read the memo I attached",
        "分析这份报告",
    ],
)
def test_soft_intent_read_suppressed(msg):
    assert detect_soft_intent(msg) is None


@pytest.mark.parametrize(
    "msg,expected",
    [
        ("make a report", "docx"),
        ("write up a memo on Q3", "docx"),
        ("prepare a deck for the board", "pptx"),
    ],
)
def test_soft_intent_create_unchanged(msg, expected):
    assert detect_soft_intent(msg) == expected
