"""Regression tests for DashboardAppManager lifecycle — no real DB required.

We monkeypatch ``SessionLocal`` with an in-memory fake so the test does not
depend on the deployed Postgres / migration state. The tests exercise the
record lifecycle (create -> set_status -> get -> list -> unmount) and the
in-memory mount registry.
"""
from types import SimpleNamespace

import pytest

import app.services.dashboard_app.manager as mgr_mod
from app.services.dashboard_app.manager import DashboardAppManager


class FakeSession:
    def __init__(self):
        self._rows = {}
        self._counter = 0
        self.closed = False

    def add(self, obj):
        self._counter += 1
        obj.id = self._counter
        self._rows[obj.slug] = obj

    def commit(self):
        pass

    def refresh(self, obj):
        pass

    def query(self, model):
        rows = list(self._rows.values())

        class Q:
            def __init__(self, rows):
                self._rows = rows
                self._filtered = list(rows)

            def filter(self, *args, **kwargs):
                # Support `DashboardApp.slug == value` style comparisons.
                for expr in args:
                    left = getattr(expr, "left", None)
                    right = getattr(expr, "right", None)
                    attr = getattr(left, "key", None) if left is not None else None
                    if attr is not None:
                        # right may be a BindParameter wrapping the literal value.
                        value = getattr(right, "value", right)
                        self._filtered = [r for r in self._filtered if getattr(r, attr, None) == value]
                return self

            def order_by(self, *args, **kwargs):
                return self

            def all(self):
                return list(self._filtered)

            def first(self):
                return self._filtered[0] if self._filtered else None

        return Q(rows)

    def close(self):
        self.closed = True


@pytest.fixture
def mgr():
    fake = FakeSession()
    orig = mgr_mod.SessionLocal
    mgr_mod.SessionLocal = lambda: fake
    manager = DashboardAppManager()
    yield manager, fake
    mgr_mod.SessionLocal = orig


def test_create_app_record_sets_building(mgr):
    manager, fake = mgr
    rec = manager.create_app_record(
        {"name": "X", "slug": "x", "datasource_id": "kb", "metrics": []},
        org_id="org1", created_by_id="u1",
    )
    assert rec.status == "building"
    assert rec.org_id == "org1"
    assert rec.slug == "x"


def test_set_status_updates_record(mgr):
    manager, fake = mgr
    manager.create_app_record(
        {"name": "X", "slug": "x", "datasource_id": "kb", "metrics": []},
        org_id="org1", created_by_id="u1",
    )
    rec = manager.set_status("x", "running", app_url="/api/dashboards/apps/x/")
    assert rec.status == "running"
    assert rec.app_url == "/api/dashboards/apps/x/"


def test_get_and_list_apps(mgr):
    manager, fake = mgr
    manager.create_app_record({"name": "A", "slug": "a", "datasource_id": "kb", "metrics": []}, "org1", "u1")
    manager.create_app_record({"name": "B", "slug": "b", "datasource_id": "kb", "metrics": []}, "org1", "u1")
    assert manager.get_app("a") is not None
    assert manager.get_app("missing") is None
    apps = manager.list_apps(org_id="org1")
    assert len(apps) == 2


def test_mount_requires_init_app(mgr):
    manager, fake = mgr
    manager.create_app_record({"name": "A", "slug": "a", "datasource_id": "kb", "metrics": []}, "org1", "u1")
    # Documented contract: mount() before init_app(app) must raise.
    with pytest.raises(RuntimeError):
        manager.mount("a")


def test_unmount_missing_is_noop(mgr):
    manager, fake = mgr
    # unmounting a slug that was never mounted must not raise
    manager.unmount("never-mounted")
    assert "never-mounted" not in manager._mounted
