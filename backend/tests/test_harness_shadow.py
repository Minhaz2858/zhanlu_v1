"""Phase 2 of the Agent Harness migration: shadow divergence.

Phase 1 installed the primitives, Phase 0 instrumented the rules. Phase 3 deletes
the rules — and must not do so on a hunch. These tests pin three things:

1. The COMPARISON is correct for every primitive kind and every (rule, model)
   combination, including the polarity inversions (suppression) and the
   conservative direction for property-qualified targets (pptx).
2. The VERDICT is safe: a rule that ever supplied an outcome the model had not
   reached is never a deletion candidate, no matter how many redundancy samples
   pile up.
3. OBSERVATION cannot change a turn — fail-open, trace-bounded, env-gated, and no
   Phase 0 row is altered when shadow is off.

Plus a drift guard: every ``@observe_rule`` in the source tree must have a declared
primitive, so a newly instrumented rule fails loudly here instead of silently
reporting nothing.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

import pytest

from app.services.harness import rule_events as re_mod
from app.services.harness import shadow as sh
from app.services.harness.rule_events import (
    bind_turn,
    clear_turn,
    counters,
    record_rule_event,
    reset_counters,
)

APP_DIR = Path(__file__).resolve().parents[1] / "app"

# The 12 rules from the plan document's inventory, plus the turn_planner.
DOCUMENTED_RULES = {
    "file_turn_guard.should_force_create_file",
    "file_turn_guard.is_file_deliverable_request",
    "dashboard_turn_guard.should_force_create_dashboard",
    "dashboard_turn_guard.is_live_dashboard_request",
    "pptx_turn_guard.should_force_create_pptx",
    "pptx_turn_guard.is_pptx_request",
    "goal_contract.is_report_request",
    "turn_action.is_online_research_request",
    "intent_router.is_file_read_request",
    "intent_router.detect_file_intent",
    "intent_router.detect_deck_edit_intent",
    "default_skills.detect_soft_intent",
    "turn_planner.build_turn_plan",
}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setenv("HARNESS_SHADOW_ENABLED", "1")
    monkeypatch.setenv("HARNESS_RULE_EVENTS_ENABLED", "1")
    monkeypatch.setenv("HARNESS_RULE_EVENTS_PERSIST", "0")
    monkeypatch.setenv("HARNESS_RULE_EVENTS_LOG", "0")
    sh.reset()
    reset_counters()
    clear_turn()
    yield
    sh.reset()
    reset_counters()
    clear_turn()


def _act(*calls):
    """Put model-authored calls on the current turn's trace."""
    for call in calls:
        if isinstance(call, tuple):
            sh.note_tool_call(*call)
        else:
            sh.note_tool_call(call)


def _view(rule: str, fired: bool) -> dict:
    """shadow_view() for a rule that must have a declared primitive."""
    view = sh.shadow_view(rule, fired)
    assert view is not None, f"{rule} has no declared primitive"
    return view


def _spec(rule: str) -> sh.Primitive:
    spec = sh.spec_for(rule)
    assert spec is not None, f"{rule} has no declared primitive"
    return spec


# ── the trace ────────────────────────────────────────────────────────────────

def test_trace_starts_empty_and_resets():
    _act("create_artifact")
    assert sh.current_trace()
    sh.start_trace()
    assert sh.current_trace() == []


def test_trace_records_tool_action_and_args():
    sh.note_tool_call("kanban", {"action": "add_card", "title": "t"})
    entry = sh.current_trace()[0]
    assert entry["tool"] == "kanban"
    assert entry["action"] == "add_card"
    assert "add_card" in entry["args"]


def test_trace_is_bounded():
    for i in range(sh._TRACE_MAX + 50):
        sh.note_tool_call("web_search", {"q": i})
    assert len(sh.current_trace()) == sh._TRACE_MAX


def test_trace_is_per_context_not_global():
    """A second thread must not see the first thread's trace — otherwise one
    user's tool calls would be compared against another user's rules."""
    sh.start_trace()
    _act("create_artifact")
    seen: list[int] = []

    def worker():
        seen.append(len(sh.current_trace()))
        sh.start_trace()
        _act("web_search")
        seen.append(len(sh.current_trace()))

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert seen == [0, 1]
    assert [c["tool"] for c in sh.current_trace()] == ["create_artifact"]


