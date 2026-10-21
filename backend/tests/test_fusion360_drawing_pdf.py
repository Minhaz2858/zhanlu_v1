"""fusion360_make_drawing format=pdf — roadmap P0 (quote packages need
embeddable drawings; DXF alone is not presentable). Default stays DXF;
format='pdf' renders the same spec via ezdxf's drawing add-on (matplotlib).
"""
import pytest

from app.services.tool_handlers import fusion360_io as fio

SPEC = {
    "title": "Test Plate",
    "units": "mm",
    "views": [
        {"name": "TOP", "shapes": [{"type": "rect", "x": 0, "y": 0, "w": 60, "h": 40}],
         "dims": [{"type": "linear", "p1": [0, 0], "p2": [60, 0], "label": "60", "offset": 8}]},
    ],
    "dims": [],
}


@pytest.fixture
def saved(monkeypatch):
    """Capture _save_file calls instead of touching disk/db."""
    calls = {}

    def _fake_save(db, user_id, name, file_type, data):
        calls.update(name=name, file_type=file_type, data=data)
        return "url", len(data)

    monkeypatch.setattr(fio, "_save_file", _fake_save)
    return calls


def test_default_format_is_dxf(saved):
    out = fio._fusion360_make_drawing({"spec": SPEC}, None, "u")
    assert out["success"] is True
    assert saved["file_type"] == "dxf"
    assert saved["name"].endswith(".dxf")
    assert saved["data"].lstrip().startswith(b"0")  # ASCII DXF: "0\nSECTION"


def test_pdf_format_renders_pdf_bytes(saved):
    out = fio._fusion360_make_drawing({"spec": SPEC, "format": "pdf"}, None, "u")
    assert out["success"] is True, out
    assert saved["file_type"] == "pdf"
    assert saved["name"].endswith(".pdf")
    assert saved["data"][:4] == b"%PDF"
    assert out["format"] == "pdf"


def test_invalid_format_rejected(saved):
    out = fio._fusion360_make_drawing({"spec": SPEC, "format": "png"}, None, "u")
    assert not out["success"] and "format" in out["error"]
