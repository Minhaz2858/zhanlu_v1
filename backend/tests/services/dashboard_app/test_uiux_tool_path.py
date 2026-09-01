"""Tests for the ui-ux-pro-max tool CLI path resolution + builtin fallback."""

import importlib
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.tool_handlers import ui_ux_pro_max_tool as mod

SKILL_DIR = Path(mod.__file__).resolve().parents[3] / "skills" / "ui-ux-pro-max"


def test_search_py_resolves_to_real_cli():
    """The tool must point at the real search.py shipped with the skill."""
    assert mod._SEARCH_PY.is_file(), f"search.py missing at {mod._SEARCH_PY}"
    assert mod._SEARCH_PY.name == "search.py"
    # The canonical location (not the stale nested src/ui-ux-pro-max/ path).
    assert mod._SEARCH_PY == SKILL_DIR / "scripts" / "search.py"


def test_build_cli_argv_does_not_raise():
    cmd = mod._build_cli_argv(["sales dashboard", "--density", "8"], design_system=True)
    assert cmd[0] == "python" or cmd[0].endswith("python") or "python" in cmd[0]
    assert str(mod._SEARCH_PY) in cmd
    assert "--design-system" in cmd


@patch.object(mod, "_SEARCH_PY", SKILL_DIR / "does-not-exist.py")
def test_build_cli_argv_raises_when_missing():
    with pytest.raises(FileNotFoundError):
        mod._build_cli_argv(["q"])


def test_builtin_fallback_design_system_has_structured_tokens():
    result = mod._builtin_fallback("ERP dashboard", design_system=True)
    assert result["success"] is True
    assert result["fallback_used"] is True
    structured = result["structured"]
    assert structured["colors"]["primary"].startswith("#")
    assert len(structured["colors"]["chart_palette"]) == 6
    assert structured["typography"]["heading"]
    assert structured["spacing_scale"]["md"]


def test_builtin_fallback_search_has_no_structured():
    result = mod._builtin_fallback("colors", domain="color", design_system=False)
    assert result["success"] is True
    assert "structured" not in result


def test_persist_design_system_writes_json_sidecar(tmp_path):
    from app.config import settings

    original = settings.GENERATED_DIR
    settings.GENERATED_DIR = str(tmp_path / "generated")
    try:
        result = {
            "success": True,
            "result": "# Design System\n\n- token guidance",
            "structured": {
                "colors": {"primary": "#2563eb", "chart_palette": ["#2563eb"]},
                "typography": {"heading": "Inter", "body": "Inter"},
                "spacing_scale": {"md": "8px"},
            },
        }
        args = {"persist": True, "output_dir": "design-system", "org_id": "org-1"}
        out = mod._persist_design_system(result, args, "Proj")
        assert (tmp_path / "generated" / "design-system" / "org-1" / "MASTER.md").exists()
        assert (tmp_path / "generated" / "design-system" / "org-1" / "design-system.json").exists()
        assert out["design_system_ref"] == "design-system/org-1/MASTER.md"
        assert out["design_system_json_ref"] == "design-system/org-1/design-system.json"
    finally:
        settings.GENERATED_DIR = original


def test_persist_design_system_noop_without_persist(tmp_path):
    result = {"success": True, "result": "text"}
    out = mod._persist_design_system(result, {"persist": False}, None)
    assert "design_system_ref" not in out
