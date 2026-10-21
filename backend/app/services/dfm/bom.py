"""BOM normalization + manufacturing notes (roadmap P4).

Pure functions: the agent passes component rows collected from the build
(fusion360_component / assembly info / physical properties); this module
aggregates, validates, and renders production documentation. No LLM.
"""
from __future__ import annotations


def normalize_bom_rows(components: list[dict], process: str) -> dict:
    """Aggregate component rows into a BOM.

    Duplicate (name, material) rows merge with summed quantities and masses.
    Returns {rows, totals, warnings}; raises ValueError on unusable rows.
    """
    if not isinstance(components, list) or not components:
        raise ValueError("components must be a non-empty list")
    warnings: list[str] = []
    merged: dict[tuple, dict] = {}
    order: list[tuple] = []
    for i, raw in enumerate(components):
        if not isinstance(raw, dict):
            raise ValueError(f"components[{i}] must be an object")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ValueError(f"components[{i}].name is required")
        try:
            qty = int(float(raw.get("quantity", 1)))
        except (TypeError, ValueError):
            qty = 0
        if qty < 1:
            raise ValueError(f"components[{i}].quantity must be >= 1")
        material = (str(raw.get("material")).strip().lower()
                    if raw.get("material") else None)
        mass = raw.get("mass_g")
        try:
            mass = float(mass) if mass is not None else None
        except (TypeError, ValueError):
            mass = None
        key = (name.lower(), material)
        if key not in merged:
            merged[key] = {"name": name, "material": material, "quantity": 0,
                           "mass_g": 0.0, "mass_known": False,
                           "process": raw.get("process")}
            order.append(key)
        entry = merged[key]
        entry["quantity"] += qty
        if mass is not None:
            entry["mass_g"] += mass * qty
            entry["mass_known"] = True
    rows = []
    for line_no, key in enumerate(order, start=1):
        e = merged[key]
        if e["material"] is None:
            warnings.append(f"Row '{e['name']}': material unknown — procurement "
                            "cannot price it without a material")
        if not e["mass_known"]:
            warnings.append(f"Row '{e['name']}': mass not measured — run "
                            "fusion360_physical_properties per body")
        rows.append({
            "line_no": line_no,
            "name": e["name"],
            "material": e["material"],
            "quantity": e["quantity"],
            "mass_g": round(e["mass_g"], 2) if e["mass_known"] else None,
            "process": e["process"] or process,
        })
    total_qty = sum(r["quantity"] for r in rows)
    total_mass = sum(r["mass_g"] for r in rows if r["mass_g"] is not None)
    return {
        "rows": rows,
        "totals": {"line_count": len(rows), "total_quantity": total_qty,
                   "total_mass_g": round(total_mass, 2)},
        "warnings": warnings,
    }


def mfg_notes(process: str, feature_counts: dict | None,
              dfm_result: dict | None) -> list[str]:
    """Deterministic manufacturing notes: setups, ops, finishing, DFM repairs."""
    notes: list[str] = []
    proc = (process or "").strip().lower()
    fc = feature_counts if isinstance(feature_counts, dict) else {}
    if proc == "cnc":
        notes.append("Setup 1: fixture in vise, reference face as Z0, indicate "
                     "X/Y from stock edge; machine all top-face features.")
        holes = int(fc.get("holes") or 0)
        pockets = int(fc.get("pockets") or 0)
        ops = []
        if pockets:
            ops.append(f"rough+finish {pockets} pocket(s) before drilling (burrs "
                       "from drilling contaminate pocket floors)")
        if holes:
            ops.append(f"drill/ream {holes} hole(s) with spot-drill first")
        if ops:
            notes.append("Operation order: " + "; ".join(ops) + ".")
        if int(fc.get("profiles") or 0):
            notes.append("Profile cutouts last (part loses clamping material).")
        notes.append("Deburr all edges; break sharp corners 0.2-0.5mm unless "
                     "drawing specifies otherwise.")
        notes.append("Finishing: anodize (Type II clear/black) after machining; "
                     "mask precision bores if specified.")
    elif proc == "sheet_metal":
        bends = int(fc.get("bends") or 0)
        if bends:
            notes.append(f"Bend sequence: {bends} bend(s) — bend inside flanges "
                         "first, outside/long bends last to keep the part "
                         "controllable in the press brake.")
        notes.append("Punch/laser cutouts BEFORE bending; holes inside the "
                     "deformation zone must respect hole-to-bend distance.")
        notes.append("Finishing: powder coat or clear anodize after forming; "
                     "flat-pattern check before production run.")
    else:
        notes.append(f"Process '{process}': add setups and operation order to "
                     "this note list when the process is supported.")

    if isinstance(dfm_result, dict):
        for i in (dfm_result.get("issues") or []):
            if not isinstance(i, dict):
                continue
            if i.get("severity") in ("error", "warning"):
                repair = i.get("repair") or {}
                notes.append(
                    f"DFM OUTSTANDING — {i.get('rule')} ({i.get('severity')}): "
                    f"measured {i.get('measured')}, required {i.get('required')}. "
                    f"Apply repair: {repair}."
                )
    return notes
