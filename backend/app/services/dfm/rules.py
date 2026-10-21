"""Deterministic DFM (Design-for-Manufacturability) rule engine — roadmap P1.

Pure functions over MEASURED values (no LLM, no Fusion calls). Measurements
come from `fusion360_probe` / `fusion360_physical_properties` / geometry info
the agent already collected. Every rule emits the issue→reason→repair
contract from the operational roadmap:

    {rule, severity: error|warning|info, measured, required, reason,
     repair: {option, ...} | null, auto_fixable: bool}

Severity semantics:
- error   → cannot be manufactured as specified on the named process
- warning → manufacturable but costly/risky (extra setups, tool length, …)
- info    → advisory

Missing measurements SKIP their rule (we never invent values to check).
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Material profiles (seed values; per-tenant overrides come later)
# ---------------------------------------------------------------------------
MATERIALS: dict[str, dict] = {
    "6061-t6": {
        "name": "6061-t6",
        "display": "6061-T6 aluminum",
        "density_g_cm3": 2.70,
        "min_wall_mm": 1.0,
        "stock_thicknesses_mm": [1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0],
        "min_bend_radius_factor": 1.0,  # × thickness, air bending
    },
    "5052-h32": {
        "name": "5052-h32",
        "display": "5052-H32 aluminum (sheet)",
        "density_g_cm3": 2.68,
        "min_wall_mm": 0.8,
        "stock_thicknesses_mm": [0.8, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0],
        "min_bend_radius_factor": 1.0,
    },
    "mild-steel": {
        "name": "mild-steel",
        "display": "Mild steel (S235/A36)",
        "density_g_cm3": 7.85,
        "min_wall_mm": 1.0,
        "stock_thicknesses_mm": [1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0],
        "min_bend_radius_factor": 1.2,
    },
    "304-stainless": {
        "name": "304-stainless",
        "display": "304 stainless steel",
        "density_g_cm3": 8.00,
        "min_wall_mm": 1.0,
        "stock_thicknesses_mm": [1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0],
        "min_bend_radius_factor": 1.5,
    },
}

_GENERIC: dict = {
    "name": "generic",
    "display": "generic (unknown material)",
    "density_g_cm3": 2.70,
    "min_wall_mm": 1.0,
    "stock_thicknesses_mm": [1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0],
    "min_bend_radius_factor": 1.5,
}

PROCESSES = ("cnc", "sheet_metal")


def get_material(name: str | None) -> dict:
    key = (name or "").strip().lower()
    return MATERIALS.get(key, _GENERIC)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _issue(rule: str, severity: str, measured, required, reason: str,
           repair: dict | None = None, auto_fixable: bool = False) -> dict:
    return {
        "rule": rule,
        "severity": severity,
        "measured": str(measured),
        "required": str(required),
        "reason": reason,
        "repair": repair,
        "auto_fixable": auto_fixable,
    }


def _num(m: dict, key: str):
    v = m.get(key)
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


# ---------------------------------------------------------------------------
# CNC rules
# ---------------------------------------------------------------------------
def _cnc_rules(m: dict, mat: dict) -> tuple[list[dict], int]:
    issues: list[dict] = []
    checked = 0

    # 1. internal corner radius vs tool radius
    corner = _num(m, "pocket_corner_radius_mm")
    tool_d = _num(m, "tool_diameter_mm")
    if corner is not None and tool_d is not None:
        checked += 1
        min_r = tool_d / 2.0
        if corner < min_r - 1e-9:
            issues.append(_issue(
                "cnc_internal_corner_radius", "error",
                f"{corner}mm", f">= {min_r}mm",
                (f"A {tool_d}mm endmill cannot clear a {corner}mm internal "
                 "corner — the tool leaves its own radius in every pocket corner."),
                {"option": "increase_radius", "value_mm": min_r,
                 "effect": "pocket corners match the tool; no extra op needed"},
                auto_fixable=True,
            ))

    # 2. pocket depth : tool diameter ratio (3x warn, 4x error)
    depth = _num(m, "pocket_depth_mm")
    if depth is not None and tool_d is not None and tool_d > 0:
        checked += 1
        ratio = depth / tool_d
        if ratio > 4.0:
            issues.append(_issue(
                "cnc_pocket_depth_ratio", "error",
                f"{ratio:.1f}x tool dia", "<= 4x tool dia",
                (f"Pocket depth {depth}mm with a {tool_d}mm tool needs extended-reach "
                 "tooling or a 5-axis operation; chip evacuation and rigidity degrade "
                 "beyond 4x tool diameter."),
                {"option": "reduce_depth_or_widen_tool",
                 "effect": "keeps the part on a standard 3-axis op"},
            ))
        elif ratio > 3.0:
            issues.append(_issue(
                "cnc_pocket_depth_ratio", "warning",
                f"{ratio:.1f}x tool dia", "<= 3x tool dia (comfortable)",
                "Deep-pocket machining: expect slower feeds and possible tool-length "
                "upgrades.",
                None,
            ))

    # 3. hole-to-edge distance
    hole_edge = _num(m, "hole_edge_distance_mm")
    hole_d = _num(m, "hole_diameter_mm")
    if hole_edge is not None and hole_d is not None:
        checked += 1
        min_dist = hole_d / 2.0 + mat["min_wall_mm"]
        if hole_edge < min_dist - 1e-9:
            issues.append(_issue(
                "cnc_hole_to_edge", "error",
                f"{hole_edge}mm", f">= {min_dist}mm",
                (f"Hole edge is {hole_edge}mm from the part edge; a Ø{hole_d}mm hole "
                 f"needs {hole_d / 2}mm + {mat['min_wall_mm']}mm wall to avoid breakout "
                 "and burr formation on the outer edge."),
                {"option": "move_hole_inward", "value_mm": min_dist},
                auto_fixable=True,
            ))

    # 4. wall thickness
    wall = _num(m, "wall_thickness_mm")
    if wall is not None:
        checked += 1
        if wall < mat["min_wall_mm"]:
            issues.append(_issue(
                "min_wall_thickness", "error",
                f"{wall}mm", f">= {mat['min_wall_mm']}mm",
                (f"Wall of {wall}mm will chatter/vibrate during machining and distort "
                 f"after clamping release; {mat['display']} needs at least "
                 f"{mat['min_wall_mm']}mm."),
                {"option": "increase_wall", "value_mm": mat["min_wall_mm"]},
                auto_fixable=True,
            ))

    return issues, checked


# ---------------------------------------------------------------------------
# Sheet-metal rules
# ---------------------------------------------------------------------------
def _sheet_metal_rules(m: dict, mat: dict) -> tuple[list[dict], int]:
    issues: list[dict] = []
    checked = 0
    t = _num(m, "material_thickness_mm")
    bend_r = _num(m, "bend_radius_mm")

    # 1. bend radius >= factor × thickness
    if t is not None and bend_r is not None:
        checked += 1
        min_r = round(t * mat["min_bend_radius_factor"], 3)
        if bend_r < min_r - 1e-9:
            issues.append(_issue(
                "sm_bend_radius", "error",
                f"{bend_r}mm", f">= {min_r}mm",
                (f"Bending {mat['display']} at {t}mm thickness with a {bend_r}mm inside "
                 f"radius cracks/stretch marks the outer surface; the minimum is "
                 f"{mat['min_bend_radius_factor']}× thickness = {min_r}mm."),
                {"option": "increase_bend_radius", "value_mm": min_r},
                auto_fixable=True,
            ))

    # 2. minimum flange length
    flange = _num(m, "flange_length_mm")
    if flange is not None and t is not None:
        checked += 1
        # press-brake rule: flange must reach past the die edge;
        # conservative: bend_r + 3×t
        min_fl = round((bend_r if bend_r is not None else t) + 3.0 * t, 3)
        if flange < min_fl - 1e-9:
            issues.append(_issue(
                "sm_min_flange_length", "error",
                f"{flange}mm", f">= {min_fl}mm",
                (f"Flange of {flange}mm is too short to grip in the press-brake die "
                 f"(needs ~{min_fl}mm at {t}mm thickness); the bend will slide or deform."),
                {"option": "increase_flange", "value_mm": min_fl},
                auto_fixable=True,
            ))

    # 3. hole-to-bend distance
    hole_bend = _num(m, "hole_to_bend_mm")
    hole_d = _num(m, "hole_diameter_mm")
    if hole_bend is not None and t is not None:
        checked += 1
        br = bend_r if bend_r is not None else mat["min_bend_radius_factor"] * t
        hd = (hole_d if hole_d is not None else 0.0) / 2.0
        min_hb = round(br + 2.5 * t + hd, 3)
        if hole_bend < min_hb - 1e-9:
            issues.append(_issue(
                "sm_hole_to_bend", "error",
                f"{hole_bend}mm", f">= {min_hb}mm",
                (f"A hole {hole_bend}mm from the bend line deforms during bending "
                 f"(material stretches inside the deformation zone); keep at least "
                 f"{min_hb}mm (radius + 2.5×thickness + hole radius)."),
                {"option": "move_hole_away_from_bend", "value_mm": min_hb},
                auto_fixable=True,
            ))

    # 4. thickness must exist in stock
    if t is not None:
        checked += 1
        stock = mat.get("stock_thicknesses_mm") or []
        if stock and not any(abs(t - s) < 1e-6 for s in stock):
            issues.append(_issue(
                "sm_stock_thickness", "error",
                f"{t}mm", f"one of {stock}",
                (f"{t}mm sheet is not a stocked thickness for {mat['display']}; "
                 "special-order sheet adds lead time and minimum-order cost."),
                {"option": "use_stock_thickness", "value_mm": stock},
            ))

    return issues, checked


_RULES_BY_PROCESS = {
    "cnc": _cnc_rules,
    "sheet_metal": _sheet_metal_rules,
}


# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------
def run_dfm_check(measurements: dict, process: str, material: str | None = None) -> dict:
    """Run the process's DFM rules over ``measurements``.

    Returns {process, material, issues, checked_rules, verdict} where verdict
    is "pass" | "pass_with_warnings" | "fail". Raises ValueError for an
    unknown process (caller maps that to a retryable tool error).
    """
    proc = (process or "").strip().lower()
    fn = _RULES_BY_PROCESS.get(proc)
    if fn is None:
        raise ValueError(f"unknown process: {process!r} (supported: {sorted(_RULES_BY_PROCESS)})")
    mat = get_material(material)
    m = measurements if isinstance(measurements, dict) else {}
    issues, checked = fn(m, mat)
    if any(i["severity"] == "error" for i in issues):
        verdict = "fail"
    elif any(i["severity"] == "warning" for i in issues):
        verdict = "pass_with_warnings"
    else:
        verdict = "pass"
    return {
        "process": proc,
        "material": mat["name"],
        "issues": issues,
        "checked_rules": checked,
        "verdict": verdict,
    }
