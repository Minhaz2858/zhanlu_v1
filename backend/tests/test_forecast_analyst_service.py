"""Analyst service — cache, fallback chain, persistence."""
from types import SimpleNamespace

from app.services.forecasting.analyst import service as analyst_service
from tests.test_forecast_analyst_pack import _explanation, _history


class _FakeQuery:
    def __init__(self, rows): self._rows = rows
    def filter(self, *a): return self
    def order_by(self, *a): return self
    def first(self): return self._rows[0] if self._rows else None


class _FakeDB:
    def __init__(self, target, run):
        self._target, self._run = target, run
        self.commits = 0
    def query(self, model):
        name = getattr(model, "__name__", "")
        return _FakeQuery([self._target] if "Target" in name else [self._run])
    def commit(self): self.commits += 1
    def rollback(self): pass


def _run(explanation):
    return SimpleNamespace(
        results={"7": {"base": [6660.0], "bull": [6900.0], "bear": [6400.0]}},
        model_detail={"models_run": ["a"] * 8},
        explanation=explanation,
    )


def _patch_reads(monkeypatch):
    monkeypatch.setattr(analyst_service.mds, "read_product_history_rows",
                        lambda pid: _history(start=7000.0, step=-0.8))
    monkeypatch.setattr(analyst_service.mds, "PRODUCT_FORECAST_TARGET_KEY",
                        {"dcpd": "ecisco.dcpd"})
    monkeypatch.setattr(analyst_service, "PRODUCT_LABELS",
                        {"dcpd": {"label_zh": "双环戊二烯"}})


def test_cache_hit_returns_without_llm(monkeypatch):
    _patch_reads(monkeypatch)
    cached = {"market_update_zh": "cached", "source": "llm", "day": 7}
    run = _run({**_explanation(), "analyst_brief": {"7": cached}})
    db = _FakeDB(SimpleNamespace(id="t1"), run)

    def _no_llm():
        raise AssertionError("LLM called on cache hit")
    monkeypatch.setattr(analyst_service, "_llm_enabled", _no_llm)
    assert analyst_service.get_analyst_brief("dcpd", 7, db=db) == cached
    assert db.commits == 0


def test_miss_builds_template_and_persists(monkeypatch):
    _patch_reads(monkeypatch)
    run = _run(_explanation())
    db = _FakeDB(SimpleNamespace(id="t1"), run)
    monkeypatch.setattr(analyst_service, "_llm_enabled", lambda: False)
    brief = analyst_service.get_analyst_brief("dcpd", 7, db=db)
    assert brief is not None and brief["source"] == "template"
    assert run.explanation["analyst_brief"]["7"]["source"] == "template"
    assert db.commits == 1


def test_llm_upgrade_when_enabled(monkeypatch):
    _patch_reads(monkeypatch)
    run = _run(_explanation())
    db = _FakeDB(SimpleNamespace(id="t1"), run)
    monkeypatch.setattr(analyst_service, "_llm_enabled", lambda: True)
    good = {"market_update_zh": "市场情报中性。",
            "price_data_zh": "现价 6,940 元/吨。",
            "upstream_logic_zh": "上游裂解碳五下跌,弹性 0.66 传导。",
            "supply_demand_zh": "8模型分歧偏大。",
            "forecast_zh": "预计跌至 6,660 元/吨,↘ 偏弱。",
            "watch_triggers_zh": ["准确率 55% 以上再评估"], "risk_zh": "近期无重大市场事件。"}
    monkeypatch.setattr(analyst_service, "chat_completion_json_sync",
                        lambda prompt, schema: dict(good))
    brief = analyst_service.get_analyst_brief("dcpd", 7, db=db)
    assert brief["source"] == "llm"


def test_llm_failure_falls_back_to_template(monkeypatch):
    _patch_reads(monkeypatch)
    run = _run(_explanation())
    db = _FakeDB(SimpleNamespace(id="t1"), run)
    monkeypatch.setattr(analyst_service, "_llm_enabled", lambda: True)
    monkeypatch.setattr(analyst_service, "chat_completion_json_sync", lambda p, s: {})
    assert analyst_service.get_analyst_brief("dcpd", 7, db=db)["source"] == "template"


def test_unknown_product_returns_none(monkeypatch):
    monkeypatch.setattr(analyst_service.mds, "PRODUCT_FORECAST_TARGET_KEY", {})
    assert analyst_service.get_analyst_brief("nope", 7, db=_FakeDB(None, None)) is None
