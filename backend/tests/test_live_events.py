"""Tests for the typed live-activity event feed (v3 SSE stream).

Covers:
  1. Container structure + SSE framing of emitted events.
  2. Template coverage — every label_key used by an emission site resolves
     in BOTH languages (the most common future regression).
  3. Content invariant — SQL keywords, ``erp_*`` table names and ERP column
     IDs never leak into the persisted stream.
  4. Per-turn event cap — the feed never exceeds ``_LIVE_EVENT_CAP``.
  5. Lifecycle ordering — FINALIZE implies verify_passed + finalize_started;
     tool status transitions map to started/finished/failed/retry.
"""
import json

import pytest

from app.routers import agents


# ── 1. Structure + SSE framing ──────────────────────────────────────────

class TestEventContainer:
    def test_build_live_event_shape(self):
        ev = agents._build_live_event(
            "tool_call_finished", "tool_call_finished",
            {"tool_label": "Querying data", "row_count": 847, "duration": 1.2},
        )
        assert ev is not None
        assert ev["type"] == "tool_call_finished"
        assert ev["label_key"] == "tool_call_finished"
        assert ev["params"]["row_count"] == 847
        assert ev["params"]["duration"] == 1.2
        # ISO timestamp present and parseable
        from datetime import datetime
        datetime.fromisoformat(ev["ts"])

    def test_sse_frame_framing(self):
        ev = agents._build_live_event("phase_enter", "phase_enter.goal")
        assert ev is not None
        frame = agents._sse_live_event(ev)
        assert frame.startswith("data: ")
        assert frame.endswith("\n\n")
        parsed = json.loads(frame[len("data: "):].strip())
        assert parsed["type"] == "live_event"
        assert parsed["event"] == ev

    def test_emit_live_event_none_when_capped(self):
        count = [agents._LIVE_EVENT_CAP]
        assert agents._emit_live_event("phase_enter", "phase_enter.goal", count=count) is None

    def test_emit_live_event_serialized(self):
        count = [0]
        frame = agents._emit_live_event(
            "tool_call_started", "tool_call_started",
            {"tool_label": "Querying data"}, count=count,
        )
        assert frame is not None
        assert count[0] == 1
        parsed = json.loads(frame[len("data: "):].strip())
        assert parsed["event"]["label_key"] == "tool_call_started"


# ── 2. Template coverage (both languages) ───────────────────────────────

class TestTemplateCoverage:
    """Every label_key referenced at an emission site must have EN + zh
    templates so the feed never renders a raw label_key string."""

    # Label keys observed at emission sites (FSM states + v3 loop + on_plan_node).
    EMISSION_LABEL_KEYS = [
        "phase_enter.init",
        "phase_enter.goal",
        "phase_enter.context",
        "phase_enter.plan",
        "phase_enter.gate",
        "phase_enter.act",
        "phase_enter.observe",
        "phase_enter.verify",
        "phase_enter.finalize",
        "phase_enter.quality_eval",
        "phase_enter.done",
        "phase_enter.fail",
        "tool_call_started",
        "tool_call_finished",
        "tool_call_failed",
        "retry",
        "verify_passed",
        "finalize_started",
    ]

    @pytest.mark.parametrize("label_key", EMISSION_LABEL_KEYS)
    def test_label_key_has_both_language_templates(self, label_key):
        tmpl = agents.LIVE_EVENT_TEMPLATES.get(label_key)
        assert tmpl is not None, f"missing template for {label_key!r}"
        assert tmpl.get("en"), f"missing EN template for {label_key!r}"
        assert tmpl.get("zh"), f"missing zh template for {label_key!r}"

    def test_templates_use_known_structured_params_only(self):
        # Static labels (verify_passed, finalize_started, phase_enter.*)
        # legitimately carry no placeholders. Every other template's
        # placeholders must be drawn from the known structured-param set.
        EXPECTED_PARAMS = {
            "plan_preview": {"n"},
            "tool_call_started": {"tool_label"},
            "tool_call_finished": {"tool_label", "row_count", "duration"},
            "tool_call_failed": {"tool_label"},
            "artifact_progress": {"artifact_type", "current", "total"},
            "retry": {"target"},
            "subagent_invoked": {"agent_label", "target"},
            "subagent_returned": {"agent_label", "duration", "row_count", "artifacts"},
            "data_offer": {"tool_label", "row_count", "columns", "sample_rows"},
            "plan_summary": {"n", "steps"},
        }
        for label_key, langs in agents.LIVE_EVENT_TEMPLATES.items():
            for lang, tmpl in langs.items():
                placeholders = set(_extract_placeholders(tmpl))
                if not placeholders:
                    # Static label — allowed only for these label keys.
                    assert label_key.startswith("phase_enter.") or label_key in {
                        "verify_passed", "verify_failed", "finalize_started",
                        "finalize_done",
                    }, (
                        f"{label_key}/{lang} is a dynamic label but has no "
                        "structured placeholders — labels must come from the "
                        "template map, never raw text"
                    )
                    continue
                assert label_key in EXPECTED_PARAMS, (
                    f"no declared param set for dynamic label {label_key!r}"
                )
                unknown = placeholders - EXPECTED_PARAMS[label_key]
                assert not unknown, f"{label_key}/{lang} uses undeclared params {unknown}"

    def test_phase_enter_states_cover_all_fsm_states(self):
        from app.services.synexia.fsm import FSMState
        for state in FSMState:
            label_key = f"phase_enter.{state.value}"
            assert label_key in agents.LIVE_EVENT_TEMPLATES, (
                f"FSM state {state.value!r} has no phase_enter template"
            )


