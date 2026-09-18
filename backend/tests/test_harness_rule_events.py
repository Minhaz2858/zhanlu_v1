"""Phase 0 of the Agent Harness migration: rule-firing evidence.

The ~12 deterministic decision functions decide agent behaviour from the user's
text (force a deliverable, route a skill, suppress a creation). They cannot be
deleted until we can prove, per rule, how often each fires — that is what
``app/services/harness/rule_events.py`` records. These tests pin the two things
that matter: the EVIDENCE is correct, and the OBSERVATION cannot change a turn
(fail-open).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.harness_rule_event import HarnessRuleEvent
from app.services.harness import rule_events as re_mod
from app.services.harness.rule_events import (
    bind_turn,
    clear_turn,
    counters,
    flush_rule_events,
    is_fired,
    forced_from,
    observe_rule,
    record_rule_event,
    reset_counters,
    summary,
)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Fresh counters, no persistence, no daemon, no ambient turn."""
    monkeypatch.setenv("HARNESS_RULE_EVENTS_ENABLED", "1")
    monkeypatch.setenv("HARNESS_RULE_EVENTS_PERSIST", "0")
    monkeypatch.setenv("HARNESS_RULE_EVENTS_LOG", "0")
    reset_counters()
    clear_turn()
    yield
    reset_counters()
    clear_turn()


@pytest.fixture
def db(tmp_path):
    db_file = tmp_path / f"test_{uuid.uuid4().hex[:8]}.db"
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


# ── decision shapes ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "decision,expected",
    [
        (True, True),
        (False, False),
        (None, False),
        ("docx", True),
        ("", False),
        ("none", False),
        ("general", False),          # default_skills.detect_soft_intent's "no match"
        (("docx",), True),
        ((False, None), False),      # is_file_deliverable_request's shape
        ((True, "docx"), True),
        ((), False),
        ({"fired": True}, True),
        ({"fired": False}, False),
        ({"should_force": True}, True),
        (3, True),
    ],
)
def test_is_fired_shapes(decision, expected):
    assert is_fired(decision) is expected


@pytest.mark.parametrize(
    "decision,forced,expected",
    [
        (True, "create_artifact", "create_artifact"),
        ((True, "docx"), None, "docx"),
        ((False, None), None, None),
        ("pptx", None, "pptx"),
        (False, None, None),
    ],
)
def test_forced_from(decision, forced, expected):
    assert forced_from(decision, forced) == expected


# ── the decorator ──────────────────────────────────────────────────────────


def test_decorator_records_and_passes_result_through():
    @observe_rule("test.rule")
    def decide(text):
        return (True, "docx")

    assert decide("make me a docx") == (True, "docx")
    assert counters()["test.rule"] == 1
    assert counters()["test.rule::fired"] == 1
    rows = re_mod.pending_rows()
    assert rows[0]["forced_target"] == "docx"
    assert rows[0]["fired"] is True


def test_decorator_wraps_only_once():
    @observe_rule("test.once")
    @observe_rule("test.once")
    def decide():
        return True

    decide()
    assert counters()["test.once"] == 1


def test_decorator_captures_iteration_and_input_prefix():
    @observe_rule("test.force", forced="create_artifact")
    def force(user_content, tool_calls, *, iteration, max_iterations):
        return True

    force("本周销售情况，不要输出文档", [], iteration=9, max_iterations=10)
    row = re_mod.pending_rows()[0]
    assert row["forced_target"] == "create_artifact"
    assert row["detail"]["iteration"] == 9
    assert row["detail"]["max_iterations"] == 10
    assert row["detail"]["input"].startswith("本周销售情况")


def test_input_capture_can_be_disabled(monkeypatch):
    monkeypatch.setenv("HARNESS_RULE_EVENTS_CAPTURE_INPUT", "0")

    @observe_rule("test.nocapture")
    def decide(text):
        return True

    decide("secret-looking text")
    assert "input" not in re_mod.pending_rows()[0]["detail"]


def test_raising_rule_is_recorded_then_reraised():
    @observe_rule("test.raises")
    def boom(text):
        raise ValueError("nope")

    with pytest.raises(ValueError):
        boom("x")
    assert counters()["test.raises"] == 1
    assert counters()["test.raises::fired"] == 0
    assert re_mod.pending_rows()[0]["detail"]["raised"] is True


# ── fail-open: telemetry must never break a turn ───────────────────────────


def test_record_never_raises_when_telemetry_is_broken(monkeypatch):
    class Exploding(dict):
        def __setitem__(self, *_):
            raise RuntimeError("counter store broken")

    monkeypatch.setattr(re_mod, "_COUNTS", Exploding())
    record_rule_event("test.exploding", True)  # must not raise


