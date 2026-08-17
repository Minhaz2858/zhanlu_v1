"""Tests for BaselineCache (services/perception/baseline_cache.py).

Covers: fresh-row cache hit (no LLM), miss → seed default + schedule LLM,
awaitable refresh path, LLM failure → default preserved, invalid JSON → default.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.models.alert_baseline import AlertProductBaseline
from app.services.perception.baseline_cache import (
    DEFAULT_BASELINE_PCT,
    BaselineCache,
)


class FakeDB:
    """Minimal in-memory stand-in for the SQLAlchemy Session used in tests."""

    def __init__(self):
        self.rows: dict[str, AlertProductBaseline] = {}

    def get(self, _model, pk):
        return self.rows.get(pk)

    def add(self, obj):
        self.rows[obj.product_id] = obj

    def commit(self):
        pass


def _product(pid: str = "c5_resin", name: str = "C5石油树脂",
             price: float = 9900.0, change: float = 16.82) -> dict:
    return {
        "product_id": pid,
        "product_name": name,
        "current_price": price,
        "change_7d_pct": change,
    }


def _fresh_row(pid: str = "c5_resin", baseline: float = 3.2) -> AlertProductBaseline:
    return AlertProductBaseline(
        product_id=pid,
        baseline_pct=baseline,
        reasoning="cached",
        computed_at=datetime.now(timezone.utc),
        model_used="test",
    )


def test_cache_hit_returns_fresh_row_without_llm():
    db = FakeDB()
    fresh = _fresh_row()
    db.rows["c5_resin"] = fresh
    cache = BaselineCache(db)

    with patch("app.services.perception.baseline_cache.call_llm") as llm:
        row = cache.get_or_compute(_product())

    assert row is fresh
    llm.assert_not_called()


def test_cache_hit_ignores_stale_row_and_reseeds_default():
    """Row older than TTL must not be trusted; default is seeded, LLM scheduled."""
    db = FakeDB()
    stale = AlertProductBaseline(
        product_id="c5_resin",
        baseline_pct=1.0,
        reasoning="old",
        computed_at=datetime.now(timezone.utc) - timedelta(days=30),
    )
    db.rows["c5_resin"] = stale
    cache = BaselineCache(db)

    with patch("app.services.perception.baseline_cache.call_llm") as llm:
        row = cache.get_or_compute(_product())

    assert row.baseline_pct == DEFAULT_BASELINE_PCT  # reseeded default
    assert db.rows["c5_resin"].baseline_pct == DEFAULT_BASELINE_PCT
    llm.assert_not_called()  # fire-and-forget, not awaited here


def test_miss_seeds_default_and_schedules_llm_refresh():
    db = FakeDB()
    cache = BaselineCache(db)

    with patch(
        "app.services.perception.baseline_cache.asyncio.create_task"
    ) as create_task:
        row = cache.get_or_compute(_product())

    assert row.baseline_pct == DEFAULT_BASELINE_PCT
    assert "c5_resin" in db.rows
    assert create_task.call_count == 1  # one fire-and-forget refresh scheduled


@pytest.mark.asyncio
async def test_refresh_calls_llm_and_persists_baseline():
    db = FakeDB()
    cache = BaselineCache(db)

    with patch(
        "app.services.perception.baseline_cache.call_llm",
        new=AsyncMock(
            return_value={
                "response": '{"baseline_pct": 5.5, "reasoning": "volatile market"}',
                "model": "deepseek-chat",
            }
        ),
    ) as llm:
        row = await cache.refresh(_product())

    llm.assert_called_once()
    assert row.baseline_pct == 5.5
    assert row.reasoning == "volatile market"
    assert row.model_used == "deepseek-chat"
    assert db.rows["c5_resin"] is row  # persisted


@pytest.mark.asyncio
async def test_refresh_updates_existing_seeded_row():
    db = FakeDB()
    cache = BaselineCache(db)
    db.rows["c5_resin"] = _fresh_row(baseline=2.5)

    with patch(
        "app.services.perception.baseline_cache.call_llm",
        new=AsyncMock(return_value={"response": '{"baseline_pct": 4.1, "reasoning": "up"}'}),
    ):
        row = await cache.refresh(_product())

    assert row.baseline_pct == 4.1


@pytest.mark.asyncio
async def test_llm_failure_keeps_default_baseline():
    db = FakeDB()
    cache = BaselineCache(db)

    with patch(
        "app.services.perception.baseline_cache.call_llm",
        new=AsyncMock(side_effect=RuntimeError("llm down")),
    ):
        row = await cache.refresh(_product())

    assert row.baseline_pct == DEFAULT_BASELINE_PCT
    assert "c5_resin" in db.rows  # default row still persisted


@pytest.mark.asyncio
async def test_invalid_json_response_keeps_default_baseline():
    db = FakeDB()
    cache = BaselineCache(db)

    with patch(
        "app.services.perception.baseline_cache.call_llm",
        new=AsyncMock(return_value={"response": "sorry, no json here", "model": "x"}),
    ):
        row = await cache.refresh(_product())

    assert row.baseline_pct == DEFAULT_BASELINE_PCT
