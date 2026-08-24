"""forecast_what_if chat tool — scenario simulation for forecast products."""
import asyncio

from app.services.tool_handlers import forecast_tool


class FakeTarget:
    def __init__(self, product_key="ecisco.dcpd"):
        self.product_key = product_key
        self.report_order = 0


class FakeQuery:
    def __init__(self, target):
        self._target = target

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def first(self):
        return self._target


class FakeDB:
    def __init__(self, target):
        self._target = target

    def query(self, model):
        return FakeQuery(self._target)


SEVEN_POINTS = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0]


def _canned_what_if(product_key="ecisco.dcpd"):
    return {
        "product_key": product_key,
        "base_forecast": list(SEVEN_POINTS),
        "adjusted_forecast": [round(v * 1.05, 2) for v in SEVEN_POINTS],
        "total_impact_pct": 5.0,
        "adjustments": [
            {"driver": "brent", "delta_pct": 5.0, "impact_pct": 3.0,
             "description": "Brent +5.0% -> naphtha +4.0% -> +3.00% impact"},
            {"driver": "naphtha", "delta_pct": 2.0, "impact_pct": 2.0,
             "description": "Naphtha +2.0% -> +2.00% impact"},
        ],
    }


def test_what_if_happy_path(monkeypatch):
    """Success: drivers normalized, horizon_table 7 rows, narration_hint present."""
    monkeypatch.setattr(forecast_tool, "_resolve_org_context",
                        lambda context: ("default-org", "default-app"))
    monkeypatch.setattr(forecast_tool, "compute_what_if",
                        lambda pk, brent, naphtha, db: _canned_what_if(pk))
    out = asyncio.run(forecast_tool._forecast_what_if(
        {"product_id": "dcpd", "brent_delta_pct": 5.0, "naphtha_delta_pct": 2.0},
        db=FakeDB(FakeTarget()), user_id=None, context=None))
    assert out["success"] is True
    assert out["product_key"] == "ecisco.dcpd"
    assert len(out["horizon_table"]) == 7
    assert out["horizon_table"][0]["day"] == 1
    assert out["horizon_table"][6]["day"] == 7
    assert out["horizon_table"][6]["delta_pct"] is not None
    assert len(out["drivers"]) == 2
    assert {d["driver"] for d in out["drivers"]} == {"brent", "naphtha"}
    assert out["narration_hint"]


def test_what_if_both_deltas_zero():
    out = asyncio.run(forecast_tool._forecast_what_if(
        {"product_id": "dcpd", "brent_delta_pct": 0, "naphtha_delta_pct": 0},
        db=None, user_id=None, context=None))
    assert out["success"] is False
    assert "specify at least one price shock" in out["error"]


def test_what_if_unknown_product():
    out = asyncio.run(forecast_tool._forecast_what_if(
        {"product_id": "nope"}, db=FakeDB(None), user_id=None, context=None))
    assert out["success"] is False
    assert "no forecast target for 'nope'" in out["error"]


def test_what_if_empty_product_id():
    out = asyncio.run(forecast_tool._forecast_what_if(
        {"product_id": ""}, db=None, user_id=None, context=None))
    assert out["success"] is False
    assert "no forecast target for" in out["error"]


def test_what_if_no_forecast_data(monkeypatch):
    monkeypatch.setattr(forecast_tool, "_resolve_org_context",
                        lambda context: ("default-org", "default-app"))
    monkeypatch.setattr(forecast_tool, "compute_what_if",
                        lambda pk, brent, naphtha, db: {
                            "product_key": pk, "base_forecast": [],
                            "adjusted_forecast": [], "total_impact_pct": 0.0,
                            "adjustments": []})
    out = asyncio.run(forecast_tool._forecast_what_if(
        {"product_id": "dcpd", "brent_delta_pct": 5.0},
        db=FakeDB(FakeTarget()), user_id=None, context=None))
    assert out["success"] is False
    assert "No forecast available for simulation" in out["error"]


def test_what_if_simulation_exception(monkeypatch):
    monkeypatch.setattr(forecast_tool, "_resolve_org_context",
                        lambda context: ("default-org", "default-app"))
    def boom(pk, brent, naphtha, db):
        raise RuntimeError("boom")
    monkeypatch.setattr(forecast_tool, "compute_what_if", boom)
    out = asyncio.run(forecast_tool._forecast_what_if(
        {"product_id": "dcpd", "brent_delta_pct": 5.0},
        db=FakeDB(FakeTarget()), user_id=None, context=None))
    assert out["success"] is False
    assert "simulation failed" in out["error"]


def test_forecast_what_if_registered():
    from app.services.tool_registry import registry
    assert registry.get_handler("forecast_what_if") is not None
