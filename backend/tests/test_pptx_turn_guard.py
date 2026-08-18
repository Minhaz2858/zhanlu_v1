"""Tests for the PPTX turn-guard (deliverable enforcement).

The guard mirrors ``dashboard_turn_guard``: when a user explicitly asks
for a PPT/PPTX deck and the agent ends its turn without calling
``create_artifact(type="pptx")``, the guard either nudges the model to
build the deck (synthesis-boundary nudge, cap 1/turn) or forces the
``create_artifact`` call as the tool-loop budget runs out (T-3 window).
"""

import pytest

from app.config import settings
from app.services.pptx_turn_guard import (
    build_pptx_disclosure,
    build_pptx_nudge,
    is_pptx_request,
    pptx_artifact_created,
    pptx_turn_guard,
    should_force_create_pptx,
)


# ---------------------------------------------------------------- detection

def test_is_pptx_request_english():
    assert is_pptx_request("make a sales overview PPT")
    assert is_pptx_request("Please build a ppt for me")
    assert is_pptx_request("export to pptx")
    assert is_pptx_request("build a PowerPoint deck")
    assert is_pptx_request("a pitch deck for investors")
    assert is_pptx_request("a slide deck please")


def test_is_pptx_request_chinese():
    assert is_pptx_request("做一份销售总览PPT")
    assert is_pptx_request("帮我生成一个演示文稿")
    assert is_pptx_request("做一个幻灯片汇报")
    assert is_pptx_request("帮我做一个ppt汇报材料")


# ---------------------------------------------- agents.py wiring (regression)

def test_agents_module_wires_pptx_force_next():
    """Fix 1b: the last-allowed nudge must arm a force flag consumed at the
    top of the next loop iteration in both v3 and v2 stream loops, so prose
    deflection can never end the turn without a deck."""
    import inspect

    import app.routers.agents as agents_mod

    src = inspect.getsource(agents_mod)
    assert "_pptx_force_next_iteration" in src
    assert "_pptx_guard.force_next" in src
    # The force flag must be read at the same place the T-window force is
    # evaluated — i.e. OR'd into the should_force_create_pptx condition.
    assert "_pptx_force_next_iteration or should_force_create_pptx" in src
    # Both v3 and v2 stream loops must initialize the flag (UnboundLocalError
    # discipline — the value is read AFTER the loop's guard block).
    assert src.count("_pptx_force_next_iteration = False") >= 2


def test_agents_module_exports_turn_guard_symbols():
    """Regression: agents.py must import the pptx turn-guard helpers.

    A silently dropped import edit caused a runtime NameError
    ("should_force_create_pptx" is not defined) inside the v2/v3 stream
    loops at the T-3 forcing block. Because that block runs for every
    message (the guard call is unconditional and internally checks the
    request), every chat turn died with a broken SSE connection
    ("Sorry, the connection was interrupted") — even plain "hi".
    """
    import app.routers.agents as agents_mod

    assert callable(agents_mod.should_force_create_pptx)
    assert callable(agents_mod.pptx_turn_guard)


def test_is_pptx_request_negative():
    assert not is_pptx_request("make a sales summary")
    assert not is_pptx_request("export to xlsx")
    assert not is_pptx_request("generate a word document")
    assert not is_pptx_request("send me the markdown")
    assert not is_pptx_request(None)
    assert not is_pptx_request("")


def test_is_pptx_request_uses_shared_intent_router():
    """Single source of truth — same decision as finalize/user_signal."""
    from app.services.synexia.intent_router import detect_file_intent
    assert detect_file_intent("make a sales overview PPT") == "pptx"
    assert detect_file_intent("做一份销售总览PPT") == "pptx"


# ---------------------------------------------------------- created-detection

def test_pptx_artifact_created_via_create_artifact():
    calls = [
        {"name": "create_artifact",
         "arguments_string": '{"type": "pptx", "title": "Sales Overview"}'},
    ]
    assert pptx_artifact_created(calls)


def test_pptx_artifact_created_docx_does_not_count():
    calls = [
        {"name": "create_artifact",
         "arguments_string": '{"type": "docx", "title": "Sales"}'},
    ]
    assert not pptx_artifact_created(calls)


def test_pptx_artifact_created_via_skill_path():
    assert pptx_artifact_created(
        [{"name": "run_sandbox_skill", "arguments_string": "html2pptx build"}]
    )
    assert pptx_artifact_created(
        [{"name": "Skill", "arguments_string": '{"name": "html2pptx"}'}]
    )


def test_pptx_artifact_created_result_text_counts():
    calls = [
        {"name": "create_artifact", "arguments_string": "{}",
         "results": {"artifact_type": "pptx", "url": "/files/deck.pptx"}},
    ]
    assert pptx_artifact_created(calls)


def test_pptx_artifact_created_other_tools_ignored():
    """Query results that merely mention 'pptx' must NOT count as a build."""
    calls = [
        {"name": "execute_query", "arguments_string": "select 1",
         "results": "the report deck is a pptx file"},
    ]
    assert not pptx_artifact_created(calls)


def test_pptx_artifact_created_empty():
    assert not pptx_artifact_created([])
    assert not pptx_artifact_created(None)


# ------------------------------------------------------------ budget forcing

def test_should_force_in_t_window(monkeypatch):
    monkeypatch.setattr(settings, "PPT_TURN_GUARD_ENABLED", True)
    assert should_force_create_pptx(
        "make a sales PPT", [], iteration=36, max_iterations=40,
    )
    assert should_force_create_pptx(
        "make a sales PPT", [], iteration=39, max_iterations=40,
    )


