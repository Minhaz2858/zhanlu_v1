"""Tests for /api/market-dashboard/events/latest endpoint (Task 3)."""
import pytest
import json


class FakeResult:
    def __init__(self, rows): self._rows = rows
    def fetchall(self): return self._rows
    def first(self): return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, rows): self._rows = rows
    def execute(self, stmt, params=None): return FakeResult(self._rows)
    def __enter__(self): return self
    def __exit__(self, *a): return False


class FakeEngine:
    def __init__(self, rows): self._rows = rows
    def connect(self): return FakeConn(self._rows)


def test_events_latest_maps_intelligence_events_rows(monkeypatch):
    monkeypatch.setattr(
        "app.services.market_dashboard_service.settings.EDIA_MYSQL_URL",
        "mysql+pymysql://x", raising=False,
    )
    monkeypatch.setattr(
        "app.services.market_dashboard_service.is_edia_reachable_cached",
        lambda: True,
    )
    rows = [(
        "evt_1", "Headline A", "policy_change", "neutral", 0.5, "high",
        "tier2", 0.7, 0.8, json.dumps(["c5", "naphtha"]),
        "causal hint", "http://x", "Full text", "raw quote",
        "high", "key_info", "2026-07-31T10:00:00",
        "short", "national", json.dumps(["entity1", "entity2"]),
    )]
    monkeypatch.setattr(
        "app.services.market_dashboard_service.get_mysql_engine",
        lambda: FakeEngine(rows),
    )
    from app.services import market_dashboard_service as svc
    r = svc.get_events_latest(hours=168, review_status="approved", min_usefulness_score=0)
    assert r.count == 1
    e = r.events[0]
    assert e.event_id == "evt_1"
    assert e.headline == "Headline A"
    assert e.affected_commodities == ["c5", "naphtha"]
    assert e.key_entities == ["entity1", "entity2"]


def test_events_latest_empty_when_no_rows(monkeypatch):
    monkeypatch.setattr(
        "app.services.market_dashboard_service.settings.EDIA_MYSQL_URL",
        "mysql+pymysql://x", raising=False,
    )
    monkeypatch.setattr(
        "app.services.market_dashboard_service.is_edia_reachable_cached",
        lambda: True,
    )
    monkeypatch.setattr(
        "app.services.market_dashboard_service.get_mysql_engine",
        lambda: FakeEngine([]),
    )
    from app.services import market_dashboard_service as svc
    r = svc.get_events_latest(hours=168, review_status="approved", min_usefulness_score=0)
    assert r.events == []
    assert r.count == 0
