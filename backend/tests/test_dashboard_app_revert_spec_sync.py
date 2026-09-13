"""Spec-persistence + revert sync for full-stack dashboard apps (#9).

Undo (revert_fullstack_dashboard → manager.revert) restores files from a
filesystem snapshot. The DashboardApp.spec JSONB column must be re-synced from
the restored app dir, otherwise the DB row keeps describing the reverted-away
state (divergent /config, agent context, and next edit). The generator now
persists the full authored spec as a DOTFILE (.spec.json) inside the app dir so
snapshots carry it. Pure unit tests — no DB.
"""
import json

from app.services.dashboard_app.generator import DashboardAppGenerator, get_generator
from app.services.dashboard_app.versioning import (
    consume_latest,
    restore_snapshot,
    snapshot_app_dir,
    latest_snapshot,
)
from app.services.dashboard_app.manager import restored_spec_from_dir


MINIMAL_SPEC = {
    "name": "Spec Sync Test",
    "slug": "spec_sync_test",
    "description": "round-trip",
    "datasource_id": "kb-1",
    "design_system_ref": None,
    "theme": "light",
    "style": "standard",
    "refresh_interval_seconds": 45,
    "insights": [{"title": "Agent insight", "body": "authored"}],
    "layout": [],
    "metrics": [
        {
            "id": "m1",
            "title": "Revenue",
            "type": "kpi",
            "sql": "SELECT SUM(amount) AS total FROM sales",
            "options": {},
        },
        {
            "id": "m2",
            "title": "By product",
            "type": "bar",
            "sql": "SELECT product, SUM(amount) AS total FROM sales GROUP BY 1",
            "options": {"filters": [{"key": "region", "label": "Region", "column": "region"}]},
        },
    ],
}


def _make_generator(tmp_path):
    apps_dir = tmp_path / "apps"
    return DashboardAppGenerator(apps_dir=apps_dir), apps_dir


def test_generate_persists_spec_dotfile_next_to_config(tmp_path):
    gen, apps_dir = _make_generator(tmp_path)
    app_dir = gen.generate(MINIMAL_SPEC)
    assert app_dir.name == MINIMAL_SPEC["slug"]
    spec_file = app_dir / ".spec.json"
    assert spec_file.exists(), ".spec.json must be written at the app root"
    assert (app_dir / "config.json").exists()
    persisted = json.loads(spec_file.read_text(encoding="utf-8"))
    assert persisted == MINIMAL_SPEC
    # Raw SQL must NOT leak into the public config.json (dotfile only).
    public_config = json.loads((app_dir / "config.json").read_text(encoding="utf-8"))
    assert "SELECT SUM" not in json.dumps(public_config)


def test_snapshot_carries_spec_dotfile_and_restore_roundtrips(tmp_path):
    gen, apps_dir = _make_generator(tmp_path)
    app_dir = gen.generate(MINIMAL_SPEC)
    snapshot_app_dir(app_dir, "pre-change")
    # Mutate the live spec (simulate an accepted edit).
    edited = dict(MINIMAL_SPEC)
    edited["name"] = "EDITED NAME"
    edited["metrics"] = [dict(m) for m in MINIMAL_SPEC["metrics"]]
    gen.generate(edited)
    assert json.loads((app_dir / ".spec.json").read_text(encoding="utf-8"))["name"] == "EDITED NAME"
    # Undo: restore the pre-change snapshot; the dotfile must come back.
    snap = latest_snapshot(app_dir)
    assert snap is not None
    restore_snapshot(app_dir, snap)
    assert json.loads((app_dir / ".spec.json").read_text(encoding="utf-8"))["name"] == MINIMAL_SPEC["name"]


def test_restored_spec_from_dir_reads_dotfile(tmp_path):
    gen, apps_dir = _make_generator(tmp_path)
    app_dir = gen.generate(MINIMAL_SPEC)
    assert restored_spec_from_dir(app_dir) == MINIMAL_SPEC


def test_restored_spec_from_dir_none_when_missing(tmp_path):
    # Pre-2026-09-02 app dirs have no .spec.json → None (DB spec left untouched).
    app_dir = tmp_path / "legacy_app"
    app_dir.mkdir()
    (app_dir / "config.json").write_text("{}", encoding="utf-8")
    assert restored_spec_from_dir(app_dir) is None


def test_restored_spec_from_dir_none_when_corrupt(tmp_path):
    app_dir = tmp_path / "broken_app"
    app_dir.mkdir()
    (app_dir / ".spec.json").write_text("{not json", encoding="utf-8")
    assert restored_spec_from_dir(app_dir) is None


def test_consume_latest_full_undo_chain_keeps_dotfile(tmp_path):
    """End-to-end undo chain: generate → edit → undo → spec file follows."""
    gen, apps_dir = _make_generator(tmp_path)
    app_dir = gen.generate(MINIMAL_SPEC)
    snapshot_app_dir(app_dir, "pre-change")
    edited = dict(MINIMAL_SPEC)
    edited["name"] = "EDITED NAME"
    edited["metrics"] = [dict(m) for m in MINIMAL_SPEC["metrics"]]
    gen.generate(edited)
    # Undo consumes the snapshot and restores files.
    consumed = consume_latest(app_dir)
    assert consumed is not None
    assert json.loads((app_dir / ".spec.json").read_text(encoding="utf-8"))["name"] == MINIMAL_SPEC["name"]
