"""T17 regression: DashboardAppManager.resolve_app_dir.

The catch-all route serves a dashboard app's static files by resolving its
on-disk directory per request. `resolve_app_dir` is the single source of truth
and must:
  - return the existing app dir,
  - be cached (lru_cache keyed on the dist mtime) so hot apps don't pay a
    syscall on every request,
  - invalidate the cache when the app is regenerated (mtime changes).
"""

import sys
import os
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from app.services.dashboard_app.manager import DashboardAppManager
from app.services.dashboard_app.generator import get_generator


def _fake_generator(tmp_root: Path):
    """Return a generator-like object whose app_dir points under tmp_root."""
    class _FakeGen:
        def app_dir(self, slug: str) -> Path:
            d = tmp_root / slug
            (d / "dist").mkdir(parents=True, exist_ok=True)
            return d
    return _FakeGen()


def test_resolve_returns_existing_dir(tmp_path, monkeypatch):
    gen = _fake_generator(tmp_path)
    monkeypatch.setattr(
        "app.services.dashboard_app.manager.get_generator", lambda: gen
    )
    mgr = DashboardAppManager()
    # Clear any prior cache from other tests in the process.
    mgr._resolve_app_dir_cached.cache_clear()
    out = mgr.resolve_app_dir("erp-sales-overview")
    assert out is not None
    assert out.exists()
    assert (out / "dist").exists()


def test_resolve_caches_on_mtime(tmp_path, monkeypatch):
    gen = _fake_generator(tmp_path)
    monkeypatch.setattr(
        "app.services.dashboard_app.manager.get_generator", lambda: gen
    )
    mgr = DashboardAppManager()
    mgr._resolve_app_dir_cached.cache_clear()

    first = mgr.resolve_app_dir("cached-app")
    # The lru_cache returns the SAME Path object for the same (slug, mtime).
    second = mgr.resolve_app_dir("cached-app")
    assert first is second  # cached — identical object, no recompute


def test_resolve_invalidates_on_regenerate(tmp_path, monkeypatch):
    """When the dist dir is regenerated (mtime changes), the cache must
    return a fresh result rather than the stale cached path."""
    gen = _fake_generator(tmp_path)
    monkeypatch.setattr(
        "app.services.dashboard_app.manager.get_generator", lambda: gen
    )
    mgr = DashboardAppManager()
    mgr._resolve_app_dir_cached.cache_clear()

    before = mgr.resolve_app_dir("regen-app")
    # Simulate a regenerate: touch the dist dir so its mtime advances.
    dist = before / "dist"
    time.sleep(0.01)
    (dist / "index.html").write_text("<html></html>")
    os.utime(dist, (time.time(), time.time()))

    after = mgr.resolve_app_dir("regen-app")
    # Different path object (cache invalidated by mtime) but same location.
    assert after is not before
    assert after == before
