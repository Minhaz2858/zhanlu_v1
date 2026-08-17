"""ForecastingAgentService — thin facade delegation + parity tests.

Verifies each facade method delegates to the *same* underlying implementation
as the pre-unification call path (zero frontend impact, no logic moved).
"""
from types import SimpleNamespace

import pytest

from app.services.forecasting.unified_service import ForecastingAgentService


class _FakeQuery:
    def __init__(self, rows):
        self._rows = list(rows)
        self._offset = 0
        self._limit = None
        self.count_called = False

    def filter(self, *a):
        return self

    def order_by(self, *a):
        return self

    def offset(self, n):
        self._offset = n
        return self

    def limit(self, n):
        self._limit = n
        return self

    def count(self):
        self.count_called = True
        return len(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        rows = self._rows[self._offset:]
        if self._limit is not None:
            rows = rows[: self._limit]
        return rows


class _FakeDB:
    def __init__(self, by_model=None):
        self._by_model = by_model or {}

    def query(self, model):
        name = getattr(model, "__name__", str(model))
        return _FakeQuery(self._by_model.get(name, []))


def _svc(db=None):
    return ForecastingAgentService(db or _FakeDB())


def test_engine_is_lazy_and_cached(monkeypatch):
    created = []

    class _FakeEngine:
        def __init__(self, db):
            created.append(db)

    monkeypatch.setattr(
        "app.services.forecasting.unified_service.ForecastEngine", _FakeEngine
    )
    svc = _svc()
    assert svc._engine is None
    e1 = svc.engine
    e2 = svc.engine
    assert e1 is e2 and len(created) == 1


def test_discover_products_delegates(monkeypatch):
    calls = {}

    class _FakeEngine:
        def __init__(self, db):
            pass

        def discover_and_register(self, kb_id, org_id=None, app_id=None, max_tables=None):
            calls.update(kb_id=kb_id, org_id=org_id, app_id=app_id, max_tables=max_tables)
            return ["target"]

    monkeypatch.setattr(
        "app.services.forecasting.unified_service.ForecastEngine", _FakeEngine
    )
    svc = _svc()
    assert svc.discover_products("kb1", org_id="o", app_id="a", max_tables=3) == ["target"]
    assert calls == {"kb_id": "kb1", "org_id": "o", "app_id": "a", "max_tables": 3}


def test_run_forecast_only_forwards_provided_kwargs(monkeypatch):
    calls = {}

    class _FakeEngine:
        def __init__(self, db):
            pass

        def compute_target(self, target_id, **kwargs):
            calls.update(target_id=target_id, kwargs=kwargs)
            return "run"

    monkeypatch.setattr(
        "app.services.forecasting.unified_service.ForecastEngine", _FakeEngine
    )
    svc = _svc()
    assert svc.run_forecast("t1") == "run"
    assert calls["kwargs"] == {}

    svc.run_forecast("t1", horizons=[7, 30])
    assert calls["kwargs"] == {"horizons": [7, 30]}


def test_get_forecast_batch_delegates_to_mds(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "app.services.forecasting.unified_service.mds",
        SimpleNamespace(
            get_forecast_base_batch_v2=lambda ids, day=None, db=None: seen.update(
                ids=ids, day=day, db=db
            )
            or "batch"
        ),
    )
    db = _FakeDB()
    svc = _svc(db)
    assert svc.get_forecast_batch(["p1", "p2"], day=7) == "batch"
    assert seen == {"ids": ["p1", "p2"], "day": 7, "db": db}


def test_get_decision_board_delegates(monkeypatch):
    import app.services.decision_board_service as dbs

    seen = {}
    monkeypatch.setattr(
        dbs,
        "build_decision_board",
        lambda group, day=None, db=None: seen.update(group=group, day=day, db=db)
        or "board",
    )
    db = _FakeDB()
    assert _svc(db).get_decision_board("c5", day=7) == "board"
    assert seen == {"group": "c5", "day": 7, "db": db}


def test_get_forecast_brief_delegates_to_analyst(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "app.services.forecasting.unified_service._analyst",
        SimpleNamespace(
            get_analyst_brief=lambda pid, day=None, db=None: seen.update(
                pid=pid, day=day, db=db
            )
            or "brief"
        ),
    )
    db = _FakeDB()
    assert _svc(db).get_forecast_brief("dcpd", day=7) == "brief"
    assert seen == {"pid": "dcpd", "day": 7, "db": db}


def test_get_forecast_trust_tier_delegates(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "app.services.forecasting.unified_service._trust_tier",
        SimpleNamespace(
            compute_forecast_trust_tier=lambda pid, **kw: seen.update(pid=pid, **kw)
            or "tier"
        ),
    )
    assert (
        _svc().get_forecast_trust_tier("p", below_naive=True, mape=0.1) == "tier"
    )
    assert seen == {"pid": "p", "below_naive": True, "cadence_class": None, "mape": 0.1}


def test_weekly_digest_list_paginates_and_filters():
    card = SimpleNamespace(id="c1")
    db = _FakeDB(by_model={"WeeklyReportCard": [card, card, card]})
    svc = _svc(db)
    res = svc.get_weekly_digest_list(org_id="o", page=2, page_size=1)
    assert res["total"] == 3
    assert res["page"] == 2
    assert len(res["items"]) == 1


def test_weekly_digest_detail_returns_none_when_missing():
    svc = _svc(_FakeDB(by_model={"WeeklyReportCard": []}))
    assert svc.get_weekly_digest_detail("missing", org_id="o") is None


def test_weekly_digest_detail_returns_card_and_products():
    card = SimpleNamespace(id="c1")
    product = SimpleNamespace(report_id="c1")
    db = _FakeDB(
        by_model={"WeeklyReportCard": [card], "WeeklyReportProduct": [product]}
    )
    res = _svc(db).get_weekly_digest_detail("c1", org_id="o")
    assert res is not None
    assert res["card"] is card
    assert res["products"] == [product]


def test_weekly_digest_latest_returns_none_when_missing():
    svc = _svc(_FakeDB(by_model={"WeeklyReportCard": []}))
    assert svc.get_weekly_digest_latest(org_id="o") is None


def test_product_forecast_target_key_exposed(monkeypatch):
    monkeypatch.setattr(
        "app.services.forecasting.unified_service.mds",
        SimpleNamespace(PRODUCT_FORECAST_TARGET_KEY={"dcpd": "ecisco.dcpd"}),
    )
    assert _svc().product_forecast_target_key == {"dcpd": "ecisco.dcpd"}


# ── Engine-compatible facade methods (drop-in for ForecastEngine call-sites) ──


def test_compute_target_anchored_delegates(monkeypatch):
    calls = {}

    class _FakeEngine:
        def __init__(self, db):
            pass

        def compute_target_anchored(self, target_id, **kwargs):
            calls.update(target_id=target_id, kwargs=kwargs)
            return {"run": "r", "anchor_rows": []}

    monkeypatch.setattr(
        "app.services.forecasting.unified_service.ForecastEngine", _FakeEngine
    )
    svc = _svc()
    assert svc.compute_target_anchored("t1", horizons=[7, 30], seasonal_period=7) == {
        "run": "r",
        "anchor_rows": [],
    }
    assert calls == {
        "target_id": "t1",
        "kwargs": {"horizons": [7, 30], "seasonal_period": 7},
    }


def test_get_latest_run_delegates(monkeypatch):
    calls = {}

    class _FakeEngine:
        def __init__(self, db):
            pass

        def get_latest_run(self, target_id):
            calls["target_id"] = target_id
            return "run"

    monkeypatch.setattr(
        "app.services.forecasting.unified_service.ForecastEngine", _FakeEngine
    )
    assert _svc().get_latest_run("t1") == "run"
    assert calls == {"target_id": "t1"}


def test_engine_compat_aliases(monkeypatch):
    seen = {}
    svc = _svc()
    monkeypatch.setattr(svc, "discover_products", lambda *a, **k: seen.setdefault("d", a))
    monkeypatch.setattr(svc, "get_forecast_accuracy", lambda tid: seen.setdefault("acc", tid))
    monkeypatch.setattr(
        svc, "run_forecast_batch", lambda org, h, s: seen.setdefault("b", (org, h, s))
    )

    svc.discover_and_register("kb", "o", "a", 3)
    assert seen["d"] == ("kb", "o", "a", 3)
    svc.get_accuracy("t")
    assert seen["acc"] == "t"
    svc.compute_all("o", [7], 7)
    assert seen["b"] == ("o", [7], 7)


def test_read_product_history_rows_delegates(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "app.services.forecasting.unified_service.mds",
        SimpleNamespace(
            read_product_history_rows=lambda pid: seen.update(pid=pid) or [("d", 1.0)]
        ),
    )
    assert _svc().read_product_history_rows("dcpd") == [("d", 1.0)]
    assert seen == {"pid": "dcpd"}


def test_get_weekly_forecast_payloads_delegates(monkeypatch):
    import app.services.weekly_digest.generator as gen

    monkeypatch.setattr(
        gen,
        "_read_forecast_for_product",
        lambda pkey, db: {"7d": {"base": 1.0}},
    )
    db = _FakeDB()
    assert _svc(db).get_weekly_forecast_payloads("dcpd") == {"7d": {"base": 1.0}}


# ── Redirect helpers (flag-gated facade-vs-legacy) ──


def test_forecast_tool_surface_flag_off(monkeypatch):
    from app.config import settings
    from app.services.tool_handlers import forecast_tool

    monkeypatch.setattr(settings, "UNIFIED_FORECASTING_SERVICE_ENABLED", False)
    db = _FakeDB()
    assert isinstance(forecast_tool._forecast_engine(db), forecast_tool.ForecastEngine)


def test_forecast_tool_surface_flag_on(monkeypatch):
    from app.config import settings
    from app.services.tool_handlers import forecast_tool

    monkeypatch.setattr(settings, "UNIFIED_FORECASTING_SERVICE_ENABLED", True)
    db = _FakeDB()
    assert isinstance(forecast_tool._forecast_engine(db), ForecastingAgentService)


def test_generator_surface_flag_off_on(monkeypatch):
    from app.config import settings
    from app.services.weekly_digest.generator import _surface

    monkeypatch.setattr(settings, "UNIFIED_FORECASTING_SERVICE_ENABLED", False)
    assert _surface(_FakeDB()) is None

    monkeypatch.setattr(settings, "UNIFIED_FORECASTING_SERVICE_ENABLED", True)
    db = _FakeDB()
    assert isinstance(_surface(db), ForecastingAgentService)


def test_decision_board_surface_flag_off_on(monkeypatch):
    from app.config import settings
    from app.services.decision_board_service import _surface

    monkeypatch.setattr(settings, "UNIFIED_FORECASTING_SERVICE_ENABLED", False)
    assert _surface(_FakeDB()) is None

    monkeypatch.setattr(settings, "UNIFIED_FORECASTING_SERVICE_ENABLED", True)
    db = _FakeDB()
    assert isinstance(_surface(db), ForecastingAgentService)
