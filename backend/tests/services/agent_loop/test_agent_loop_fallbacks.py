"""Unit tests for the extracted canned fallback text (P2-12).

Covers ``app.services.agent_loop.fallbacks`` — the exact wording is load-
bearing (``automation_executor`` compares run output against
``_EMPTY_CONTENT_FALLBACK``; the apology/bounce-back regexes drive the
post-loop guards), so the constants must match byte-for-byte.
"""
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.agent_loop.fallbacks import (
    _APOLOGY_PATTERN_RE,
    _BOUNCE_BACK_PATTERN_RE,
    _DASHBOARD_REDIRECT_FALLBACK,
    _EMPTY_CONTENT_FALLBACK,
    _GENERIC_EMPTY_CONTENT_FALLBACK,
    _artifact_aware_fallback,
    _data_summary_fallback,
    _is_degenerate_dataset,
)

# Router copies must stay in lockstep with the extracted module.
from app.routers.agents import (
    _APOLOGY_PATTERN_RE as _ROUTER_APOLOGY_RE,
    _BOUNCE_BACK_PATTERN_RE as _ROUTER_BOUNCE_RE,
    _DASHBOARD_REDIRECT_FALLBACK as _ROUTER_DASHBOARD,
    _EMPTY_CONTENT_FALLBACK as _ROUTER_EMPTY,
    _GENERIC_EMPTY_CONTENT_FALLBACK as _ROUTER_GENERIC,
)


def test_empty_fallback_wording_locked():
    # automation_executor compares run output against this exact string.
    assert _EMPTY_CONTENT_FALLBACK == (
        "I've completed the requested changes. "
        "Please review the agent configuration above."
    )
    assert _GENERIC_EMPTY_CONTENT_FALLBACK != _EMPTY_CONTENT_FALLBACK


def test_fallbacks_match_router_re_exports():
    assert _ROUTER_EMPTY is _EMPTY_CONTENT_FALLBACK
    assert _ROUTER_GENERIC is _GENERIC_EMPTY_CONTENT_FALLBACK
    assert _ROUTER_DASHBOARD is _DASHBOARD_REDIRECT_FALLBACK
    assert _ROUTER_APOLOGY_RE.pattern == _APOLOGY_PATTERN_RE.pattern
    assert _ROUTER_BOUNCE_RE.pattern == _BOUNCE_BACK_PATTERN_RE.pattern


def test_apology_pattern_matches_english_and_chinese():
    matches = [
        "I gathered some information but had trouble putting it all together.",
        "I was unable to synthesize the results into a single answer.",
        "无法将结果整合在一起",
        "我收集的数据遇到问题",
    ]
    for text in matches:
        assert _APOLOGY_PATTERN_RE.search(text), text
    assert not _APOLOGY_PATTERN_RE.search("Here is the complete sales report for July.")


def test_bounce_back_pattern_matches_invitations():
    matches = [
        "I retrieved 42 rows from the database. You can ask me for a summary.",
        "retrieved 8 records from erp_t_sal_outstock",
        "Let me know if you want me to break this down further.",
        "需要我生成一份分析报告",
    ]
    for text in matches:
        assert _BOUNCE_BACK_PATTERN_RE.search(text), text
    assert not _BOUNCE_BACK_PATTERN_RE.search("July sales increased 12% month-over-month.")


def test_dashboard_redirect_keeps_action_hint():
    assert "create dashboard" in _DASHBOARD_REDIRECT_FALLBACK


def test_artifact_aware_fallback_mentions_titles():
    fallback = _artifact_aware_fallback(["Weekly Sales Deck", "Q3 Forecast"])
    assert "Weekly Sales Deck" in fallback
    assert "Q3 Forecast" in fallback
    assert fallback != _GENERIC_EMPTY_CONTENT_FALLBACK


def test_data_summary_fallback_names_titles():
    fallback = _data_summary_fallback(["July Sales Report"])
    assert "July Sales Report" in fallback
    multi = _data_summary_fallback(["Report A", "Report B"])
    assert "**Report A**" in multi
    assert "**Report B**" in multi
    # Empty titles must not raise and still produce a usable message.
    assert isinstance(_data_summary_fallback([]), str)


def test_is_degenerate_dataset_detects_all_zero():
    rows = [{"qty": 0, "total": 0}, {"qty": 0, "total": 0}]
    assert _is_degenerate_dataset(rows) is True
    rows2 = [{"qty": 0, "total": 0}, {"qty": 5, "total": 100}]
    assert _is_degenerate_dataset(rows2) is False
    # Empty / missing input is treated as degenerate by the caller guard.
    assert _is_degenerate_dataset(None) is True
    assert _is_degenerate_dataset([]) is True
