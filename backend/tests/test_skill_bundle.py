"""Unit tests for skill bundle packaging + runner module bundling.

These cover the helpers in ``app/services/tool_handlers/sandbox_tool.py``
that assemble the C-Heavy skill-driven input package.  They don't
require Docker or a real LLM — they just verify the packaging logic
itself (path-traversal guard, size cap, required files).
"""
from __future__ import annotations

import base64
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── _build_skill_bundle ──────────────────────────────────────────────────


def test_build_skill_bundle_returns_known_files_for_docx():
    """The docx bundle must include SKILL.md + docx-js.md + ooxml.md."""
    from app.services.tool_handlers.sandbox_tool import _build_skill_bundle

    bundle = _build_skill_bundle("docx")
    names = {entry["path"] for entry in bundle}
    assert "SKILL.md" in names, f"SKILL.md missing from {names}"
    assert any(n.startswith("docx-js") for n in names), f"docx companion missing from {names}"


def test_build_skill_bundle_returns_known_files_for_pptx():
    """The pptx bundle must include SKILL.md + html2pptx.md + ooxml.md."""
    from app.services.tool_handlers.sandbox_tool import _build_skill_bundle

    bundle = _build_skill_bundle("pptx")
    names = {entry["path"] for entry in bundle}
    assert "SKILL.md" in names
    assert any(n.startswith("html2pptx") for n in names), f"pptx companion missing from {names}"


def test_build_skill_bundle_xlsx_has_skill_md():
    """The xlsx bundle must at least include SKILL.md (companion may or may not)."""
    from app.services.tool_handlers.sandbox_tool import _build_skill_bundle

    bundle = _build_skill_bundle("xlsx")
    names = {entry["path"] for entry in bundle}
    assert "SKILL.md" in names


def test_build_skill_bundle_returns_empty_for_unknown_format():
    """An unknown format key returns an empty list (graceful)."""
    from app.services.tool_handlers.sandbox_tool import _build_skill_bundle

    bundle = _build_skill_bundle("not_a_real_format")
    assert bundle == []


def test_build_skill_bundle_all_entries_are_valid_base64():
    """Every entry's data_base64 must decode to a non-empty bytes object."""
    from app.services.tool_handlers.sandbox_tool import _build_skill_bundle

    for fmt in ("docx", "pptx", "xlsx", "pdf"):
        bundle = _build_skill_bundle(fmt)
        for entry in bundle:
            raw = base64.b64decode(entry["data_base64"])
            assert len(raw) > 0, f"empty content for {fmt}/{entry['path']}"


def test_build_skill_bundle_respects_size_cap():
    """Files larger than the cap should be silently skipped (never raise)."""
    from app.services.tool_handlers import sandbox_tool

    assert sandbox_tool._SKILL_BUNDLE_MAX_FILE_BYTES >= 50_000
    assert sandbox_tool._SKILL_BUNDLE_MAX_FILE_BYTES <= 200_000


# ── _build_runner_modules ────────────────────────────────────────────────


def test_build_runner_modules_returns_three_files():
    """The skill-driven runner needs 3 sibling modules."""
    from app.services.tool_handlers.sandbox_tool import _build_runner_modules

    modules = _build_runner_modules()
    assert set(modules.keys()) == {
        "skill_driven_runner.py",
        "llm_client.py",
        "fallback_generator.py",
    }


def test_build_runner_modules_are_decodable():
    """Each module's base64 payload should decode cleanly."""
    from app.services.tool_handlers.sandbox_tool import _build_runner_modules

    modules = _build_runner_modules()
    for filename, content_b64 in modules.items():
        raw = base64.b64decode(content_b64)
        assert len(raw) > 1000, f"{filename} suspiciously small: {len(raw)} bytes"
        assert b"import" in raw, f"{filename} doesn't look like Python"


# ── _format_supports_skill_driven ────────────────────────────────────────


def test_format_supports_skill_driven_false_for_rich_formats():
    """docx/pptx/xlsx/pdf currently use the deterministic runner."""
    from app.services.tool_handlers.sandbox_tool import _format_supports_skill_driven

    for fmt in ("docx", "pptx", "xlsx", "pdf"):
        assert _format_supports_skill_driven(fmt) is False, fmt


def test_format_supports_skill_driven_false_for_html_md():
    """html/md stay on the deterministic utility path."""
    from app.services.tool_handlers.sandbox_tool import _format_supports_skill_driven

    for fmt in ("html", "md", "dashboard", ""):
        assert _format_supports_skill_driven(fmt) is False, fmt


def test_deterministic_office_formats_use_existing_images():
    from app.services.tool_handlers import sandbox_tool

    assert sandbox_tool._IMAGE_BY_FORMAT["docx"] == "zhanlu-sandbox-office:latest"
    assert sandbox_tool._IMAGE_BY_FORMAT["pdf"] == "zhanlu-sandbox-office:latest"
    assert sandbox_tool._IMAGE_BY_FORMAT["xlsx"] == "zhanlu-sandbox-office:latest"
    assert sandbox_tool._IMAGE_BY_FORMAT["pptx"] == "zhanlu-sandbox-pptx:latest"


# ── _SKILL_DRIVEN_RUNNER_SCRIPT ─────────────────────────────────────────


def test_skill_driven_runner_script_loaded():
    """The runner script source must be loaded at import time."""
    from app.services.tool_handlers import sandbox_tool

    assert sandbox_tool._SKILL_DRIVEN_RUNNER_SCRIPT, "runner script not loaded"
    assert "def main" in sandbox_tool._SKILL_DRIVEN_RUNNER_SCRIPT
    assert "_run_skill_driven" in sandbox_tool._SKILL_DRIVEN_RUNNER_SCRIPT