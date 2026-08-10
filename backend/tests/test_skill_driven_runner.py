"""Unit tests for the skill-driven runner's orchestration logic.

We mock out the LLM client and the subprocess execution so the tests
can verify the runner's decision logic (planning → code-gen → exec →
retry → fallback) without actually invoking Docker or a real LLM.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import importlib
import app.services.sandbox.skill_driven_runner as runner_mod


@pytest.fixture(autouse=True)
def _isolate_filesystem(monkeypatch, tmp_path):
    """Make the runner's hardcoded /input, /output, /tmp paths point to
    a temp dir so we don't accidentally write to the real filesystem."""
    monkeypatch.setattr(runner_mod, "INPUT_DIR", tmp_path / "input")
    monkeypatch.setattr(runner_mod, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(runner_mod, "SKILL_BUNDLE_DIR", tmp_path / "input" / "skill_bundle")
    monkeypatch.setattr(runner_mod, "DATA_DIR", tmp_path / "input" / "data")
    monkeypatch.setattr(runner_mod, "GEN_DIR", tmp_path / "gen")
    monkeypatch.setattr(runner_mod, "CONFIG_PATH", tmp_path / "input" / "config.json")
    for d in (runner_mod.INPUT_DIR, runner_mod.OUTPUT_DIR,
              runner_mod.SKILL_BUNDLE_DIR, runner_mod.DATA_DIR, runner_mod.GEN_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ── _compact_data_for_prompt ─────────────────────────────────────────────


def test_compact_data_for_prompt_handles_empty():
    assert runner_mod._compact_data_for_prompt([]) == "(no data rows)"


def test_compact_data_for_prompt_caps_rows():
    """Should only return at most max_rows in the sample."""
    rows = [{"x": i} for i in range(100)]
    text = runner_mod._compact_data_for_prompt(rows, max_rows=5)
    # 5 rows = 6 lines (1 header + 5 data) plus a "more rows" note
    line_count = text.count("\n") + 1
    assert line_count <= 8  # 6 + a couple for the suffix


def test_compact_data_for_prompt_includes_more_rows_note():
    """When the data exceeds the sample cap, a suffix should be appended."""
    rows = [{"x": i} for i in range(100)]
    text = runner_mod._compact_data_for_prompt(rows, max_rows=3)
    assert "more rows" in text


# ── _load_config / _load_data / _load_skill_bundle ────────────────────────


def test_load_config_returns_parsed_dict():
    """The loader should parse /input/config.json into a dict."""
    runner_mod.CONFIG_PATH.write_text(json.dumps({"format": "docx", "title": "T"}))
    cfg = runner_mod._load_config()
    assert cfg == {"format": "docx", "title": "T"}


def test_load_config_raises_on_missing():
    """A missing config.json should raise FileNotFoundError."""
    if runner_mod.CONFIG_PATH.exists():
        runner_mod.CONFIG_PATH.unlink()
    with pytest.raises(FileNotFoundError):
        runner_mod._load_config()


def test_load_data_handles_no_data_dir():
    """An absent /input/data directory returns an empty list."""
    if runner_mod.DATA_DIR.exists():
        for f in runner_mod.DATA_DIR.iterdir():
            f.unlink()
        runner_mod.DATA_DIR.rmdir()
    rows = runner_mod._load_data()
    assert rows == []


def test_load_data_parses_multiple_files():
    """Each *.json under /input/data contributes rows to the list."""
    (runner_mod.DATA_DIR / "a.json").write_text(json.dumps([{"x": 1}, {"x": 2}]))
    (runner_mod.DATA_DIR / "b.json").write_text(json.dumps([{"y": 3}]))
    rows = runner_mod._load_data()
    assert len(rows) == 3


def test_load_skill_bundle_loads_skill_md():
    """A SKILL.md in the bundle should appear in the loaded dict."""
    (runner_mod.SKILL_BUNDLE_DIR / "SKILL.md").write_text("# SKILL\nworkflow...")
    bundle = runner_mod._load_skill_bundle("docx")
    assert "SKILL.md" in bundle
    assert "workflow" in bundle["SKILL.md"]


def test_load_skill_bundle_loads_format_companion_for_docx():
    """docx bundles should include docx-js.md when present."""
    (runner_mod.SKILL_BUNDLE_DIR / "SKILL.md").write_text("# SKILL")
    (runner_mod.SKILL_BUNDLE_DIR / "docx-js.md").write_text("# docx-js reference")
    bundle = runner_mod._load_skill_bundle("docx")
    assert "docx-js.md" in bundle


def test_load_skill_bundle_handles_missing_directory():
    """A missing bundle dir should return empty dict, not crash."""
    runner_mod.SKILL_BUNDLE_DIR.rmdir()
    bundle = runner_mod._load_skill_bundle("docx")
    assert bundle == {}


# ── _run_fallback ────────────────────────────────────────────────────────


def test_run_fallback_docx_produces_file():
    """Calling the fallback for docx should write a file to OUTPUT_DIR.

    We use the REAL generate_docx_fallback (not a mock) so this also
    exercises the actual python-docx code path end-to-end.  This is
    intentionally an integration-flavored unit test.
    """
    from app.services.sandbox.fallback_generator import generate_docx_fallback
    cfg = {"title": "Test Doc", "summary": "Hello", "kpis": [], "key_findings": [], "recommendations": [], "sections": [], "insights": []}
    ok = runner_mod._run_fallback("docx", cfg, [{"a": 1}], "test reason")
    assert ok is True
    out = runner_mod.OUTPUT_DIR / "report.docx"
    assert out.exists()
    assert out.stat().st_size > 1000


def test_run_fallback_returns_false_when_file_missing():
    """If the fallback function doesn't write a file, return False."""
    def fake_fallback(*, output_path, config, data):
        pass  # intentionally do nothing
    with patch.object(runner_mod, "FORMAT_SPEC", {
        "docx": {"lang": "py", "ext": "docx", "fallback": fake_fallback},
    }):
        ok = runner_mod._run_fallback("docx", {"title": "X"}, [], "x")
    assert ok is False


# ── _emit_manifest ────────────────────────────────────────────────────────


def test_emit_manifest_writes_json():
    """The manifest writer should produce a valid JSON file at OUTPUT_DIR/build_manifest.json."""
    runner_mod._emit_manifest("docx", "skill_driven", {"sections": []}, True, None)
    manifest_path = runner_mod.OUTPUT_DIR / "build_manifest.json"
    assert manifest_path.exists()
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert data["format"] == "docx"
    assert data["mode"] == "skill_driven"
    assert data["ok"] is True


# ── main() orchestration (mocked LLM + execution) ───────────────────────


def test_main_routes_html_to_deterministic_path():
    """format=html should skip the LLM and call generate_html_utility directly."""
    runner_mod.CONFIG_PATH.write_text(json.dumps({
        "format": "html", "title": "Quick HTML", "summary": "Hi",
        "kpis": [], "key_findings": [], "recommendations": [], "sections": [],
        "instructions": "", "row_count": 0,
    }))
    (runner_mod.DATA_DIR / "q.json").write_text(json.dumps([{"a": 1}]))
    # generate_html_utility will be called via _run_fallback because
    # FORMAT_SPEC["html"] maps to it.
    rc = runner_mod.main()
    assert rc == 0
    manifest = json.loads((runner_mod.OUTPUT_DIR / "build_manifest.json").read_text())
    assert manifest["format"] == "html"


def test_main_rejects_unknown_format():
    """An unsupported format should exit non-zero and emit a manifest."""
    runner_mod.CONFIG_PATH.write_text(json.dumps({"format": "xyz", "title": "T"}))
    rc = runner_mod.main()
    assert rc == 1
    manifest = json.loads((runner_mod.OUTPUT_DIR / "build_manifest.json").read_text())
    assert manifest["ok"] is False


# ── LLM planning call (mocked client) ────────────────────────────────────


def test_call_llm_planning_parses_valid_json():
    """A planning call returning clean JSON should produce a usable plan."""
    fake_client = MagicMock()
    fake_client.chat.return_value = json.dumps({
        "document_type": "competitive_analysis",
        "sections": [
            {"title": "Market Landscape", "purpose": "Set context",
             "content_source": "synthesis", "priority": "high"},
        ],
        "design": {"tone": "professional"},
    })
    plan = runner_mod._call_llm_planning(
        fake_client,
        skill_md="# SKILL",
        user_message="Analyze competitors",
        title="Competitor Analysis",
        data_sample="region, revenue\nNA, 100",
        synthesized_payload={"summary": "Brief", "key_findings": [], "sections": [], "kpis": []},
    )
    assert plan is not None
    assert plan["document_type"] == "competitive_analysis"
    assert len(plan["sections"]) == 1


def test_call_llm_planning_returns_none_on_proxy_error():
    """A proxy failure should surface as None, not crash."""
    fake_client = MagicMock()
    fake_client.chat.side_effect = ConnectionRefusedError("no socket")
    plan = runner_mod._call_llm_planning(
        fake_client,
        skill_md="# SKILL",
        user_message="x",
        title="T",
        data_sample="",
        synthesized_payload={},
    )
    assert plan is None


def test_call_llm_planning_returns_none_on_invalid_json():
    """Non-JSON output should be discarded, not crash."""
    fake_client = MagicMock()
    fake_client.chat.return_value = "Sorry, I can't help with that."
    plan = runner_mod._call_llm_planning(
        fake_client,
        skill_md="# SKILL",
        user_message="x",
        title="T",
        data_sample="",
        synthesized_payload={},
    )
    assert plan is None


def test_call_llm_planning_strips_markdown_fences():
    """An LLM wrapping its JSON in ```json ... ``` should still parse."""
    fake_client = MagicMock()
    fake_client.chat.return_value = (
        "```json\n"
        + json.dumps({"document_type": "t", "sections": [{"title": "S"}]})
        + "\n```"
    )
    plan = runner_mod._call_llm_planning(
        fake_client,
        skill_md="# SKILL",
        user_message="x",
        title="T",
        data_sample="",
        synthesized_payload={},
    )
    assert plan is not None
    assert plan["document_type"] == "t"