def test_note_tool_call_is_fail_open():
    sh.note_tool_call(None)      # type: ignore[arg-type]
    sh.note_tool_call("")        # empty name
    sh.note_tool_call("x", object())  # un-JSON-able args
    assert sh.divergence_counters()["shadow_failures"] == 0


def test_note_tool_call_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("HARNESS_SHADOW_ENABLED", "0")
    _act("create_artifact")
    assert sh.current_trace() == []


# ── primitive resolution ─────────────────────────────────────────────────────

@pytest.mark.parametrize("case", [
    # force / classifier: a deliverable was produced by the model
    ("file_turn_guard.should_force_create_file", ("create_artifact", {"type": "docx"}), True),
    # property-qualified: a docx is NOT a pptx
    ("pptx_turn_guard.should_force_create_pptx", ("create_artifact", {"type": "docx"}), False),
    ("pptx_turn_guard.should_force_create_pptx", ("create_artifact", {"type": "pptx"}), True),
    # unrelated tool does not count as acting
    ("file_turn_guard.should_force_create_file", "web_search", False),
    # dashboards have their own tool family
    ("dashboard_turn_guard.should_force_create_dashboard",
     "create_fullstack_dashboard", True),
    # research
    ("turn_action.is_online_research_request", "web_search", True),
    # skill routing
    ("default_skills.detect_soft_intent", "skills", True),
])
def test_model_acted_resolves_the_primitive(case):
    rule, call, expected = case
    sh.start_trace()
    _act(call)
    assert sh.model_acted(_spec(rule)) is expected


def test_pptx_property_is_conservative():
    """An artifact we cannot prove is a pptx must NOT count as the model acting —
    under-detection keeps a rule we could have deleted; over-detection deletes one
    we should not have."""
    sh.start_trace()
    sh.note_tool_call("create_artifact", {"title": "quarterly review"})
    assert sh.model_acted(_spec("pptx_turn_guard.should_force_create_pptx")) is False


# ── the comparison, per kind ─────────────────────────────────────────────────

@pytest.mark.parametrize("kind,fired,acted,label,diverged,load_bearing", [
    # force
    ("force", True, True, "rule_redundant", True, False),
    ("force", True, False, "rule_load_bearing", False, True),
    ("force", False, True, "rule_missed", True, False),
    ("force", False, False, "both_agree_idle", False, False),
    # classifier
    ("classifier", True, True, "classification_corroborated", False, False),
    ("classifier", True, False, "classification_unconfirmed", False, False),
    ("classifier", False, True, "classification_missed", True, False),
    ("classifier", False, False, "classification_idle", False, False),
    # suppress — the polarity is inverted: firing means "do NOT create"
    ("suppress", True, True, "suppression_overridden", True, False),
    ("suppress", True, False, "suppression_unopposed", False, False),
    ("suppress", False, True, "creation_unconstrained", False, False),
    ("suppress", False, False, "both_agree_idle", False, False),
    # route
    ("route", True, True, "rule_redundant", True, False),
    ("route", True, False, "rule_load_bearing", False, True),
    ("route", False, True, "model_self_routed", False, False),
    ("route", False, False, "both_agree_idle", False, False),
    # plan
    ("plan", True, True, "server_plan_model_todo", True, False),
    ("plan", True, False, "server_plan_only", False, True),
    ("plan", False, True, "model_plan_only", False, False),
    ("plan", False, False, "neither_planned", False, False),
])
def test_classify_matrix(kind, fired, acted, label, diverged, load_bearing):
    spec = sh.Primitive(kind, frozenset({"t"}))
    assert sh.classify(spec, fired, acted) == label
    assert (label in sh._DIVERGENT_LABELS) is diverged
    assert (label in sh._LOAD_BEARING_LABELS) is load_bearing


def test_unknown_kind_is_unclassified():
    assert sh.classify(sh.Primitive("nonsense"), True, True) == "unclassified"


