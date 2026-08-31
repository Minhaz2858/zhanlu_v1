"""Regression tests for DashboardAppManager lifecycle — no real DB required.

We monkeypatch ``SessionLocal`` with an in-memory fake so the test does not
depend on the deployed Postgres / migration state. The tests exercise the
record lifecycle (create -> set_status -> get -> list -> unmount) and the
in-memory mount registry.
"""
from types import SimpleNamespace

import pytest

import app.services.dashboard_app.manager as mgr_mod
from app.services.dashboard_app.generator import DashboardAppGenerator, TEMPLATE_DIR
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


def test_create_app_record_stores_chat_thread(mgr):
    """T5: chat_thread_id passed at creation is persisted on the row."""
    manager, fake = mgr
    rec = manager.create_app_record(
        {"name": "T", "slug": "t5-dash", "datasource_id": "kb", "metrics": []},
        org_id="org1", created_by_id="u1",
        chat_thread_id="conv-abc",
    )
    assert rec.chat_thread_id == "conv-abc"


def test_create_app_record_defaults_scope_personal(mgr):
    """T10: scope defaults to personal unless explicitly overridden."""
    manager, fake = mgr
    rec = manager.create_app_record(
        {"name": "T", "slug": "t5-scope", "datasource_id": "kb", "metrics": []},
        org_id="org1", created_by_id="u1",
    )
    assert rec.scope == "personal"


def test_touch_chat_thread_binds_existing_record(mgr):
    """T5: touch_chat_thread re-binds the thread on an existing record."""
    manager, fake = mgr
    manager.create_app_record({"name": "T", "slug": "t5-touch", "datasource_id": "kb", "metrics": []},
                              "org1", "u1")
    assert manager.touch_chat_thread("t5-touch", "conv-xyz") is True
    assert manager.get_app("t5-touch").chat_thread_id == "conv-xyz"


def test_touch_chat_thread_missing_slug_returns_false(mgr):
    """T5: touching a slug that was never created must not raise."""
    manager, fake = mgr
    assert manager.touch_chat_thread("never-existed", "conv-q") is False


def test_mount_without_init_app_is_graceful(mgr):
    """T17 contract: mount() no longer requires init_app(app).

    The per-app FastAPI mount was removed (T17) — serving is done by the GLOBAL
    catch-all routes in routers/dashboards.py, so ``init_app`` is now only a
    stored reference and mount() must degrade gracefully (log + skip the
    poller when there is no running event loop) instead of raising.
    """
    manager, fake = mgr
    manager.create_app_record({"name": "A", "slug": "a", "datasource_id": "kb", "metrics": []}, "org1", "u1")
    # mount() without init_app must not raise (pre-T17 this raised RuntimeError).
    manager.mount("a")


def test_unmount_missing_is_noop(mgr):
    manager, fake = mgr
    # unmounting a slug that was never mounted must not raise
    manager.unmount("never-mounted")
    assert "never-mounted" not in manager._tasks


def _tmp_generator(monkeypatch, tmp_path):
    """Point manager's get_generator at an isolated temp-dir generator."""
    from app.services.dashboard_app import generator as gen_mod

    gen = DashboardAppGenerator(template_dir=TEMPLATE_DIR, apps_dir=tmp_path / "apps")
    monkeypatch.setattr(gen_mod, "get_generator", lambda: gen)
    return gen


def test_commit_version_snapshots_app_dir(mgr, monkeypatch, tmp_path):
    """T6: commit_version writes a .versions snapshot for the on-disk app."""
    manager, fake = mgr
    gen = _tmp_generator(monkeypatch, tmp_path)
    gen.generate({"name": "S", "slug": "t6-snap", "datasource_id": "kb", "metrics": []})

    snap = manager.commit_version("t6-snap", "create")
    assert snap is not None and snap.is_dir()
    assert (snap / "api.py").is_file()
    assert (snap / "config.json").is_file()


def test_commit_version_missing_dir_returns_none(mgr, monkeypatch, tmp_path):
    """T6: commit_version on a never-generated slug must not raise."""
    manager, fake = mgr
    _tmp_generator(monkeypatch, tmp_path)
    assert manager.commit_version("never-generated", "create") is None


def test_revert_nothing_to_undo(mgr, monkeypatch, tmp_path):
    """T6: revert with no snapshots returns a helpful failure, never raises."""
    manager, fake = mgr
    _tmp_generator(monkeypatch, tmp_path)
    manager.create_app_record({"name": "S", "slug": "t6-noundo", "datasource_id": "kb", "metrics": []},
                              "org1", "u1")
    result = manager.revert("t6-noundo")
    assert result["success"] is False
    assert "Nothing to undo" in result["message"]


def test_revert_restores_previous_version_and_reloads(mgr, monkeypatch, tmp_path):
    """T6: revert restores the pre-change files and hot-reloads the app."""
    manager, fake = mgr
    gen = _tmp_generator(monkeypatch, tmp_path)
    gen.generate({"name": "S", "slug": "t6-revert", "datasource_id": "kb", "metrics": []})
    manager.create_app_record({"name": "S", "slug": "t6-revert", "datasource_id": "kb", "metrics": []},
                              "org1", "u1")
    manager.commit_version("t6-revert", "create")

    # Simulate an edit (as regenerate() would) after the snapshot.
    app_dir = gen.app_dir("t6-revert")
    original = (app_dir / "api.py").read_text(encoding="utf-8")
    (app_dir / "api.py").write_text(original + "\n# EDIT-MARKER\n", encoding="utf-8")
    assert "# EDIT-MARKER" in (app_dir / "api.py").read_text(encoding="utf-8")

    calls = []
    monkeypatch.setattr(manager, "_reload_module", lambda slug: calls.append(("reload", slug)))
    monkeypatch.setattr(manager, "unmount", lambda slug: calls.append(("unmount", slug)))
    monkeypatch.setattr(manager, "mount", lambda slug: calls.append(("mount", slug)))
    monkeypatch.setattr(manager, "start_poller", lambda slug: calls.append(("poller", slug)))

    result = manager.revert("t6-revert")
    assert result["success"] is True
    assert "# EDIT-MARKER" not in (app_dir / "api.py").read_text(encoding="utf-8")
    assert calls == [("reload", "t6-revert"), ("unmount", "t6-revert"),
                     ("mount", "t6-revert"), ("poller", "t6-revert")]
    assert manager.get_app("t6-revert").status == "running"
