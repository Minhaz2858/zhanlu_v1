"""Unit tests for the Kimi/GPT-style chat UX helpers in app.routers.agents.

Covers:
- ``_extract_citations_from_tool_calls`` — data-source citations extracted
  from a turn's tool calls (FSM shape, legacy shape, nested
  fetch_data_batch, dedup, malformed-input defenses).
- ``_resolve_regenerate_turn`` — the ``regenerate: true`` backend contract:
  reuse the last user message (content + file_urls + skill) and pop the
  previous assistant reply so the history stays [.., user, NEW assistant].
"""

from app.routers.agents import (
    _extract_citations_from_tool_calls,
    _resolve_regenerate_turn,
)

import pytest


class TestExtractCitationsFromToolCalls:
    """Kimi/GPT-style source chips — extraction from tool call shapes."""

    def test_fsm_shape_with_rows_list(self):
        tool_calls = [{
            "name": "execute_query",
            "result": {
                "source_id": "kb_123",
                "source_name": "Production DB",
                "rows": [{"a": 1}, {"a": 2}],
            },
        }]
        citations = _extract_citations_from_tool_calls(tool_calls)
        assert citations == [{
            "source_id": "kb_123",
            "source_name": "Production DB",
            "rows": 2,
        }]

    def test_fsm_shape_with_rows_int(self):
        """A row COUNT (int) is kept as-is; only lists/tuples are len()'d."""
        tool_calls = [{
            "name": "execute_query",
            "result": {"source_id": "s1", "source_name": "DB", "rows": 42},
        }]
        citations = _extract_citations_from_tool_calls(tool_calls)
        assert citations[0]["rows"] == 42

    def test_legacy_shape_results_dict(self):
        tool_calls = [{
            "name": "ask_data_agent",
            "results": {
                "source_name": "Legacy DS",
                "rows": [{"x": 1}],
            },
        }]
        citations = _extract_citations_from_tool_calls(tool_calls)
        assert citations == [{
            "source_id": None,
            "source_name": "Legacy DS",
            "rows": 1,
        }]

    def test_nested_fetch_data_batch(self):
        tool_calls = [{
            "name": "fetch_data_batch",
            "result": {
                "results": [
                    {"label": "Sales 2026", "rows": [{"r": 1}, {"r": 2}]},
                    {"label": "Inventory", "rows": [{"r": 3}]},
                ],
            },
        }]
        citations = _extract_citations_from_tool_calls(tool_calls)
        names = {c["source_name"] for c in citations}
        assert names == {"Sales 2026", "Inventory"}
        assert {c["rows"] for c in citations} == {2, 1}

    def test_dedupes_by_source_id(self):
        tool_calls = [
            {"name": "q", "result": {"source_id": "kb_1", "source_name": "DB"}},
            {"name": "q2", "result": {"source_id": "kb_1", "source_name": "DB"}},
        ]
        citations = _extract_citations_from_tool_calls(tool_calls)
        assert len(citations) == 1

    def test_duplicate_source_upgrades_rows_from_probe_zero(self):
        # A turn often probes a datasource (0 rows) before the real query.
        # The citation must keep the LATER non-zero count, not the first
        # probe's 0 — otherwise the chip shows "0 rows" for data that exists.
        tool_calls = [
            {"name": "q", "result": {"source_id": "kb_1", "source_name": "DB", "rows": 0}},
            {"name": "q2", "result": {"source_id": "kb_1", "source_name": "DB", "rows": 4}},
        ]
        citations = _extract_citations_from_tool_calls(tool_calls)
        assert len(citations) == 1
        assert citations[0]["rows"] == 4

    def test_duplicate_source_keeps_highest_rows(self):
        tool_calls = [
            {"name": "q", "result": {"source_id": "kb_1", "source_name": "DB", "rows": 3}},
            {"name": "q2", "result": {"source_id": "kb_1", "source_name": "DB", "rows": 7}},
            {"name": "q3", "result": {"source_id": "kb_1", "source_name": "DB", "rows": 5}},
        ]
        citations = _extract_citations_from_tool_calls(tool_calls)
        assert len(citations) == 1
        assert citations[0]["rows"] == 7

    def test_duplicate_source_does_not_downgrade_rows(self):
        tool_calls = [
            {"name": "q", "result": {"source_id": "kb_1", "source_name": "DB", "rows": 12}},
            {"name": "q2", "result": {"source_id": "kb_1", "source_name": "DB", "rows": 0}},
        ]
        citations = _extract_citations_from_tool_calls(tool_calls)
        assert len(citations) == 1
        assert citations[0]["rows"] == 12

    def test_source_name_falls_back_to_id(self):
        tool_calls = [{"name": "q", "result": {"source_id": "kb_9"}}]
        citations = _extract_citations_from_tool_calls(tool_calls)
        assert citations[0]["source_name"] == "kb_9"

    def test_skips_malformed_entries(self):
        tool_calls = [
            "not-a-dict",
            {"name": "q"},                      # no result
            {"name": "q", "result": "str"},     # result not a dict
            {"name": "q", "result": {}},        # no source info
            None,
        ]
        assert _extract_citations_from_tool_calls(tool_calls) == []

    def test_empty_input(self):
        assert _extract_citations_from_tool_calls([]) == []
        assert _extract_citations_from_tool_calls(None) == []

    def test_legacy_results_source_as_dict(self):
        """REGRESSION (2026-08-31 E2E): ``results[\"source\"]`` is a nested
        OBJECT {id, name, db_type} on describe_schema — feeding the dict
        into the seen-set raised TypeError (unhashable) which the callers'
        ``except: pass`` swallowed, silently dropping ALL citations."""
        tool_calls = [{
            "name": "describe_schema",
            "results": {
                "success": True,
                "source": {
                    "id": "kb-demo-001",
                    "name": "Demo E2E PostgreSQL",
                    "db_type": "postgresql",
                    "database_name": "demo_e2e",
                },
                "tables": ["customers", "orders", "products"],
            },
        }]
        citations = _extract_citations_from_tool_calls(tool_calls)
        assert len(citations) == 1
        assert citations[0]["source_id"] is None
        assert citations[0]["source_name"] == "Demo E2E PostgreSQL"
        assert citations[0]["rows"] is None

    def test_legacy_fetch_data_batch_label_only_subs(self):
        """REGRESSION (2026-08-31 E2E): real fetch_data_batch results carry
        nested subs with only ``label``/``rows``/``row_count`` (no
        source_name/source) — must still produce one chip per sub-query."""
        subs = [
            {"label": "order_totals", "row_count": 1, "rows": [{"total": 64}], "success": True},
            {"label": "orders_by_region", "row_count": 4, "rows": [{"r": 1}, {"r": 2}, {"r": 3}, {"r": 4}], "success": True},
            {"label": "top_customers", "row_count": 8, "rows": [{"c": 1}] * 8, "success": True},
        ]
        tool_calls = [{
            "name": "fetch_data_batch",
            "results": {
                "success": True,
                "hint": "ok",
                "summary": "4 sub-queries",
                "results": subs,
            },
        }]
        citations = _extract_citations_from_tool_calls(tool_calls)
        names = {c["source_name"] for c in citations}
        assert names == {"order_totals", "orders_by_region", "top_customers"}
        rows = {c["source_name"]: c["rows"] for c in citations}
        assert rows == {"order_totals": 1, "orders_by_region": 4, "top_customers": 8}

    def test_real_turn_mixed_shapes(self):
        """REGRESSION (2026-08-31 E2E): the exact 6-tool-call turn from a
        live Data Analysis chat — list_data_sources adds nothing,
        describe_schema source dicts extract by name, fetch_data_batch
        subs extract by label, all deduped into string chips."""
        tool_calls = [
            {"name": "list_data_sources", "results": {"success": True, "data_sources": [
                {"id": "kb-demo-001", "name": "Demo E2E PostgreSQL", "source_kind": "database", "db_type": "postgresql"},
            ]}},
            {"name": "describe_schema", "results": {"success": True, "source": {
                "id": "kb-demo-001", "name": "Demo E2E PostgreSQL", "db_type": "postgresql"}, "tables": ["orders"]}},
            {"name": "fetch_data_batch", "results": {"success": True, "results": [
                {"label": "order_totals", "row_count": 1, "rows": [{"total": 64}]},
                {"label": "orders_by_region", "row_count": 4, "rows": [{}, {}, {}, {}]},
            ]}},
        ]
        citations = _extract_citations_from_tool_calls(tool_calls)
        # describe_schema's name + the two sub-query labels; no crash, no dict chips
        assert {c["source_name"] for c in citations} == {
            "Demo E2E PostgreSQL", "order_totals", "orders_by_region",
        }
        for c in citations:
            assert isinstance(c["source_name"], str)


