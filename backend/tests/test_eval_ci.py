"""Tests for the eval CI runner (app.services.synexia.eval_ci)."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.synexia import eval_ci
from app.services.synexia.eval_harness import HarnessReport, ScenarioResult


# ── CLI parser ───────────────────────────────────────────────────────────

class TestParser:
    def test_defaults(self):
        args = eval_ci.build_parser().parse_args([])
        assert args.base_url == eval_ci.DEFAULT_BASE_URL
        assert args.scenarios is None
        assert args.report is None
        assert args.timeout == 120.0

    def test_custom_args(self):
        args = eval_ci.build_parser().parse_args([
            "--base-url", "http://x:1/api",
            "--scenarios", "s.json",
            "--report", "r.json",
            "--timeout", "30",
        ])
        assert args.base_url == "http://x:1/api"
        assert args.scenarios == "s.json"
        assert args.report == "r.json"
        assert args.timeout == 30.0


# ── HTTP run_fn ──────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestMakeHttpRunFn:
    def test_posts_to_executions_endpoint(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode("utf-8"))
            captured["timeout"] = timeout
            return _FakeResponse({"assistant_content": "ok", "confidence": 0.9})

        with patch("urllib.request.urlopen", fake_urlopen):
            run_fn = eval_ci.make_http_run_fn("http://h:9/api/", timeout=7)
            out = asyncio.run(run_fn({
                "user_message": "hello",
                "agent_name": "general_assistant",
                "conversation_id": "c1",
            }))

        assert captured["url"] == "http://h:9/api/executions"
        assert captured["body"]["user_message"] == "hello"
        assert captured["body"]["conversation_id"] == "c1"
        assert captured["timeout"] == 7
        assert out["confidence"] == 0.9

    def test_omits_conversation_id_when_absent(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResponse({})

        with patch("urllib.request.urlopen", fake_urlopen):
            run_fn = eval_ci.make_http_run_fn("http://h:9/api")
            asyncio.run(run_fn({"user_message": "hi"}))

        assert "conversation_id" not in captured["body"]
        assert captured["body"]["agent_name"] == "general_assistant"


# ── run_ci / main ────────────────────────────────────────────────────────

class TestRunCi:
    def test_all_builtin_scenarios_pass_with_clean_backend(self):
        """A stub run_fn returning a clean output passes all builtins."""
        async def clean_run_fn(scenario):
            return {
                "assistant_content": "Sales_Report is ready.",
                "artifact_ids": ["a1"],
                "confidence": 0.9,
                "quality_gate": {"passed": True},
                "quality_eval": {
                    "verdict": "accept",
                    "completeness_score": 0.9,
                    "is_ok": True,
                },
            }

        with patch.object(eval_ci, "make_http_run_fn", lambda *a, **k: clean_run_fn):
            report = asyncio.run(eval_ci.run_ci(base_url="http://unused"))

        assert report.total == len(eval_ci.load_all_scenarios())
        assert report.is_ok, [
            (s.name, [g.detail for g in s.graders if not g.passed], s.error)
            for s in report.scenarios if not s.passed
        ]

    def test_failing_backend_marks_scenarios_failed(self):
        async def bad_run_fn(scenario):
            return {
                "assistant_content": "Failed to load artifact: HTTP 404",
                "artifact_ids": [],
                "confidence": 0.1,
            }

        with patch.object(eval_ci, "make_http_run_fn", lambda *a, **k: bad_run_fn):
            report = asyncio.run(eval_ci.run_ci(base_url="http://unused"))

        assert not report.is_ok
        assert report.failed > 0


class TestMain:
    def _report(self, passed: bool) -> HarnessReport:
        sc = ScenarioResult(name="s1", passed=passed, duration_ms=1.0)
        return HarnessReport(
            passed=1 if passed else 0,
            failed=0 if passed else 1,
            total=1,
            scenarios=[sc],
            duration_ms=1.0,
        )

    def test_exit_zero_on_success(self, capsys):
        with patch.object(
            eval_ci, "run_ci", AsyncMock(return_value=self._report(True)),
        ):
            code = eval_ci.main([])
        assert code == 0
        assert "1/1 scenarios passed" in capsys.readouterr().out

    def test_exit_one_on_failure(self, capsys):
        with patch.object(
            eval_ci, "run_ci", AsyncMock(return_value=self._report(False)),
        ):
            code = eval_ci.main([])
        assert code == 1
        assert "0/1 scenarios passed" in capsys.readouterr().out

    def test_report_file_written(self, tmp_path):
        out = tmp_path / "report.json"
        with patch.object(
            eval_ci, "run_ci", AsyncMock(return_value=self._report(True)),
        ):
            code = eval_ci.main(["--report", str(out)])
        assert code == 0
        data = json.loads(out.read_text())
        assert data["passed"] == 1
        assert data["scenarios"][0]["name"] == "s1"

    def test_golden_scenarios_file_loads(self):
        """The shipped golden JSON parses and overrides/extends builtins."""
        import os
        path = os.path.join(
            os.path.dirname(__file__), "eval", "golden_scenarios.json",
        )
        scenarios = eval_ci.load_all_scenarios(user_file=path)
        names = {s["name"] for s in scenarios}
        assert "golden_sales_report_ships_clean" in names
        assert "golden_no_fabricated_success" in names
        # Builtins are still present.
        assert "sales_report_minimal" in names
