"""Snapshot-based versioning tests (T6). Pure filesystem — no DB, no generator.

Covers snapshot_app_dir / restore_snapshot / consume_latest semantics that back
the "undo" tool (revert_fullstack_dashboard): each accepted change creates a
snapshot, and consuming the latest steps back one change at a time.
"""
from pathlib import Path

from app.services.dashboard_app.versioning import (
    VERSIONS_DIR,
    consume_latest,
    list_snapshots,
    restore_snapshot,
    snapshot_app_dir,
)


def _make_app(tmp_path, content="v1") -> Path:
    app_dir = tmp_path / "app"
    app_dir.mkdir(exist_ok=True)
    (app_dir / "api.py").write_text(content, encoding="utf-8")
    (app_dir / "config.json").write_text(f'{{"v": "{content}"}}', encoding="utf-8")
    return app_dir


def test_snapshot_missing_dir_returns_none(tmp_path):
    assert snapshot_app_dir(tmp_path / "missing", "x") is None


def test_snapshot_and_restore_roundtrip(tmp_path):
    app_dir = _make_app(tmp_path, "v1")
    snap = snapshot_app_dir(app_dir, "first")
    assert snap is not None and snap.is_dir()

    (app_dir / "api.py").write_text("v2", encoding="utf-8")
    restore_snapshot(app_dir, snap)
    assert (app_dir / "api.py").read_text(encoding="utf-8") == "v1"
    assert (app_dir / "config.json").read_text(encoding="utf-8") == '{"v": "v1"}'


def test_snapshot_skips_versions_dir_and_pycache(tmp_path):
    app_dir = _make_app(tmp_path, "v1")
    (app_dir / "__pycache__").mkdir()
    (app_dir / "x.pyc").write_text("bytecode", encoding="utf-8")
    snap = snapshot_app_dir(app_dir, "no-artifacts")
    assert not (snap / "__pycache__").exists()
    assert not (snap / "x.pyc").exists()
    assert not (snap / VERSIONS_DIR).exists()


def test_consume_latest_steps_back_one_change_at_a_time(tmp_path):
    app_dir = _make_app(tmp_path, "v1")
    snapshot_app_dir(app_dir, "initial")
    (app_dir / "api.py").write_text("v2", encoding="utf-8")
    snapshot_app_dir(app_dir, "second")
    (app_dir / "api.py").write_text("v3", encoding="utf-8")

    # Undo #1 → back to "v2" state.
    assert consume_latest(app_dir) is not None
    assert (app_dir / "api.py").read_text(encoding="utf-8") == "v2"
    # Undo #2 → back to "v1" state.
    assert consume_latest(app_dir) is not None
    assert (app_dir / "api.py").read_text(encoding="utf-8") == "v1"
    # Undo #3 → nothing left.
    assert consume_latest(app_dir) is None


def test_consume_latest_none_when_no_snapshots(tmp_path):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    assert consume_latest(app_dir) is None


def test_list_snapshots_oldest_first(tmp_path):
    app_dir = _make_app(tmp_path, "v1")
    snapshot_app_dir(app_dir, "one")
    snapshot_app_dir(app_dir, "two")
    snaps = list_snapshots(app_dir)
    assert len(snaps) == 2
    assert snaps[0].name < snaps[1].name  # seq prefix ⇒ lexicographic == chronological
