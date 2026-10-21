"""Impact-diff engine (roadmap P3) — deterministic before/after comparison.

Pure functions over measurement dicts + DFM results. Never invents values:
only compares what both sides actually measured.
"""
import pytest

from app.services.dfm.impact import compute_impact


def _dfm(verdict, rules):
    return {"verdict": verdict,
            "issues": [{"rule": r, "severity": "error"} for r in rules]}


def test_changed_metrics_with_delta():
    out = compute_impact(
        before={"mass_g": 500.0, "wall_thickness_mm": 4.0},
        after={"mass_g": 540.0, "wall_thickness_mm": 5.0},
    )
    changed = {c["metric"]: c for c in out["changed"]}
    assert changed["mass_g"]["delta_pct"] == pytest.approx(8.0)
    assert changed["wall_thickness_mm"]["delta_pct"] == pytest.approx(25.0)
    assert out["unchanged"] == []


def test_unchanged_and_missing_sides_ignored():
    out = compute_impact(
        before={"mass_g": 500.0, "bbox_z_mm": 40.0},
        after={"mass_g": 500.0},  # bbox_z not measured after → not compared
    )
    assert out["changed"] == []
    assert "mass_g" in out["unchanged"]
    assert "bbox_z_mm" not in out["unchanged"]  # missing on one side


def test_dfm_new_and_resolved_errors():
    out = compute_impact(
        before={"mass_g": 500.0},
        after={"mass_g": 540.0},
        before_dfm=_dfm("pass", []),
        after_dfm=_dfm("fail", ["min_wall_thickness"]),
    )
    assert out["dfm"]["errors_new"] == ["min_wall_thickness"]
    assert out["dfm"]["errors_resolved"] == []
    assert out["dfm"]["verdict_before"] == "pass"
    assert out["dfm"]["verdict_after"] == "fail"


def test_dfm_resolved_errors():
    out = compute_impact(
        before={}, after={},
        before_dfm=_dfm("fail", ["min_wall_thickness"]),
        after_dfm=_dfm("pass", []),
    )
    assert out["dfm"]["errors_resolved"] == ["min_wall_thickness"]
    assert out["dfm"]["errors_new"] == []


def test_verdict_transition_flagged():
    out = compute_impact(
        before={}, after={},
        before_dfm=_dfm("pass", []),
        after_dfm=_dfm("fail", ["sm_bend_radius"]),
    )
    assert out["requires_attention"] is True
    assert any("DFM verdict degraded" in s for s in out["summary"])


def test_clean_change_no_attention():
    out = compute_impact(
        before={"mass_g": 500.0}, after={"mass_g": 501.0},
        before_dfm=_dfm("pass", []), after_dfm=_dfm("pass", []),
    )
    assert out["requires_attention"] is False
    assert out["dfm"]["errors_new"] == []


def test_no_dfm_results_still_works():
    out = compute_impact(before={"mass_g": 1.0}, after={"mass_g": 2.0})
    assert out["dfm"] is None
