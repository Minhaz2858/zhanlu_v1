"""cad_revision_create / cad_revision_release tools (roadmap P3)."""
import pytest

from app.services.tool_handlers import cad_revision as cr


# ── validation ────────────────────────────────────────────────────────────

def test_create_requires_change_summary():
    out = cr._cad_revision_create({"before_measurements": {}, "after_measurements": {}}, None, "u")
    assert not out["success"] and "change_summary" in out["error"]


def test_create_requires_after_measurements():
    out = cr._cad_revision_create({"change_summary": "thk 4→5"}, None, "u")
    assert not out["success"] and "after_measurements" in out["error"]


# ── impact report in create ───────────────────────────────────────────────

def test_create_builds_impact_report():
    out = cr._cad_revision_create({
        "change_summary": "thickness 4→5mm",
        "before_measurements": {"mass_g": 500.0, "wall_thickness_mm": 4.0},
        "after_measurements": {"mass_g": 540.0, "wall_thickness_mm": 5.0},
        "before_dfm": {"verdict": "pass", "issues": []},
        "after_dfm": {"verdict": "pass", "issues": []},
    }, None, "u")
    assert out["success"] is True
    rep = out["impact_report"]
    assert {c["metric"] for c in rep["changed"]} == {"mass_g", "wall_thickness_mm"}
    assert out["status"] == "pending_approval"
    assert "approved" in out["message"].lower()


def test_create_flags_dfm_degradation():
    out = cr._cad_revision_create({
        "change_summary": "shrunk wall",
        "before_measurements": {"mass_g": 500.0},
        "after_measurements": {"mass_g": 300.0},
        "before_dfm": {"verdict": "pass", "issues": []},
        "after_dfm": {"verdict": "fail",
                      "issues": [{"rule": "min_wall_thickness", "severity": "error"}]},
    }, None, "u")
    assert out["impact_report"]["requires_attention"] is True
    assert "warning" in out["message"].lower()


# ── release approval gate ─────────────────────────────────────────────────

def _rev_row():
    """Stub db: the handler only needs add/commit/flush + query for rev_no."""
    rows = []

    class Q:
        def filter_by(self, **kw):
            return self

        def count(self):
            return 0

        def all(self):
            return []

    class Db:
        def add(self, r):
            rows.append(r)

        def commit(self):
            pass

        def flush(self):
            pass

        def query(self, model):
            return Q()

        def get(self, model, id):
            return rows[0] if rows else None

        def refresh(self, r):
            pass

    return Db(), rows


def test_release_requires_user_approved():
    db, rows = _rev_row()
    created = cr._cad_revision_create({
        "change_summary": "thk", "before_measurements": {},
        "after_measurements": {"mass_g": 500.0}}, db, "u")
    rev_id = created["revision_id"]
    out = cr._cad_revision_release({"revision_id": rev_id}, db, "u")
    assert not out["success"] and "explicit" in out["error"].lower()


def test_release_with_approval_releases():
    db, rows = _rev_row()
    created = cr._cad_revision_create({
        "change_summary": "thk", "before_measurements": {},
        "after_measurements": {"mass_g": 500.0}}, db, "u")
    rev_id = created["revision_id"]
    out = cr._cad_revision_release(
        {"revision_id": rev_id, "user_approved": True, "approver_note": "ok"}, db, "u")
    assert out["success"] is True
    assert rows[-1].status == "released"


def test_release_unknown_revision():
    db, rows = _rev_row()
    out = cr._cad_revision_release({"revision_id": "nope", "user_approved": True}, db, "u")
    assert not out["success"]


# ── registration ──────────────────────────────────────────────────────────

def test_registered_in_tool_registry():
    import app.services.tool_handlers.cad_revision  # noqa: F401
    from app.services.tool_registry import registry
    assert "cad_revision_create" in registry.list_names()
    assert "cad_revision_release" in registry.list_names()
