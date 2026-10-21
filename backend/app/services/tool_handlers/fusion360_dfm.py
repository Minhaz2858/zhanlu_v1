"""fusion360_dfm_check — deterministic manufacturability check (roadmap P1).

Thin validator + delegator to app.services.dfm.rules (pure Python, no LLM).
The agent collects measurements via fusion360_probe / info tools and passes
them here; the engine returns issue→reason→repair findings for the named
process. Results are also visible to the deterministic fallback (verdict in
the message), so the final answer quotes real manufacturability findings.
"""
from __future__ import annotations

from app.services.dfm.rules import run_dfm_check


def _bad(error: str) -> dict:
    return {"success": False, "error": error}


def _fusion360_dfm_check(args: dict, db, user_id: str | None) -> dict:
    """Run DFM rules for a manufacturing process over measured values.

    Args:
        process: "cnc" | "sheet_metal"
        measurements: {metric_name_mm: number, ...} — values collected from
            fusion360_probe / physical properties / geometry info. Missing
            keys skip their rule (never invent values).
        material: optional material key (6061-t6, 5052-h32, mild-steel,
            304-stainless; unknown → generic profile)
    """
    process = str(args.get("process") or "").strip().lower()
    if not process:
        return _bad("process is required (cnc | sheet_metal)")
    m = args.get("measurements")
    if isinstance(m, str):
        import json as _json
        try:
            m = _json.loads(m)
        except Exception:  # noqa: BLE001
            return _bad("measurements must be valid JSON when passed as a string")
    if not isinstance(m, dict):
        return _bad("measurements (object) is required")

    try:
        dfm = run_dfm_check(m, process, args.get("material"))
    except ValueError as e:
        return _bad(f"{e} — retry with a supported process")

    issues = dfm["issues"]
    n_err = sum(1 for i in issues if i["severity"] == "error")
    n_warn = sum(1 for i in issues if i["severity"] == "warning")
    if dfm["verdict"] == "fail":
        msg = (f"DFM FAIL ({dfm['process']}, {dfm['material']}): {n_err} error(s), "
               f"{n_warn} warning(s). Apply the suggested repairs, then re-run.")
    elif dfm["verdict"] == "pass_with_warnings":
        msg = (f"DFM PASS with {n_warn} warning(s) ({dfm['process']}, "
               f"{dfm['material']}) — manufacturable but costly/risky; review warnings.")
    else:
        msg = (f"DFM PASS ({dfm['process']}, {dfm['material']}): no manufacturability "
               f"issues found in {dfm['checked_rules']} rule check(s).")
    return {
        "success": True,
        "dfm": dfm,
        "message": msg,
        "prompt_hint": (
            "Report every issue with its reason and repair. For auto_fixable "
            "issues, apply the repair via fusion360_parameter_set / feature "
            "edits, then re-run fusion360_verify_build and this check."
        ),
    }


from app.services.tool_registry import registry  # noqa: E402

FUSION360_DFM_CHECK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "fusion360_dfm_check",
        "description": (
            "Run deterministic manufacturability (DFM) rules for a process "
            "(cnc | sheet_metal) over measured geometry values. Pass ONLY "
            "values you actually measured (fusion360_probe, physical "
            "properties, feature info) — missing keys skip their rule. "
            "Returns issue→reason→repair findings and a verdict "
            "(pass | pass_with_warnings | fail). Run after "
            "fusion360_verify_build passes."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "process": {
                    "type": "string",
                    "enum": ["cnc", "sheet_metal"],
                    "description": "Manufacturing process to check against.",
                },
                "measurements": {
                    "type": "object",
                    "description": (
                        "Measured values in mm. CNC keys: "
                        "pocket_corner_radius_mm, tool_diameter_mm, pocket_depth_mm, "
                        "hole_edge_distance_mm, hole_diameter_mm, wall_thickness_mm. "
                        "Sheet-metal keys: material_thickness_mm, bend_radius_mm, "
                        "flange_length_mm, hole_to_bend_mm, hole_diameter_mm."
                    ),
                },
                "material": {
                    "type": "string",
                    "description": "Material key: 6061-t6, 5052-h32, mild-steel, 304-stainless (unknown → generic profile).",
                },
            },
            "required": ["process", "measurements"],
        },
    },
}

registry.register(
    name="fusion360_dfm_check",
    schema=FUSION360_DFM_CHECK_SCHEMA,
    handler=_fusion360_dfm_check,
    category="cad",
    toolset="cad",
    enabled_by_default=True,
    description=(
        "Deterministic DFM check (cnc | sheet_metal) over measured values; "
        "issue→reason→repair findings with a pass/warn/fail verdict."
    ),
    emoji="🔧",
)