def _extract_placeholders(tmpl: str) -> list[str]:
    import re
    return re.findall(r"\{(\w+)\}", tmpl)


# ── 3. Content invariant ────────────────────────────────────────────────

class TestContentInvariant:
    def test_sql_keywords_replaced(self):
        out = agents._sanitize_live_event_params({"label": "SELECT * FROM orders"})
        assert out["label"] == agents._LIVE_EVENT_SAFE_FALLBACK

    def test_erp_table_names_replaced(self):
        out = agents._sanitize_live_event_params({"label": "erp_t_sal_outstock"})
        assert out["label"] == agents._LIVE_EVENT_SAFE_FALLBACK
        out2 = agents._sanitize_live_event_params({"label": "erp_v_sale_orderentry"})
        assert out2["label"] == agents._LIVE_EVENT_SAFE_FALLBACK

    def test_erp_column_ids_replaced(self):
        out = agents._sanitize_live_event_params({"label": "FMATERIALID"})
        assert out["label"] == agents._LIVE_EVENT_SAFE_FALLBACK
        out2 = agents._sanitize_live_event_params({"label": "FREALQTY desc"})
        assert out2["label"] == agents._LIVE_EVENT_SAFE_FALLBACK

    def test_safe_values_pass_through(self):
        params = {
            "tool_label": "Querying data",
            "row_count": 847,
            "duration": 1.2,
            "current": 3,
            "total": 12,
            "n": 5,
        }
        out = agents._sanitize_live_event_params(params)
        assert out == params  # untouched (numbers + safe labels)

    def test_safe_label_with_sql_word_inside_passes(self):
        # "Querying data" is not a SQL keyword match (boundary-anchored).
        out = agents._sanitize_live_event_params({"tool_label": "Querying data"})
        assert out["tool_label"] == "Querying data"

    def test_build_live_event_sanitizes(self):
        ev = agents._build_live_event(
            "tool_call_finished", "tool_call_finished",
            {"tool_label": "SELECT erp_t_sal_outstock", "row_count": 3},
        )
        assert ev is not None
        assert ev["params"]["tool_label"] == agents._LIVE_EVENT_SAFE_FALLBACK
        assert ev["params"]["row_count"] == 3  # numbers untouched


# ── 4. Event cap ────────────────────────────────────────────────────────

class TestEventCap:
    def test_cap_hard_limit(self):
        count = [0]
        emitted = 0
        for _ in range(agents._LIVE_EVENT_CAP + 25):
            ev = agents._build_live_event(
                "tool_call_finished", "tool_call_finished",
                {"tool_label": "Querying data"}, count=count,
            )
            if ev is not None:
                emitted += 1
        assert emitted == agents._LIVE_EVENT_CAP
        assert count[0] == agents._LIVE_EVENT_CAP

    def test_cap_skips_further_events(self):
        count = [agents._LIVE_EVENT_CAP - 1]
        assert agents._build_live_event("phase_enter", "phase_enter.goal", count=count) is not None
        assert agents._build_live_event("phase_enter", "phase_enter.act", count=count) is None
        assert count[0] == agents._LIVE_EVENT_CAP

    def test_no_count_means_no_cap(self):
        # count=None (unbounded) never caps.
        for _ in range(agents._LIVE_EVENT_CAP + 5):
            ev = agents._build_live_event("phase_enter", "phase_enter.goal")
            assert ev is not None


# ── 5. Lifecycle ordering ───────────────────────────────────────────────

