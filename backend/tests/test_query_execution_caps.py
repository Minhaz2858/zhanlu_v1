"""Execution caps and truncation truth (QueryService + engine cache).

Three defects these pin:

1. `truncated = len(rows) >= max_rows` could not tell "the data ends here" from
   "the cap bit" — an exact-cap result was reported truncated.
2. `max_rows` / `timeout_s` came straight from model-authored tool args, so the
   model could raise its own caps (a 1-hour statement timeout).
3. The engine cache was unbounded: passwords are part of the cache key, so every
   credential ever used kept a live pool for the process lifetime.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.db import engine_cache, query_service as qs


class _FakeConnector:
    def __init__(self, rows: list[dict]):
        self._rows = rows
        self.calls: list[tuple[int, int]] = []

    def execute(self, sql, params=None, max_rows=1000, timeout_s=10):
        self.calls.append((max_rows, timeout_s))
        return self._rows[:max_rows]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _service(monkeypatch, rows: list[dict], connector: _FakeConnector | None = None):
    conn = connector or _FakeConnector(rows)
    monkeypatch.setattr(qs, "get_connector", lambda kb: conn)
    monkeypatch.setattr(
        qs.QueryService, "_load_kb",
        lambda self, kb_id: SimpleNamespace(
            id=kb_id, name="KB", db_type="sqlite", source_kind="database",
        ),
    )
    return qs.QueryService(db=None), conn


# ---------------------------------------------------------------------------
# Truncation truth
# ---------------------------------------------------------------------------

def test_small_result_is_not_truncated(monkeypatch):
    svc, _conn = _service(monkeypatch, [{"n": i} for i in range(3)])
    out = svc.execute("kb", "SELECT 1", max_rows=10)
    assert out["truncated"] is False
    assert out["row_count"] == 3
    assert out["rows_returned"] == 3
    assert out["row_cap"] == 10


def test_exact_cap_result_is_not_reported_truncated(monkeypatch):
    """The regression: exactly max_rows rows used to look truncated."""
    svc, _conn = _service(monkeypatch, [{"n": i} for i in range(10)])
    out = svc.execute("kb", "SELECT 1", max_rows=10)
    assert out["truncated"] is False
    assert out["row_count"] == 10


def test_more_rows_than_cap_is_proven_truncated(monkeypatch):
    svc, _conn = _service(monkeypatch, [{"n": i} for i in range(25)])
    out = svc.execute("kb", "SELECT 1", max_rows=10)
    assert out["truncated"] is True
    assert out["row_count"] == 10          # capped for the caller
    assert out["row_cap"] == 10


def test_one_extra_row_is_requested_to_prove_truncation(monkeypatch):
    svc, conn = _service(monkeypatch, [{"n": i} for i in range(25)])
    svc.execute("kb", "SELECT 1", max_rows=10)
    assert conn.calls[0][0] == 11          # cap + 1


# ---------------------------------------------------------------------------
# Caps the model cannot raise
# ---------------------------------------------------------------------------

def test_row_cap_is_clamped(monkeypatch):
    svc, conn = _service(monkeypatch, [{"n": 1}])
    out = svc.execute("kb", "SELECT 1", max_rows=10_000_000)
    assert out["row_cap"] == settings.MAX_QUERY_ROWS_CAP
    assert conn.calls[0][0] == settings.MAX_QUERY_ROWS_CAP + 1


def test_timeout_is_clamped(monkeypatch):
    svc, conn = _service(monkeypatch, [{"n": 1}])
    svc.execute("kb", "SELECT 1", max_rows=10, timeout_s=3600)
    assert conn.calls[0][1] == settings.MAX_QUERY_TIMEOUT_S


def test_sane_caps_pass_through_unchanged(monkeypatch):
    svc, conn = _service(monkeypatch, [{"n": 1}])
    svc.execute("kb", "SELECT 1", max_rows=50, timeout_s=7)
    assert conn.calls[0] == (51, 7)


def test_garbage_caps_fall_back_to_defaults(monkeypatch):
    svc, conn = _service(monkeypatch, [{"n": 1}])
    out = svc.execute("kb", "SELECT 1", max_rows="lots", timeout_s=None)
    assert out["row_cap"] == qs.DEFAULT_MAX_ROWS
    assert conn.calls[0][1] == qs.DEFAULT_TIMEOUT_S


# ---------------------------------------------------------------------------
# Engine cache is bounded
# ---------------------------------------------------------------------------

@pytest.fixture()
def clean_cache():
    saved = (dict(engine_cache._CACHE), dict(engine_cache._LAST_USED))
    engine_cache._CACHE.clear()
    engine_cache._LAST_USED.clear()
    yield
    engine_cache._CACHE.clear()
    engine_cache._LAST_USED.clear()
    engine_cache._CACHE.update(saved[0])
    engine_cache._LAST_USED.update(saved[1])


def test_cache_reuses_an_engine_for_the_same_signature(clean_cache):
    a = engine_cache.acquire_engine("sqlite", "sqlite+pysqlite:///same.db")
    b = engine_cache.acquire_engine("sqlite", "sqlite+pysqlite:///same.db")
    assert a is b
    assert engine_cache.engine_count() == 1


def test_cache_is_capped_and_evicts_the_least_recently_used(clean_cache):
    urls = [f"sqlite+pysqlite:///mem{i}.db" for i in range(engine_cache._MAX_ENGINES + 8)]
    for url in urls:
        engine_cache.acquire_engine("sqlite", url)
        time.sleep(0)   # keep timestamps distinguishable
    assert engine_cache.engine_count() <= engine_cache._MAX_ENGINES
    # The most recent one always survives its own acquisition.
    assert any(engine_cache._cache_key("sqlite", urls[-1], None) == k
               for k in engine_cache._CACHE)


def test_rotating_a_password_does_not_strand_the_old_engine(clean_cache):
    """A credential change makes a NEW key; the old engine must not live forever."""
    for i in range(engine_cache._MAX_ENGINES + 5):
        engine_cache.acquire_engine("mysql", f"mysql+pymysql://u:pw{i}@h/db")
    assert engine_cache.engine_count() <= engine_cache._MAX_ENGINES


def test_idle_engines_are_pruned(clean_cache):
    url = "sqlite+pysqlite:///idle.db"
    engine_cache.acquire_engine("sqlite", url)
    key = engine_cache._cache_key("sqlite", url, None)
    engine_cache._LAST_USED[key] = time.monotonic() - (engine_cache._IDLE_TTL_S + 60)
    assert engine_cache.prune_engines() == 1
    assert engine_cache.engine_count() == 0


def test_reset_engine_drops_one_signature(clean_cache):
    url = "sqlite+pysqlite:///reset.db"
    engine_cache.acquire_engine("sqlite", url)
    engine_cache.reset_engine("sqlite", url)
    assert engine_cache.engine_count() == 0
