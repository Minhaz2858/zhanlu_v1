"""rfq_quote tool — roadmap P2 commercial wedge. Validator + delegator to
dfm.quote.compute_quote; emits an HTML quote_package (draft_pending_approval).
"""
import pytest

from app.services.tool_handlers import rfq_quote as rq

ARGS = {
    "part_name": "Enclosure",
    "material": "6061-t6",
    "process": "cnc",
    "quantity": 10,
    "mass_g": 500.0,
    "feature_counts": {"holes": 4},
}


@pytest.fixture
def saved(monkeypatch):
    calls = {}

    def _fake_save(db, user_id, name, file_type, data):
        calls.update(name=name, file_type=file_type, data=data)
        return "url", len(data)

    monkeypatch.setattr(rq, "_save_file", _fake_save)
    return calls


def test_process_required():
    out = rq._rfq_quote({"quantity": 10, "mass_g": 100}, None, "u")
    assert not out["success"] and "process" in out["error"]


def test_missing_mass_yields_clarifying_error():
    out = rq._rfq_quote({"process": "cnc", "quantity": 10}, None, "u")
    assert not out["success"]
    assert "fusion360_physical_properties" in out["error"]  # points at the fix


def test_quantity_required():
    out = rq._rfq_quote({"process": "cnc", "mass_g": 100}, None, "u")
    assert not out["success"] and "quantity" in out["error"]


def test_quote_ok_saves_html_package(saved):
    out = rq._rfq_quote(ARGS, None, "u")
    assert out["success"] is True, out
    assert out["quote"]["status"] == "draft_pending_approval"
    assert saved["file_type"] == "html"
    assert saved["name"].endswith(".html")
    html = saved["data"].decode()
    assert "Enclosure" in html
    assert "draft_pending_approval" in html
    assert "not a binding quote" in html.lower()
    assert out["filename"].endswith(".html")


def test_dfm_results_embedded_in_package(saved):
    dfm = {"verdict": "fail", "process": "cnc", "material": "6061-t6",
           "issues": [{"rule": "min_wall_thickness", "severity": "error",
                       "measured": "0.4mm", "required": "1.0mm",
                       "reason": "too thin", "repair": None, "auto_fixable": True}],
           "checked_rules": 4}
    out = rq._rfq_quote({**ARGS, "dfm_result": dfm}, None, "u")
    assert out["success"] is True
    assert "min_wall_thickness" in saved["data"].decode()
    assert "DFM" in saved["data"].decode()


def test_registered_in_tool_registry():
    import app.services.tool_handlers.rfq_quote  # noqa: F401
    from app.services.tool_registry import registry
    assert "rfq_quote" in registry.list_names()
    entry = registry.get_entry("rfq_quote")
    assert entry is not None and entry.handler is rq._rfq_quote