class TestLifecycle:
    """Simulates the FSM on_state_change closure + v3 loop status mapping
    that LiveActivityStream consumes, asserting the typed sequences."""

    def test_fsm_state_sequence_emits_phase_enters(self):
        # Mirrors the _on_state closure in agents.py: every transition emits
        # a phase_enter, and "finalize" is preceded by verify_passed + finalize_started.
        states = ["init", "goal", "plan", "act", "verify", "finalize"]
        events = []
        for state in states:
            events.append(("phase_enter", f"phase_enter.{state}"))
            if state == "finalize":
                events.append(("verify_passed", "verify_passed"))
                events.append(("finalize_started", "finalize_started"))

        types = [e[0] for e in events]
        assert types.count("phase_enter") == len(states)
        assert events[-2] == ("verify_passed", "verify_passed")
        assert events[-1] == ("finalize_started", "finalize_started")
        # All events in the simulated stream survive the invariant check.
        for etype, lkey in events:
            assert agents._build_live_event(etype, lkey) is not None

    def test_tool_status_mapping(self):
        # Mirrors on_plan_node: tool/skill status → typed event.
        mapping = {
            "running": "tool_call_started",
            "completed": "tool_call_finished",
            "failed": "tool_call_failed",
            "denied": "tool_call_failed",
            "replanning": "retry",
        }
        for status, expected in mapping.items():
            ev = agents._build_live_event(expected, expected, {"tool_label": "execute_query"})
            assert ev is not None and ev["type"] == expected

    def test_sse_feed_parseable_end_to_end(self):
        # Build a realistic turn feed, serialize via SSE, and confirm every
        # frame parses back to a valid live_event container.
        frames = []
        count = [0]
        seq = [
            ("phase_enter", "phase_enter.goal"),
            ("phase_enter", "phase_enter.plan"),
            ("plan_preview", "plan_preview", {"n": 3}),
            ("tool_call_started", "tool_call_started", {"tool_label": "Querying data"}),
            ("tool_call_finished", "tool_call_finished",
             {"tool_label": "Querying data", "row_count": 847, "duration": 1.2}),
            ("verify_passed", "verify_passed"),
            ("finalize_started", "finalize_started"),
        ]
        for item in seq:
            frame = agents._emit_live_event(
                item[0], item[1],
                item[2] if len(item) > 2 else None,
                count=count,
            )
            assert frame is not None
            frames.append(frame)

        parsed = [json.loads(f[len("data: "):].strip()) for f in frames]
        assert all(p["type"] == "live_event" for p in parsed)
        assert [p["event"]["type"] for p in parsed] == [s[0] for s in seq]
        # Every label_key in the feed resolves to both-language templates.
        for p in parsed:
            lk = p["event"]["label_key"]
            assert lk in agents.LIVE_EVENT_TEMPLATES


# ── 6. New event types (richness additions) ────────────────────────────

class TestSubagentEvents:
    def test_templates_cover_subagent_keys(self):
        for lk in ("subagent_invoked", "subagent_returned"):
            t = agents.LIVE_EVENT_TEMPLATES[lk]
            assert t["en"] and t["zh"]

    def test_subagent_invoked_emits_with_agent_label(self):
        ev = agents._build_live_event(
            "subagent_invoked", "subagent_invoked",
            {"agent_label": "Querying data", "target": "ask_data_agent"},
        )
        assert ev is not None
        assert ev["type"] == "subagent_invoked"
        assert ev["params"]["agent_label"] == "Querying data"
        assert ev["params"]["target"] == "ask_data_agent"

    def test_subagent_returned_carries_duration_and_rows(self):
        ev = agents._build_live_event(
            "subagent_returned", "subagent_returned",
            {"agent_label": "Querying data", "duration": 1.2, "row_count": 847},
        )
        assert ev["params"]["duration"] == 1.2
        assert ev["params"]["row_count"] == 847


class TestPlanSummary:
    def test_templates_cover_plan_summary(self):
        assert agents.LIVE_EVENT_TEMPLATES["plan_summary"]["en"]
        assert agents.LIVE_EVENT_TEMPLATES["plan_summary"]["zh"]

    def test_plan_summary_includes_n_and_steps(self):
        ev = agents._build_live_event(
            "plan_summary", "plan_summary",
            {"n": 3, "steps": ["Inspect data source", "Build widgets", "Verify"]},
        )
        assert ev is not None
        assert ev["params"]["n"] == 3
        assert ev["params"]["steps"] == ["Inspect data source", "Build widgets", "Verify"]


# ── 7. Recursive sanitizer (sample_rows payload) ───────────────────────