def test_load_bearing_and_divergent_are_mutually_exclusive():
    """A label must never be both 'the model already did it' and 'the rule supplied
    it' — the verdict logic depends on that disjointness."""
    assert not (sh._DIVERGENT_LABELS & sh._LOAD_BEARING_LABELS)


# ── the live-bug shape ───────────────────────────────────────────────────────

def test_the_turn_that_triggered_this_migration():
    """`直接输出，不要输出 文档` — the user forbade a document, the rule forced one.

    Shadow must record the rule as load-bearing, not redundant: the model produced
    nothing, so the rule alone supplied the artifact. That is evidence the rule
    *acted*, not evidence it was right. The verdict `keep_load_bearing` is correct
    and deliberate: deleting this rule on divergence alone would be wrong — the
    bug is that the rule should not have fired at all, which this layer records
    separately via `rule_missed`/`suppression_overridden` in the other direction.
    """
    sh.start_trace()
    # The model obeyed the user and produced no file.
    view = _view("file_turn_guard.should_force_create_file", True)
    assert view["agreement"] == "rule_load_bearing"
    assert view["load_bearing"] is True
    assert view["diverged"] is False
    # ~10 iterations of prose: the trace is empty because nothing was created.
    assert view["primitive"]["trace_len"] == 0


def test_redundant_force_is_flagged_for_deletion():
    """The model produced the deliverable on its own and the rule asked anyway."""
    sh.start_trace()
    _act(("create_artifact", {"type": "docx"}))
    view = _view("file_turn_guard.should_force_create_file", True)
    assert view["agreement"] == "rule_redundant"
    assert view["diverged"] is True
    assert view["load_bearing"] is False


def test_model_created_what_the_suppressing_rule_forbade():
    sh.start_trace()
    _act(("create_artifact", {"type": "docx"}))
    view = _view("intent_router.is_file_read_request", True)
    assert view["agreement"] == "suppression_overridden"
    assert view["diverged"] is True


def test_model_routed_itself_to_a_skill_with_no_rule():
    sh.start_trace()
    _act("skills")
    view = _view("default_skills.detect_soft_intent", False)
    assert view["agreement"] == "model_self_routed"
    assert view["diverged"] is False


# ── unmapped rules and drift ─────────────────────────────────────────────────

def test_unmapped_rule_has_no_view():
    assert sh.shadow_view("not.a.rule", True) is None
    assert sh.record_shadow("not.a.rule", True) is None


def test_unmapped_rules_reports_observed_but_undeclared():
    record_rule_event("mystery.rule", True)
    assert "mystery.rule" in sh.unmapped_rules()
    assert "mystery.rule" not in sh.SPECS


def test_every_observed_rule_in_the_source_tree_has_a_primitive():
    """Drift guard: add an @observe_rule without declaring its primitive and this
    fails, instead of the shadow report silently under-counting."""
    found = set()
    for path in APP_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        found.update(re.findall(r'@observe_rule\(\s*"([^"]+)"', text))
    assert found, "no @observe_rule decorators found — the scan is broken"
    missing = found - set(sh.SPECS)
    assert not missing, f"rules instrumented with no shadow primitive: {sorted(missing)}"


def test_documented_rules_are_all_covered():
    assert DOCUMENTED_RULES <= set(sh.SPECS)


# ── the verdict (Phase 3's shortlist) ────────────────────────────────────────

def test_verdict_needs_a_sample_before_deleting():
    sh.start_trace()
    _act("create_artifact")
    for _ in range(sh.MIN_SAMPLE - 1):
        sh.record_shadow("file_turn_guard.should_force_create_file", True)
    report = sh.divergence_report()
    assert report["rules"]["file_turn_guard.should_force_create_file"]["verdict"] == \
        "insufficient_evidence"


def test_verdict_delete_candidate_after_enough_redundancy():
    sh.start_trace()
    _act("create_artifact")
    for _ in range(sh.MIN_SAMPLE):
        sh.record_shadow("file_turn_guard.should_force_create_file", True)
    stats = sh.divergence_report()["rules"]["file_turn_guard.should_force_create_file"]
    assert stats["verdict"] == "delete_candidate"
    assert stats["diverged"] == sh.MIN_SAMPLE
    assert stats["load_bearing"] == 0
    assert stats["divergence_rate"] == 1.0


