"""DFM rule engine (roadmap P1) — deterministic manufacturability rules.

Pure functions over measured values (from fusion360_probe / physical
properties / tool-call history). No LLM. Rule output:
{rule, severity: error|warning|info, measured, required, reason, repair,
 auto_fixable}.
"""
import pytest

from app.services.dfm.rules import (
    MATERIALS,
    get_material,
    run_dfm_check,
)


# ── material profiles ─────────────────────────────────────────────────────

def test_known_material_profiles_exist():
    for name in ("6061-t6", "5052-h32", "mild-steel", "304-stainless"):
        assert name in MATERIALS
        p = MATERIALS[name]
        assert p["density_g_cm3"] > 0
        assert p["min_wall_mm"] > 0
        assert p["stock_thicknesses_mm"]


def test_unknown_material_falls_back_to_generic():
    p = get_material("unobtanium-9000")
    assert p["name"] == "generic"
    # still usable by rules
    issues = run_dfm_check({"wall_thickness_mm": 0.1}, "cnc", "unobtanium-9000")
    assert any(i["rule"] == "min_wall_thickness" for i in issues["issues"])


# ── CNC rules ─────────────────────────────────────────────────────────────

def test_cnc_clean_part_passes():
    m = {
        "pocket_corner_radius_mm": 3.0,
        "tool_diameter_mm": 6.0,
        "pocket_depth_mm": 10.0,
        "hole_edge_distance_mm": 6.0,
        "hole_diameter_mm": 5.0,
        "wall_thickness_mm": 3.0,
    }
    out = run_dfm_check(m, "cnc", "6061-t6")
    assert out["verdict"] == "pass"
    assert out["issues"] == []
    assert out["checked_rules"] >= 4


def test_cnc_internal_corner_radius_smaller_than_tool_radius():
    m = {"pocket_corner_radius_mm": 1.0, "tool_diameter_mm": 6.0}
    out = run_dfm_check(m, "cnc", "6061-t6")
    issues = [i for i in out["issues"] if i["rule"] == "cnc_internal_corner_radius"]
    assert issues and issues[0]["severity"] == "error"
    assert issues[0]["repair"]["option"] == "increase_radius"
    assert issues[0]["auto_fixable"] is True


def test_cnc_pocket_depth_ratio_over_4x_tool():
    m = {"pocket_depth_mm": 30.0, "tool_diameter_mm": 6.0}
    out = run_dfm_check(m, "cnc", "6061-t6")
    issues = [i for i in out["issues"] if i["rule"] == "cnc_pocket_depth_ratio"]
    assert issues and issues[0]["severity"] == "error"
    assert "5-axis" in issues[0]["reason"] or "tool length" in issues[0]["reason"]


def test_cnc_pocket_depth_ratio_warn_band():
    # 3x-4x tool diameter = warning band, not error
    m = {"pocket_depth_mm": 21.0, "tool_diameter_mm": 6.0}
    out = run_dfm_check(m, "cnc", "6061-t6")
    issues = [i for i in out["issues"] if i["rule"] == "cnc_pocket_depth_ratio"]
    assert issues and issues[0]["severity"] == "warning"


def test_cnc_hole_to_edge_distance():
    # requirement: center_to_edge >= dia/2 + min_wall (1mm) = 5 for dia 8
    m = {"hole_edge_distance_mm": 3.0, "hole_diameter_mm": 8.0}
    out = run_dfm_check(m, "cnc", "6061-t6")
    issues = [i for i in out["issues"] if i["rule"] == "cnc_hole_to_edge"]
    assert issues and issues[0]["severity"] == "error"


def test_cnc_wall_thickness_below_material_min():
    m = {"wall_thickness_mm": 0.4}
    out = run_dfm_check(m, "cnc", "6061-t6")
    issues = [i for i in out["issues"] if i["rule"] == "min_wall_thickness"]
    assert issues and issues[0]["severity"] == "error"