def test_flush_failure_is_counted_not_raised(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_RULE_EVENTS_PERSIST", "1")

    def bad_session():
        raise RuntimeError("db down")

    import app.database as app_db

    monkeypatch.setattr(app_db, "SessionLocal", bad_session)

    record_rule_event("test.flushfail", True)
    assert flush_rule_events() == 0
    assert counters()["flush_failures"] == 1


def test_disabled_flag_records_nothing(monkeypatch):
    monkeypatch.setenv("HARNESS_RULE_EVENTS_ENABLED", "0")

    @observe_rule("test.disabled")
    def decide():
        return True

    assert decide() is True
    assert counters().get("test.disabled") is None


def test_buffered_overflow_is_counted_not_unbounded(monkeypatch):
    monkeypatch.setattr(re_mod, "_QUEUE_MAX", 3)
    monkeypatch.setattr(re_mod, "_BUFFER", type(re_mod._BUFFER)(maxlen=3))
    for i in range(5):
        record_rule_event("test.overflow", i % 2 == 0)
    c = counters()
    assert c["test.overflow"] == 5           # counters never lose data
    assert c["dropped"] == 2                 # rows are bounded, and say so


# ── turn correlation + persistence ─────────────────────────────────────────


def test_turn_binding_flows_into_rows():
    bind_turn(conversation_id="conv-1", agent_name="general_assistant", surface="chat.v3")

    @observe_rule("test.bound")
    def decide(text):
        return True

    decide("hi")
    row = re_mod.pending_rows()[0]
    assert row["conversation_id"] == "conv-1"
    assert row["agent_name"] == "general_assistant"
    assert row["surface"] == "chat.v3"
    assert row["turn_id"]


def test_flush_persists_rows(db, monkeypatch):
    monkeypatch.setenv("HARNESS_RULE_EVENTS_PERSIST", "1")
    bind_turn(conversation_id="conv-42", agent_name="ecisco_bi_assistant")

    @observe_rule("file_turn_guard.should_force_create_file", forced="create_artifact")
    def force(user_content, *, iteration=None, max_iterations=None):
        return True

    force("生成一份文档", iteration=9, max_iterations=10)
    written = flush_rule_events(db=db)

    assert written == 1
    row = db.query(HarnessRuleEvent).one()
    assert row.rule_name == "file_turn_guard.should_force_create_file"
    assert row.fired is True
    assert row.forced_target == "create_artifact"
    assert row.conversation_id == "conv-42"
    assert row.agent_name == "ecisco_bi_assistant"
    assert row.detail["iteration"] == 9
    # Buffered rows are consumed, counters keep the totals.
    assert re_mod.pending_rows() == []
    assert counters()["file_turn_guard.should_force_create_file"] == 1


def test_flush_with_nothing_buffered_is_a_noop(db, monkeypatch):
    monkeypatch.setenv("HARNESS_RULE_EVENTS_PERSIST", "1")
    assert flush_rule_events(db=db) == 0
    assert db.query(HarnessRuleEvent).count() == 0


# ── the summary is the Phase 3 deletion shortlist ─────────────────────────


def test_summary_reports_fire_rate_per_rule():
    @observe_rule("test.hot", forced="create_artifact")
    def hot(text):
        return "docx" in text

    @observe_rule("test.cold")
    def cold(text):
        return "never" in text

    for text in ("a docx", "b", "c docx", "d"):
        hot(text)
        cold(text)

    s = summary()["rules"]
    assert s["test.hot"] == {"evaluated": 4, "fired": 2, "fire_rate": 0.5}
    assert s["test.cold"]["fired"] == 0
    assert s["test.cold"]["fire_rate"] == 0.0


# ── the real rules are instrumented (not just the decorator) ───────────────


def test_all_twelve_real_rules_are_instrumented():
    from app.services.dashboard_turn_guard import (
        is_live_dashboard_request,
        should_force_create_dashboard,
    )
    from app.services.file_turn_guard import (
        is_file_deliverable_request,
        should_force_create_file,
    )
    from app.services.goal_contract import is_report_request
    from app.services.pptx_turn_guard import is_pptx_request, should_force_create_pptx
    from app.services.synexia.default_skills import detect_soft_intent
    from app.services.synexia.intent_router import (
        detect_deck_edit_intent,
        detect_file_intent,
        is_file_read_request,
    )
    from app.services.turn_action import is_online_research_request

    functions = [
        is_file_deliverable_request, should_force_create_file,
        is_live_dashboard_request, should_force_create_dashboard,
        is_pptx_request, should_force_create_pptx,
        is_report_request, is_online_research_request, detect_soft_intent,
        is_file_read_request, detect_file_intent, detect_deck_edit_intent,
    ]
    missing = [f.__name__ for f in functions if not getattr(f, "__harness_observed__", False)]
    assert missing == [], f"un-instrumented decision functions: {missing}"


def test_live_refusal_prompt_is_recorded_as_not_firing():
    """The 2026-09-18 live bug, as evidence: the force rule must NOT fire.

    This is the row Phase 3 will read to justify deleting the force family.
    """
    from app.services.file_turn_guard import is_file_deliverable_request

    prompt = "本周销售情况 ，只统计 惠州伊斯科数据 ，直接输出，不要输出 文档"
    assert is_file_deliverable_request(prompt) == (False, None)

    s = summary()["rules"]
    assert s["file_turn_guard.is_file_deliverable_request"]["fired"] == 0
    assert s["file_turn_guard.is_file_deliverable_request"]["evaluated"] == 1