def test_one_load_bearing_fire_blocks_deletion_regardless_of_sample_size():
    """The safety property: 200 redundancy samples plus ONE turn where the rule
    supplied the outcome means the rule is still load-bearing."""
    rule = "dashboard_turn_guard.should_force_create_dashboard"
    for _ in range(200):
        sh.start_trace()
        _act("create_fullstack_dashboard")
        sh.record_shadow(rule, True)
    sh.start_trace()  # nothing produced — the rule had to intervene
    sh.record_shadow(rule, True)
    stats = sh.divergence_report()["rules"][rule]
    assert stats["load_bearing"] == 1
    assert stats["verdict"] == "keep_load_bearing"


# ── fail-open integration with Phase 0 ───────────────────────────────────────

def test_phase0_row_gains_shadow_detail():
    bind_turn(conversation_id="conv-1", agent_name="acme", surface="chat.v3")
    sh.start_trace()
    _act(("create_artifact", {"type": "docx"}))
    record_rule_event("file_turn_guard.should_force_create_file", True)
    row = re_mod.pending_rows()[-1]
    assert row["conversation_id"] == "conv-1"       # Phase 0 grouping, now populated
    assert row["detail"]["shadow"] is True
    assert row["detail"]["agreement"] == "rule_redundant"
    assert row["detail"]["diverged"] is True
    assert row["detail"]["primitive"]["model_acted"] is True


def test_phase0_row_unchanged_when_shadow_disabled(monkeypatch):
    monkeypatch.setenv("HARNESS_SHADOW_ENABLED", "0")
    sh.start_trace()
    _act("create_artifact")
    record_rule_event("file_turn_guard.should_force_create_file", True)
    row = re_mod.pending_rows()[-1]
    assert "shadow" not in (row["detail"] or {})
    assert counters()["file_turn_guard.should_force_create_file"] == 1


def test_unmapped_rule_records_phase0_row_without_shadow():
    record_rule_event("some.other.rule", True, detail={"k": "v"})
    row = re_mod.pending_rows()[-1]
    assert row["detail"]["k"] == "v"
    assert "shadow" not in row["detail"]


def test_shadow_failure_does_not_change_the_row(monkeypatch):
    """Break the shadow layer and the Phase 0 row must still be written intact."""
    def boom(*_a, **_k):
        raise RuntimeError("shadow exploded")

    monkeypatch.setattr(sh, "record_shadow", boom)
    record_rule_event("file_turn_guard.should_force_create_file", True, detail={"a": 1})
    row = re_mod.pending_rows()[-1]
    assert row["detail"] == {"a": 1}
    assert counters()["file_turn_guard.should_force_create_file"] == 1


def test_rule_raising_still_records_and_reraises_untouched():
    @re_mod.observe_rule("shadow.probe_rule")
    def probe(text):
        raise ValueError("nope")

    with pytest.raises(ValueError):
        probe("hello")
    assert counters()["shadow.probe_rule"] == 1


def test_report_shape_and_telemetry():
    report = sh.divergence_report()
    assert report["min_sample"] == sh.MIN_SAMPLE
    assert report["telemetry"] == {"shadow_failures": 0}
    assert isinstance(report["unmapped"], list)


# ── end-of-turn resolution ───────────────────────────────────────────────────
# The three force guards (file / dashboard / pptx) return False when the artifact
# already exists, so a fire proves only that nothing existed AT THAT MOMENT.
# finish_turn() resolves that against the turn's final trace.

def test_fired_then_delivered_is_preceded_delivery():
    rule = "file_turn_guard.should_force_create_file"
    sh.start_trace()
    sh.record_shadow(rule, True)                     # fired, nothing produced yet
    _act(("create_artifact", {"type": "docx"}))      # the nudge landed
    resolved = sh.finish_turn()
    assert resolved["rule_preceded_delivery"] == [rule]
    assert resolved["rule_fired_no_delivery"] == []


def test_fired_and_still_nothing_is_no_delivery():
    rule = "file_turn_guard.should_force_create_file"
    sh.start_trace()
    sh.record_shadow(rule, True)
    resolved = sh.finish_turn()
    assert resolved["rule_fired_no_delivery"] == [rule]
    assert resolved["rule_preceded_delivery"] == []


