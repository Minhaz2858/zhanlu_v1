"""cad_bom_generate tool (roadmap P4)."""
import pytest

from app.services.tool_handlers import cad_bom as cb

ARGS = {
    "part_name": "Enclosure",
    "process": "cnc",
    "components": [
        {"name": "Body", "quantity": 1, "material": "6061-t6", "mass_g": 400.0},
        {"name": "Lid", "quantity": 1, "material": "6061-t6", "mass_g": 100.0},
        {"name": "M6 bolt", "quantity": 4, "material": "304-stainless", "mass_g": 8.0},
    ],
}


@pytest.fixture
def saved(monkeypatch):
    calls = {}

    def _fake_save(db, user_id, name, file_type, data):
        calls.update(name=name, file_type=file_type, data=data)
        return "url", len(data)

    monkeypatch.setattr(cb, "_save_file", _fake_save)
    return calls


def test_components_required():
    out = cb._cad_bom_generate({"part_name": "x", "process": "cnc"}, None, "u")
    assert not out["success"] and "components" in out["error"]


def test_bom_ok_saves_html(saved):
    out = cb._cad_bom_generate(ARGS, None, "u")
    assert out["success"] is True, out
    assert out["bom"]["totals"]["line_count"] == 3
    assert out["bom"]["totals"]["total_quantity"] == 6
    assert saved["file_type"] == "html"
    html = saved["data"].decode()
    assert "Body" in html and "Manufacturing notes" in html


def test_dfm_repairs_appear_in_notes(saved):
    out = cb._cad_bom_generate(
        {**ARGS, "dfm_result": {"verdict": "pass_with_warnings", "issues": [
            {"rule": "cnc_pocket_depth_ratio", "severity": "warning",
             "measured": "3.5x", "required": "<= 3x", "reason": "deep pocket",
             "repair": None}]}},
        None, "u")
    assert out["success"] is True
    assert "cnc_pocket_depth_ratio" in saved["data"].decode()


def test_registered_in_tool_registry():
    import app.services.tool_handlers.cad_bom  # noqa: F401
    from app.services.tool_registry import registry
    assert "cad_bom_generate" in registry.list_names()