def test_missing_measurements_skip_rules():
    out = run_dfm_check({}, "cnc", "6061-t6")
    assert out["verdict"] == "pass"
    assert out["checked_rules"] == 0


# ── sheet metal rules ─────────────────────────────────────────────────────

def test_sheet_metal_clean_part_passes():
    m = {
        "material_thickness_mm": 2.0,
        "bend_radius_mm": 2.5,
        "flange_length_mm": 12.0,
        "hole_to_bend_mm": 12.0,
        "hole_diameter_mm": 5.0,
    }
    out = run_dfm_check(m, "sheet_metal", "5052-h32")
    assert out["verdict"] == "pass"
    assert out["checked_rules"] >= 4


def test_sheet_metal_bend_radius_below_factor():
    m = {"material_thickness_mm": 2.0, "bend_radius_mm": 1.0}
    out = run_dfm_check(m, "sheet_metal", "5052-h32")
    issues = [i for i in out["issues"] if i["rule"] == "sm_bend_radius"]
    assert issues and issues[0]["severity"] == "error"
    assert issues[0]["repair"]["option"] == "increase_bend_radius"


def test_sheet_metal_flange_too_short():
    m = {"material_thickness_mm": 2.0, "bend_radius_mm": 2.0, "flange_length_mm": 5.0}
    out = run_dfm_check(m, "sheet_metal", "5052-h32")
    issues = [i for i in out["issues"] if i["rule"] == "sm_min_flange_length"]
    assert issues and issues[0]["severity"] == "error"


def test_sheet_metal_hole_too_close_to_bend():
    # required = bend_radius + 2.5*t + hole_dia/2 = 2 + 5 + 2.5 = 9.5
    m = {
        "material_thickness_mm": 2.0, "bend_radius_mm": 2.0,
        "hole_to_bend_mm": 6.0, "hole_diameter_mm": 5.0,
    }
    out = run_dfm_check(m, "sheet_metal", "5052-h32")
    issues = [i for i in out["issues"] if i["rule"] == "sm_hole_to_bend"]
    assert issues and issues[0]["severity"] == "error"


def test_sheet_metal_thickness_not_in_stock():
    m = {"material_thickness_mm": 2.3}
    out = run_dfm_check(m, "sheet_metal", "5052-h32")
    issues = [i for i in out["issues"] if i["rule"] == "sm_stock_thickness"]
    assert issues and issues[0]["severity"] == "error"
    assert issues[0]["repair"]["option"] == "use_stock_thickness"


def test_sheet_metal_thickness_in_stock_ok():
    m = {"material_thickness_mm": 2.0}
    out = run_dfm_check(m, "sheet_metal", "5052-h32")
    assert not [i for i in out["issues"] if i["rule"] == "sm_stock_thickness"]


# ── orchestration ─────────────────────────────────────────────────────────

def test_unknown_process_rejected():
    with pytest.raises(ValueError):
        run_dfm_check({}, "woodcarving", "6061-t6")


def test_verdict_fail_on_any_error():
    m = {"wall_thickness_mm": 0.4, "hole_edge_distance_mm": 0.5, "hole_diameter_mm": 8.0}
    out = run_dfm_check(m, "cnc", "6061-t6")
    assert out["verdict"] == "fail"


def test_verdict_pass_with_warnings_only():
    m = {"pocket_depth_mm": 21.0, "tool_diameter_mm": 6.0}
    out = run_dfm_check(m, "cnc", "6061-t6")
    assert out["verdict"] == "pass_with_warnings"
    assert all(i["severity"] != "error" for i in out["issues"])


def test_every_issue_has_full_contract():
    m = {"wall_thickness_mm": 0.4, "pocket_depth_mm": 30.0, "tool_diameter_mm": 6.0}
    out = run_dfm_check(m, "cnc", "6061-t6")
    for i in out["issues"]:
        assert set(i) >= {"rule", "severity", "measured", "required", "reason", "repair", "auto_fixable"}