class TestResolveRegenerateTurn:
    """``regenerate: true`` contract — reuse last user turn, pop old reply."""

    def _messages(self):
        return [
            {"role": "user", "content": "What is revenue?"},
            {"role": "assistant", "content": "Revenue is ¥123M."},
        ]

    def test_reuses_last_user_content_when_not_provided(self):
        messages = self._messages()
        content, files, skill, skill_id = _resolve_regenerate_turn(
            messages, "user", "", [], None, None
        )
        assert content == "What is revenue?"

    def test_keeps_provided_content(self):
        messages = self._messages()
        content, files, skill, skill_id = _resolve_regenerate_turn(
            messages, "user", "Edited prompt?", [], None, None
        )
        assert content == "Edited prompt?"

    def test_copies_file_urls_filtered_to_uploads(self):
        messages = [
            {"role": "user", "content": "Analyze this",
             "file_urls": ["/api/uploads/abc.xlsx", "https://evil.example/x"]},
            {"role": "assistant", "content": "done"},
        ]
        content, files, skill, skill_id = _resolve_regenerate_turn(
            messages, "user", "", [], None, None
        )
        assert files == ["/api/uploads/abc.xlsx"]

    def test_copies_skill_from_original_turn(self):
        messages = [
            {"role": "user", "content": "hi",
             "selected_skill": {"name": "analyst"}, "selected_skill_id": "sk_1"},
            {"role": "assistant", "content": "hello"},
        ]
        content, files, skill, skill_id = _resolve_regenerate_turn(
            messages, "user", "", [], None, None
        )
        assert skill == {"name": "analyst"}
        assert skill_id == "sk_1"

    def test_pops_previous_assistant_reply(self):
        messages = self._messages()
        _resolve_regenerate_turn(messages, "user", "", [], None, None)
        roles = [m["role"] for m in messages]
        assert roles == ["user"]  # [.., user, NEW assistant] — no stale answer

    def test_pops_only_last_assistant(self):
        messages = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "a2"},
        ]
        _resolve_regenerate_turn(messages, "user", "", [], None, None)
        assert [m["role"] for m in messages] == ["user", "assistant", "user"]

    def test_empty_messages_unchanged(self):
        messages = []
        content, files, skill, skill_id = _resolve_regenerate_turn(
            messages, "user", "", [], None, None
        )
        assert content == ""
        assert files == []
        assert skill is None and skill_id is None
        assert messages == []


