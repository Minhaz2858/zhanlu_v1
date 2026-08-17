"""Tests for services/alerts/channel_router.py — webhook dispatch + HMAC."""

import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.alerts.channel_router import (
    _in_quiet_hours,
    _sign,
    dispatch,
    fire_webhook,
)
from app.services.alerts.preferences import AlertPrefSnapshot


# ── HMAC signing ──────────────────────────────────────────────────────


def test_sign_produces_sha256_hmac():
    body = json.dumps({"a": 1}, sort_keys=True).encode()
    sig = _sign(body, "secret")
    assert sig.startswith("sha256=")
    expected = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert sig == f"sha256={expected}"


# ── fire_webhook ──────────────────────────────────────────────────────


def _mock_client(sequence):
    client = AsyncMock()
    client.post.side_effect = sequence
    return client


def _patch_client(client):
    """Patch httpx.AsyncClient with a real async context manager yielding the fake."""
    @asynccontextmanager
    async def _cm(*args, **kwargs):
        yield client

    return patch(
        "app.services.alerts.channel_router.httpx.AsyncClient",
        side_effect=_cm,
    )


@pytest.mark.asyncio
async def test_fire_webhook_posts_signed_json():
    client = _mock_client([SimpleNamespace(status_code=200)])
    with _patch_client(client):
        ok = await fire_webhook("https://hooks.example.com/x", {"a": 1}, "sec")
    assert ok is True
    kwargs = client.post.call_args.kwargs
    assert kwargs["url"] == "https://hooks.example.com/x"
    assert kwargs["content"] == json.dumps({"a": 1}, sort_keys=True).encode()
    assert kwargs["headers"]["X-Zhanlu-Signature"].startswith("sha256=")
    assert kwargs["headers"]["Content-Type"] == "application/json"


@pytest.mark.asyncio
async def test_fire_webhook_drops_on_4xx_no_retry():
    client = _mock_client([SimpleNamespace(status_code=404)])
    with _patch_client(client):
        ok = await fire_webhook("https://hooks.example.com/x", {}, "sec", retry_delay=0)
    assert ok is False
    assert client.post.call_count == 1


@pytest.mark.asyncio
async def test_fire_webhook_retries_once_on_5xx():
    client = _mock_client(
        [SimpleNamespace(status_code=500), SimpleNamespace(status_code=200)]
    )
    with _patch_client(client):
        ok = await fire_webhook("https://hooks.example.com/x", {}, "sec", retry_delay=0)
    assert ok is True
    assert client.post.call_count == 2


@pytest.mark.asyncio
async def test_fire_webhook_retries_once_on_timeout():
    client = _mock_client(
        [
            SimpleNamespace(post=AsyncMock(side_effect=httpx.TimeoutException("t"))),
            SimpleNamespace(status_code=200),
        ]
    )
    # first post raises; second returns 200
    client.post.side_effect = [httpx.TimeoutException("t"), SimpleNamespace(status_code=200)]
    with _patch_client(client):
        ok = await fire_webhook("https://hooks.example.com/x", {}, "sec", retry_delay=0)
    assert ok is True
    assert client.post.call_count == 2


@pytest.mark.asyncio
async def test_fire_webhook_returns_false_when_retry_also_fails():
    client = _mock_client(
        [httpx.TimeoutException("t"), httpx.ConnectError("c")]
    )
    with _patch_client(client):
        ok = await fire_webhook("https://hooks.example.com/x", {}, "sec", retry_delay=0)
    assert ok is False
    assert client.post.call_count == 2


# ── quiet hours ───────────────────────────────────────────────────────


def test_in_quiet_hours_nightly_window():
    pref = AlertPrefSnapshot(
        user_id="u1",
        quiet_hours={"enabled": True, "start": "22:00", "end": "07:00"},
    )
    assert (
        _in_quiet_hours(pref, now=datetime(2026, 8, 18, 23, 0, tzinfo=timezone.utc))
        is True
    )
    assert (
        _in_quiet_hours(pref, now=datetime(2026, 8, 18, 2, 0, tzinfo=timezone.utc))
        is True
    )
    assert (
        _in_quiet_hours(pref, now=datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc))
        is False
    )


def test_in_quiet_hours_disabled_or_malformed():
    disabled = AlertPrefSnapshot(user_id="u1", quiet_hours={"enabled": False})
    assert (
        _in_quiet_hours(disabled, now=datetime(2026, 8, 18, 23, 0, tzinfo=timezone.utc))
        is False
    )
    malformed = AlertPrefSnapshot(user_id="u1", quiet_hours={"enabled": True})
    assert _in_quiet_hours(malformed) is False


# ── dispatch ──────────────────────────────────────────────────────────


def _alert(**kw):
    base = {
        "org_id": "org-1",
        "product_id": "p1",
        "product_name": "C5 resin",
        "alert_type": "price_spike",
        "severity": "high",
        "change_7d_pct": 8.0,
        "price": 100.0,
        "message": "msg",
    }
    base.update(kw)
    return base


@pytest.mark.asyncio
async def test_dispatch_uses_webhook_channel():
    pref = AlertPrefSnapshot(
        user_id="u1",
        channels=["in_app", "webhook"],
        webhook_url="https://hooks.example.com/x",
        webhook_secret="sec",
    )
    with patch(
        "app.services.alerts.channel_router.fire_webhook",
        new_callable=AsyncMock,
        return_value=True,
    ) as fw:
        used = await dispatch(_alert(), pref)
    fw.assert_awaited_once()
    assert used == ["in_app", "webhook"]


@pytest.mark.asyncio
async def test_dispatch_skips_webhook_during_quiet_hours():
    pref = AlertPrefSnapshot(
        user_id="u1",
        channels=["in_app", "webhook"],
        webhook_url="https://hooks.example.com/x",
        webhook_secret="sec",
        quiet_hours={"enabled": True, "start": "22:00", "end": "07:00"},
    )
    with patch(
        "app.services.alerts.channel_router.fire_webhook",
        new_callable=AsyncMock,
    ) as fw:
        used = await dispatch(
            _alert(),
            pref,
            now=datetime(2026, 8, 18, 23, 0, tzinfo=timezone.utc),
        )
    fw.assert_not_awaited()
    assert used == ["in_app"]


@pytest.mark.asyncio
async def test_dispatch_no_webhook_when_not_configured():
    pref = AlertPrefSnapshot(user_id="u1", channels=["in_app"])
    with patch(
        "app.services.alerts.channel_router.fire_webhook",
        new_callable=AsyncMock,
    ) as fw:
        used = await dispatch(_alert(), pref)
    fw.assert_not_awaited()
    assert used == ["in_app"]


@pytest.mark.asyncio
async def test_dispatch_webhook_payload_carries_impact():
    pref = AlertPrefSnapshot(
        user_id="u1",
        channels=["webhook"],
        webhook_url="https://hooks.example.com/x",
        webhook_secret="sec",
    )
    with patch(
        "app.services.alerts.channel_router.fire_webhook",
        new_callable=AsyncMock,
        return_value=True,
    ) as fw:
        await dispatch(
            _alert(payload={"smart": True, "impact": {"impact_yuan_per_day": 1234.5}}),
            pref,
        )
    payload = fw.await_args.args[1]
    assert payload["event"] == "zhanlu.alert"
    assert payload["impact_yuan_per_day"] == 1234.5
