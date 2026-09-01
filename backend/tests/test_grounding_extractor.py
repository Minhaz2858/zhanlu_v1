"""Tests for the deterministic grounding extractor (Phase 1, G1).

The extractor distills each observation type into a compact metadata+data
block so the FINALIZE response generator writes grounded, specific replies.
Pure & deterministic — no LLM calls.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.services.synexia.grounding_extractor import extract_grounding


def _obs(**kw):
    """Build a minimal ObservationRecord-like object."""
    base = dict(seq=1, observation_type="tool_call", tool_name=None,
                success=True, result_data=None, result_text=None,
                error_message=None, artifact_ids=None)
    base.update(kw)
    return SimpleNamespace(**base)


class TestNl2sqlExtraction:
    def test_nl2sql_includes_sql_rows_columns_and_aggregates(self):
        obs = _obs(
            seq=2, observation_type="nl2sql", tool_name="nl2sql",
            result_data={
                "sql": "SELECT material, revenue FROM sales WHERE q='Q2'",
                "data": [
                    {"material": "Steel", "revenue": 120},
                    {"material": "Aluminium", "revenue": 60},
                    {"material": "Copper", "revenue": 30},
                    {"material": "Zinc", "revenue": 9},  # beyond top-3
                ],
            },
        )
        out = extract_grounding([obs], max_chars=10000)
        assert "#2" in out and "nl2sql" in out
        assert "SELECT material, revenue" in out           # SQL present
        assert "rows: 4" in out                            # row count
        assert "Steel" in out and "Aluminium" in out       # top-3 rows
        assert "Zinc" not in out                           # 4th row dropped (top-3)
        # aggregate stats on the numeric column
        assert "revenue: sum=219" in out
        assert "min=9" in out and "max=120" in out

    def test_nl2sql_truncates_long_sql(self):
        long_sql = "SELECT " + ", ".join(f"c{i}" for i in range(200))
        obs = _obs(observation_type="nl2sql", result_data={"sql": long_sql, "data": []})
        out = extract_grounding([obs], max_chars=10000)
        # SQL capped at 400 chars (the _SQL_MAX_CHARS constant)
        sql_line = [ln for ln in out.splitlines() if ln.strip().startswith("sql:")][0]
        assert len(sql_line) <= 400 + len("  sql: ")


class TestSynthesizeExtraction:
    def test_synthesize_includes_kpis_insights_chart(self):
        obs = _obs(
            observation_type="synthesize",
            result_data={
                "title": "Q2 Sales Report",
                "kpis": [{"label": "Revenue", "value": "189M CNY"}],
                "chart": {"type": "bar", "title": "Revenue by material"},
                "insights": [{"icon": "trending-up", "text": "Steel leads at 55%"}],
            },
        )
        out = extract_grounding([obs], max_chars=10000)
        assert "Q2 Sales Report" in out
        assert "kpi Revenue: 189M CNY" in out
        assert "chart: Revenue by material" in out
        assert "Steel leads at 55%" in out


class TestSandboxExtraction:
    def test_sandbox_includes_artifact_meta(self):
        obs = _obs(
            observation_type="sandbox", artifact_ids=["abc123"],
            result_data={"artifact_type": "docx", "title": "Q2 Report.docx"},
        )
        out = extract_grounding([obs], max_chars=10000)
        assert "artifact: docx" in out
        assert "Q2 Report.docx" in out
        assert "abc123" in out


class TestToolExtraction:
    def test_tool_truncates_result_text(self):
        obs = _obs(observation_type="tool_call", tool_name="web_search",
                   result_text="x" * 1000)
        out = extract_grounding([obs], max_chars=10000)
        # result capped at 500 chars (_TOOL_RESULT_MAX_CHARS)
        result_line = [ln for ln in out.splitlines() if "result:" in ln][0]
        assert len(result_line) <= 500 + len("  result: ")

    def test_tool_falls_back_to_result_data(self):
        obs = _obs(observation_type="tool_call", result_text=None,
                   result_data={"summary": "found 3 records"})
        out = extract_grounding([obs], max_chars=10000)
        assert "found 3 records" in out


class TestCharCapAndOrdering:
    def test_empty_observations_returns_no_actions(self):
        assert extract_grounding([]) == "No actions taken."
        assert extract_grounding(None) == "No actions taken."

    def test_metadata_preserved_over_data_under_cap(self):
        # Two nl2sql obs; cap tiny so data rows must drop but metadata stays.
        obs = _obs(
            seq=1, observation_type="nl2sql",
            result_data={"sql": "SELECT 1", "data": [{"a": "ROWDATA"}] * 5},
        )
        out = extract_grounding([obs], max_chars=120)
        # metadata survives
        assert "sql: SELECT 1" in out
        assert "rows: 5" in out
        # at least some data dropped to fit the cap
        assert out.count("ROWDATA") < 5

    def test_failed_observation_shows_error(self):
        obs = _obs(success=False, error_message="connection refused")
        out = extract_grounding([obs], max_chars=10000)
        assert "FAILED" in out
        assert "connection refused" in out


class TestDefensiveReads:
    def test_partial_observation_does_not_raise(self):
        # No result_data, no result_text, no tool_name — must not raise.
        obs = SimpleNamespace(seq=1, observation_type="tool_call")
        out = extract_grounding([obs], max_chars=10000)
        assert "#1" in out  # header still rendered


class TestDefaultMaxCharsFromSettings:
    def test_uses_settings_default_when_max_chars_omitted(self, monkeypatch):
        monkeypatch.setattr(
            "app.config.settings.SYNEXIA_GROUNDING_MAX_CHARS", 80
        )
        obs = _obs(
            observation_type="nl2sql",
            result_data={"sql": "SELECT 1", "data": [{"a": "ROW"}] * 10},
        )
        out = extract_grounding([obs])  # no max_chars → reads settings
        assert len(out) <= 80
        # metadata still preserved under the tiny cap
        assert "sql: SELECT 1" in out