def test_should_not_force_outside_window(monkeypatch):
    monkeypatch.setattr(settings, "PPT_TURN_GUARD_ENABLED", True)
    assert not should_force_create_pptx(
        "make a sales PPT", [], iteration=20, max_iterations=40,
    )


def test_should_not_force_when_already_created(monkeypatch):
    monkeypatch.setattr(settings, "PPT_TURN_GUARD_ENABLED", True)
    calls = [{"name": "create_artifact", "arguments_string": '{"type": "pptx"}'}]
    assert not should_force_create_pptx(
        "make a sales PPT", calls, iteration=38, max_iterations=40,
    )


def test_should_not_force_when_dashboard_forced(monkeypatch):
    monkeypatch.setattr(settings, "PPT_TURN_GUARD_ENABLED", True)
    assert not should_force_create_pptx(
        "make a sales PPT", [], iteration=38, max_iterations=40,
        dashboard_forced=True,
    )


def test_should_not_force_without_artifact_tool(monkeypatch):
    monkeypatch.setattr(settings, "PPT_TURN_GUARD_ENABLED", True)
    assert not should_force_create_pptx(
        "make a sales PPT", [], iteration=38, max_iterations=40,
        has_artifact_tool=False,
    )


def test_should_force_flag_off_inert(monkeypatch):
    monkeypatch.setattr(settings, "PPT_TURN_GUARD_ENABLED", False)
    assert not should_force_create_pptx(
        "make a sales PPT", [], iteration=39, max_iterations=40,
    )


def test_should_not_force_not_pptx_request(monkeypatch):
    monkeypatch.setattr(settings, "PPT_TURN_GUARD_ENABLED", True)
    assert not should_force_create_pptx(
        "make a sales summary", [], iteration=39, max_iterations=40,
    )


# ----------------------------------------------------------- nudge/disclosure

def test_nudge_mentions_create_artifact():
    msg = build_pptx_nudge()
    assert "create_artifact" in msg
    assert "pptx" in msg.lower()


def test_disclosure_non_empty():
    assert build_pptx_disclosure().strip()


# ------------------------------------------------- synthesis-boundary result

def test_turn_guard_flag_off_none(monkeypatch):
    monkeypatch.setattr(settings, "PPT_TURN_GUARD_ENABLED", False)
    res = pptx_turn_guard("make a PPT", [], budget_remaining=10, attempts=0)
    assert res.action == "none"


def test_turn_guard_nudge_within_budget(monkeypatch):
    monkeypatch.setattr(settings, "PPT_TURN_GUARD_ENABLED", True)
    res = pptx_turn_guard("make a PPT", [], budget_remaining=5, attempts=0)
    assert res.action == "nudge"
    assert "create_artifact" in res.message


def test_turn_guard_disclose_when_budget_short(monkeypatch):
    monkeypatch.setattr(settings, "PPT_TURN_GUARD_ENABLED", True)
    res = pptx_turn_guard("make a PPT", [], budget_remaining=1, attempts=0)
    assert res.action == "disclose"
    assert res.message.strip()


def test_turn_guard_attempts_cap(monkeypatch):
    monkeypatch.setattr(settings, "PPT_TURN_GUARD_ENABLED", True)
    res = pptx_turn_guard("make a PPT", [], budget_remaining=10, attempts=2)
    assert res.action == "none"


def test_turn_guard_nudge_force_next_on_last_attempt(monkeypatch):
    """The final allowed nudge must flag force_next so the loop can force
    create_artifact on the next iteration instead of accepting prose."""
    monkeypatch.setattr(settings, "PPT_TURN_GUARD_ENABLED", True)
    res = pptx_turn_guard("make a PPT", [], budget_remaining=10, attempts=1)
    assert res.action == "nudge"
    assert res.force_next is True


def test_turn_guard_nudge_no_force_next_before_last(monkeypatch):
    monkeypatch.setattr(settings, "PPT_TURN_GUARD_ENABLED", True)
    res = pptx_turn_guard("make a PPT", [], budget_remaining=10, attempts=0)
    assert res.action == "nudge"
    assert res.force_next is False


def test_turn_guard_cap_configurable(monkeypatch):
    monkeypatch.setattr(settings, "PPT_TURN_GUARD_ENABLED", True)
    monkeypatch.setattr(settings, "PPTX_NUDGE_MAX", 3)
    res = pptx_turn_guard("make a PPT", [], budget_remaining=10, attempts=2)
    assert res.action == "nudge"
    assert res.force_next is True
    res = pptx_turn_guard("make a PPT", [], budget_remaining=10, attempts=3)
    assert res.action == "none"


def test_turn_guard_skip_when_created(monkeypatch):
    monkeypatch.setattr(settings, "PPT_TURN_GUARD_ENABLED", True)
    calls = [{"name": "create_artifact", "arguments_string": '{"type":"pptx"}'}]
    res = pptx_turn_guard("make a PPT", calls, budget_remaining=10, attempts=0)
    assert res.action == "none"


def test_turn_guard_skip_non_pptx(monkeypatch):
    monkeypatch.setattr(settings, "PPT_TURN_GUARD_ENABLED", True)
    res = pptx_turn_guard(
        "make a sales summary", [], budget_remaining=10, attempts=0,
    )
    assert res.action == "none"