def test_only_load_bearing_fires_are_held_for_resolution():
    """A redundant fire is already decided at fire time — holding it would
    double-count it as a delivery outcome."""
    rule = "file_turn_guard.should_force_create_file"
    sh.start_trace()
    _act(("create_artifact", {"type": "docx"}))      # model acted first
    sh.record_shadow(rule, True)                     # -> rule_redundant
    resolved = sh.finish_turn()
    assert resolved["rule_preceded_delivery"] == []
    assert resolved["rule_fired_no_delivery"] == []


def test_finish_turn_is_idempotent():
    rule = "file_turn_guard.should_force_create_file"
    sh.start_trace()
    sh.record_shadow(rule, True)
    assert sh.finish_turn()["rule_fired_no_delivery"] == [rule]
    assert sh.finish_turn() == {"rule_preceded_delivery": [], "rule_fired_no_delivery": []}


def test_finish_turn_noop_when_disabled(monkeypatch):
    rule = "file_turn_guard.should_force_create_file"
    sh.start_trace()
    sh.record_shadow(rule, True)
    monkeypatch.setenv("HARNESS_SHADOW_ENABLED", "0")
    assert sh.finish_turn() == {}


def test_force_guard_that_never_delivers_is_kept_but_flagged_ineffective():
    """The honest limit of this layer: a guard that fires and the turn still ships
    nothing did not mis-decide, so it is not a deletion candidate — but it is not
    achieving its aim either. That is a Phase 4 problem, not a Phase 3 deletion."""
    rule = "dashboard_turn_guard.should_force_create_dashboard"
    for _ in range(sh.MIN_SAMPLE + 5):
        sh.start_trace()
        sh.record_shadow(rule, True)
        sh.finish_turn()                              # nothing ever delivered
    stats = sh.divergence_report()["rules"][rule]
    assert stats["fired_no_delivery"] == sh.MIN_SAMPLE + 5
    assert stats["preceded_delivery"] == 0
    assert stats["verdict"] == "keep_but_ineffective"


def test_one_delivery_reclassifies_the_verdict():
    rule = "dashboard_turn_guard.should_force_create_dashboard"
    for _ in range(5):
        sh.start_trace()
        sh.record_shadow(rule, True)
        sh.finish_turn()
    sh.start_trace()
    sh.record_shadow(rule, True)
    _act("create_fullstack_dashboard")
    sh.finish_turn()
    stats = sh.divergence_report()["rules"][rule]
    assert stats["preceded_delivery"] == 1
    assert stats["fired_no_delivery"] == 5
    assert stats["verdict"] == "keep_load_bearing"


def test_report_exposes_both_resolution_counts():
    report = sh.divergence_report()
    rule = "file_turn_guard.should_force_create_file"
    sh.start_trace()
    sh.record_shadow(rule, True)
    sh.finish_turn()
    stats = sh.divergence_report()["rules"][rule]
    assert {"preceded_delivery", "fired_no_delivery", "load_bearing", "diverged",
            "divergence_rate", "verdict"} <= set(stats)


def test_trace_default_is_not_shared_between_contexts():
    """A context that never called start_trace() must not inherit — or mutate —
    another context's trace. A mutable ContextVar default (``default=[]``) would be
    shared, so one turn's tool calls would leak into another turn's comparison."""
    lengths: list[int] = []

    def worker():
        # Deliberately no start_trace(): exercises the ContextVar default path.
        sh.note_tool_call("create_artifact")
        lengths.append(len(sh.current_trace()))

    for _ in range(2):
        t = threading.Thread(target=worker)
        t.start()
        t.join()
    # Each context saw exactly its own single call — not 1 then 2.
    assert lengths == [1, 1]


def test_held_fires_default_is_not_shared_between_contexts():
    lengths: list[int] = []

    def worker():
        sh.record_shadow("file_turn_guard.should_force_create_file", True)
        lengths.append(len(sh.finish_turn()["rule_fired_no_delivery"]))

    for _ in range(2):
        t = threading.Thread(target=worker)
        t.start()
        t.join()
    assert lengths == [1, 1]
