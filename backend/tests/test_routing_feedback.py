"""Tests for the routing feedback reader (IBM workstream E2)."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.services import routing_feedback as rf


def _row(route, status="completed", duration_ms=1000):
    return SimpleNamespace(route=route, status=status, duration_ms=duration_ms)


def _fake_db(rows):
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = rows
    return db


def _seed(n, route, ok_ratio=1.0, dur=1000):
    rows = []
    for i in range(n):
        status = "completed" if i < n * ok_ratio else "failed"
        rows.append(_row(route, status, dur))
    return rows


@pytest.fixture(autouse=True)
def _feedback_off(monkeypatch):
    monkeypatch.setattr(settings, "ROUTING_FEEDBACK_ENABLED", False)
    rf._CACHE.update({"overrides": None, "expires": 0.0})
    yield


# ---------------------------------------------------------------- stats

def test_compute_route_stats_success_rate_and_latency():
    rows = _seed(30, "react", ok_ratio=0.8, dur=500) + _seed(25, "fsm", ok_ratio=0.9, dur=900)
    stats = {s.route: s for s in rf.compute_route_stats(_fake_db(rows))}
    assert stats["react"].turns == 30
    assert stats["react"].success_rate == pytest.approx(0.8)
    assert stats["fsm"].p50_ms == 900


def test_compute_route_stats_min_turns_filter():
    rows = _seed(5, "react") + _seed(25, "fsm")
    stats = rf.compute_route_stats(_fake_db(rows))
    assert [s.route for s in stats] == ["fsm"]


def test_compute_route_stats_empty():
    assert rf.compute_route_stats(_fake_db([])) == []


# ---------------------------------------------------------------- threshold

def test_fsm_better_lowes_threshold():
    rows = _seed(25, "react", ok_ratio=0.7, dur=2000) + _seed(25, "fsm", ok_ratio=1.0, dur=800)
    suggested = rf.suggest_fsm_threshold(_fake_db(rows), current_threshold=0.5)
    assert suggested == pytest.approx(0.45)


def test_fsm_worse_raises_threshold():
    rows = _seed(25, "react", ok_ratio=0.95, dur=500) + _seed(25, "fsm", ok_ratio=0.5, dur=500)
    suggested = rf.suggest_fsm_threshold(_fake_db(rows), current_threshold=0.5)
    assert suggested == pytest.approx(0.55)


def test_inconclusive_returns_none():
    rows = _seed(25, "react", ok_ratio=0.9, dur=800) + _seed(25, "fsm", ok_ratio=0.92, dur=800)
    assert rf.suggest_fsm_threshold(_fake_db(rows)) is None


def test_threshold_clamped():
    rows = _seed(25, "react", ok_ratio=0.0, dur=100) + _seed(25, "fsm", ok_ratio=0.0, dur=100)
    # fsm < react? both 0.0 — equal → inconclusive → None. Force fsm worse:
    rows = _seed(25, "react", ok_ratio=0.2, dur=100) + _seed(25, "fsm", ok_ratio=0.1, dur=100)
    suggested = rf.suggest_fsm_threshold(_fake_db(rows), current_threshold=0.35)
    assert suggested == pytest.approx(0.4)  # raised, under ceiling


def test_insufficient_data_returns_none():
    rows = _seed(10, "react") + _seed(10, "fsm")
    assert rf.suggest_fsm_threshold(_fake_db(rows)) is None


# ---------------------------------------------------------------- consumption

def test_overrides_none_when_flag_off():
    rows = _seed(25, "react", ok_ratio=0.7) + _seed(25, "fsm", ok_ratio=1.0)
    assert rf.get_suggested_overrides(_fake_db(rows)) is None


def test_overrides_applied_when_flag_on(monkeypatch):
    monkeypatch.setattr(settings, "ROUTING_FEEDBACK_ENABLED", True)
    rows = _seed(25, "react", ok_ratio=0.7, dur=2000) + _seed(25, "fsm", ok_ratio=1.0, dur=800)
    overrides = rf.get_suggested_overrides(_fake_db(rows))
    assert overrides is not None
    assert overrides["fsm_plan_confidence"] == pytest.approx(0.45)
    # cached second read
    assert rf.get_suggested_overrides(_fake_db(rows)) == overrides