class TestNudgeBubbleCollapseGuard:
    """REGRESSION (2026-08-31): every v3-loop nudge site must emit a
    ``content_preserve`` SSE event (keeps the old bubble text visible with a
    "Refining answer…" indicator) instead of a bare ``content_replace`` —
    the bare replace made the bubble visibly collapse-then-regenerate when a
    guard (hallucination guardrail, apology/bounce-back force, goal-contract,
    verify/pptx/file/dashboard/delegation nudges) retried the LLM call.

    Symptom reported by the user: "agent makes an answer several times, then
    collapses it, then answers again" — reproduced live (replay of a Data
    Analysis turn: 1755 deltas + 1 content_replace + 2 hallucination-guardrail
    nudges). Deepseek-chat frequently answers data questions WITHOUT calling
    ask_data_agent, so the guardrail fires on real user turns, not just
    edge cases.

    This guard pins the pattern at the source level because the SSE emission
    is inline in a giant async generator (not unit-testable without a full
    mocked-LLM loop): it asserts every nudge site's reason string exists with
    content_preserve, and that no bare content_replace is followed by
    ``_nudge_replacement_pending = True`` inside the v3 loop (the nudge
    signature). The legitimate content_replace sites (next-iteration latch
    and tool-call cleanup) reset the flag to False instead.
    """

    NUDGE_REASONS = [
        "goal_contract",
        "apology_force",
        "bounce_back_force",
        "empty_answer_force",
        "hallucination_guardrail",
        "cad_verify",
        "verify_nudge",
        "pptx_guard",
        "file_guard",
        "dashboard_narration",
        "delegation_enforce",
        "self_eval_nudge",
    ]

    @pytest.fixture(scope="class")
    def v3_source(self):
        import inspect
        import app.routers.agents as agents_mod
        return inspect.getsource(agents_mod)

    def test_all_nudge_reasons_emit_content_preserve(self, v3_source):
        for reason in self.NUDGE_REASONS:
            assert (
                f'"type": "content_preserve"' in v3_source
                and f'"reason": "{reason}"' in v3_source
            ), f"nudge site '{reason}' lost its content_preserve emission"

    def test_no_bare_content_replace_in_nudge_blocks(self, v3_source):
        # In the v3 loop region, a content_replace must NOT be followed by
        # `_nudge_replacement_pending = True` (the nudge signature) — that
        # pairing was the visible-collapse pattern. The legitimate replaces
        # (first-delta latch, tool-call strip) reset the flag to False.
        import re
        # Region: from the v3 loop start marker to just past the loop.
        start = v3_source.find("for iteration in range(_v3_max_iterations)")
        assert start != -1, "v3 loop not found in agents.py source"
        region = v3_source[start:]
        # Find every content_replace frame, then scan the following ~600
        # chars for a `_nudge_replacement_pending = True` (nudge signature)
        # vs `= False` (legit latch reset).
        bad = []
        for m in re.finditer(r'json\.dumps\(\{"type": "content_replace"', region):
            tail = region[m.end():m.end() + 900]
            if re.search(r"_nudge_replacement_pending = True", tail):
                bad.append(m.start())
        assert not bad, (
            f"{len(bad)} nudge block(s) still emit a bare content_replace "
            f"(visible collapse) at source offsets {bad[:5]}"
        )