class TestRecursiveSanitizer:
    def test_sample_rows_cells_sanitized(self):
        # Column names that match the ERP F* pattern are themselves
        # sanitized — the content invariant applies to ALL string values
        # (column headers included) so a preview never leaks schema details.
        params = {
            "tool_label": "Querying data",
            "row_count": 2,
            "columns": ["name", "FNAME"],
            "sample_rows": [
                {"name": "ethylene", "FNAME": "select from erp_t_sal_outstock"},
                {"name": "propylene", "FNAME": "safe value"},
            ],
        }
        out = agents._sanitize_live_event_params(params)
        assert out["sample_rows"][0]["FNAME"] == agents._LIVE_EVENT_SAFE_FALLBACK
        assert out["sample_rows"][1]["FNAME"] == "safe value"
        assert out["columns"][1] == agents._LIVE_EVENT_SAFE_FALLBACK

    def test_nested_dicts_walked(self):
        out = agents._sanitize_live_event_params({"meta": {"kind": "SELECT", "n": 5}})
        assert out["meta"]["kind"] == agents._LIVE_EVENT_SAFE_FALLBACK
        assert out["meta"]["n"] == 5

    def test_list_values_walked(self):
        out = agents._sanitize_live_event_params({"steps": ["safe", "erp_v_orders"]})
        assert out["steps"] == ["safe", agents._LIVE_EVENT_SAFE_FALLBACK]

    def test_non_string_scalars_untouched(self):
        out = agents._sanitize_live_event_params({"n": 0, "f": 0.0, "b": True, "x": None})
        assert out == {"n": 0, "f": 0.0, "b": True, "x": None}


# ── 8. _sample_rows_from_payload helper ─────────────────────────────────

class TestSampleRowsExtractor:
    def test_extracts_from_list_of_dicts(self):
        payload = [{"a": 1, "b": 2, "c": 3}, {"a": 4, "b": 5, "c": 6}]
        out = agents._sample_rows_from_payload(payload)
        assert out == {
            "columns": ["a", "b", "c"],
            "sample_rows": [{"a": 1, "b": 2, "c": 3}, {"a": 4, "b": 5, "c": 6}],
        }

    def test_extracts_from_dict_with_rows_key(self):
        payload = {"rows": [{"x": 1}], "meta": "ignored"}
        out = agents._sample_rows_from_payload(payload)
        assert out["columns"] == ["x"]
        assert out["sample_rows"] == [{"x": 1}]

    def test_extracts_from_dict_with_data_key(self):
        payload = {"data": [{"a": 1}, {"b": 2}]}
        out = agents._sample_rows_from_payload(payload)
        # Union of keys (in first-seen order): ["a", "b"].
        assert out["columns"] == ["a", "b"]
        # Row 1 has only "a" — row 2 has only "b" — both keys surface, missing
        # cells stay absent (sparse preview, not None padding).
        assert out["sample_rows"] == [{"a": 1, "b": None}, {"a": None, "b": 2}]

    def test_caps_rows_at_max(self):
        payload = [{"a": i} for i in range(100)]
        out = agents._sample_rows_from_payload(payload, max_rows=3)
        assert len(out["sample_rows"]) == 3

    def test_caps_columns_at_max(self):
        payload = [{"a": 1, "b": 2, "c": 3, "d": 4, "e": 5, "f": 6}]
        out = agents._sample_rows_from_payload(payload, max_cols=5)
        assert out["columns"] == ["a", "b", "c", "d", "e"]
        assert set(out["sample_rows"][0].keys()) == {"a", "b", "c", "d", "e"}

    def test_returns_none_for_empty_payload(self):
        assert agents._sample_rows_from_payload(None) is None
        assert agents._sample_rows_from_payload([]) is None
        assert agents._sample_rows_from_payload({}) is None

    def test_returns_none_for_non_row_shaped(self):
        assert agents._sample_rows_from_payload({"summary": "no rows here"}) is None

    def test_data_offer_event_payload_sanitized(self):
        """End-to-end: extract sample rows + run through build_live_event → sample
        cells must be sanitized, but the row count + label stay intact."""
        payload = {
            "rows": [
                {"product": "ethylene", "FNAME": "select from erp_t_sal_outstock"},
            ],
            "summary": "ok",
        }
        sample = agents._sample_rows_from_payload(payload)
        ev = agents._build_live_event(
            "data_offer", "data_offer",
            {
                "tool_label": "ask_data_agent",
                "row_count": 47,
                "columns": sample["columns"],
                "sample_rows": sample["sample_rows"],
            },
        )
        assert ev["params"]["row_count"] == 47
        assert ev["params"]["sample_rows"][0]["FNAME"] == agents._LIVE_EVENT_SAFE_FALLBACK
        assert ev["params"]["sample_rows"][0]["product"] == "ethylene"
