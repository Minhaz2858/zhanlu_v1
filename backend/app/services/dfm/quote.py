"""Deterministic quote calculator (roadmap P2) — the commercial wedge.

Given measured/known inputs (mass from `fusion360_physical_properties`,
feature counts from the build history) this produces a PRELIMINARY cost
estimate with an explicit assumption list. Never a binding quote: every
result carries `status="draft_pending_approval"` and a ±20% range.

Cost model:
  material  = mass_g/1000 × cost_per_kg × waste_factor(1.3)
  machining = (base_min + Σ feature_minutes) / 60 × hourly_rate
  setup     = setup_cost / quantity        (amortized per unit)
  unit      = material + machining + setup
  total     = unit × quantity              (quoted as low/mid/high, ±20%)
"""
from __future__ import annotations

# Cost catalog — seed values, USD 2026. Per-tenant overrides land with the
# pluggable-catalog phase (roadmap P4/P6); keep every number in this dict.
CATALOG: dict = {
    "currency": "USD",
    "waste_factor": 1.3,
    "range_pct": 0.20,
    "material_cost_per_kg": {
        "6061-t6": 6.0,
        "5052-h32": 5.0,
        "mild-steel": 2.0,
        "304-stainless": 9.0,
        "generic": 6.0,
    },
    "process": {
        "cnc": {"rate_per_hr": 75.0, "setup_cost": 150.0, "base_minutes": 15.0},
        "sheet_metal": {"rate_per_hr": 60.0, "setup_cost": 100.0, "base_minutes": 10.0},
    },
    "feature_minutes": {
        "cnc": {"holes": 1.5, "pockets": 8.0, "profiles": 3.0},
        "sheet_metal": {"bends": 1.0, "cutouts": 0.8, "forms": 2.0},
    },
}

_PROCESSES = tuple(CATALOG["process"].keys())


def estimate_mass_g(mass_g: float | None = None,
                    volume_cm3: float | None = None,
                    material: str | None = None) -> float:
    """Mass in grams: direct, or volume × density (P1 material profiles)."""
    if mass_g is not None:
        try:
            m = float(mass_g)
        except (TypeError, ValueError):
            raise ValueError("mass_g must be a number")
        if not (m > 0) or m != m:
            raise ValueError("mass_g must be a positive number")
        return m
    if volume_cm3 is not None:
        from app.services.dfm.rules import get_material
        density = get_material(material)["density_g_cm3"]
        try:
            v = float(volume_cm3)
        except (TypeError, ValueError):
            raise ValueError("volume_cm3 must be a number")
        if not (v > 0) or v != v:
            raise ValueError("volume_cm3 must be a positive number")
        return v * density
    raise ValueError(
        "mass is required — measure it with fusion360_physical_properties "
        "or provide volume_cm3 to estimate from the material density"
    )


def _num(v, name: str) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number")
    if f != f or f in (float("inf"), float("-inf")):
        raise ValueError(f"{name} must be a finite number")
    return f


def compute_quote(req: dict) -> dict:
    """Compute the preliminary quote from a requirement dict.

    Required: mass_g or volume_cm3, process, quantity. Optional:
    material, part_name, feature_counts {feature: count}.
    Raises ValueError with a caller-facing message on bad input.
    """
    req = req if isinstance(req, dict) else {}
    process = str(req.get("process") or "").strip().lower()
    if process not in _PROCESSES:
        raise ValueError(f"process must be one of {list(_PROCESSES)}")
    proc = CATALOG["process"][process]

    quantity = int(_num(req.get("quantity"), "quantity"))
    if quantity < 1:
        raise ValueError("quantity must be >= 1")

    material = str(req.get("material") or "generic").strip().lower()
    from app.services.dfm.rules import get_material
    mat = get_material(material)
    material_key = mat["name"] if mat["name"] in CATALOG["material_cost_per_kg"] else "generic"

    mass_g = estimate_mass_g(
        mass_g=req.get("mass_g"),
        volume_cm3=req.get("volume_cm3"),
        material=material,
    )

    # material cost per unit
    mat_unit = (mass_g / 1000.0
                * CATALOG["material_cost_per_kg"][material_key]
                * CATALOG["waste_factor"])

    # machining time per unit
    fc = req.get("feature_counts") or {}
    if not isinstance(fc, dict):
        fc = {}
    feat_minutes = CATALOG["feature_minutes"][process]
    feature_min = 0.0
    feature_breakdown = {}
    for feat, minutes in feat_minutes.items():
        count = int(_num(fc.get(feat, 0) or 0, f"feature_counts.{feat}"))
        if count > 0:
            feature_min += count * minutes
            feature_breakdown[feat] = {"count": count, "minutes": count * minutes}
    total_minutes = proc["base_minutes"] + feature_min
    mach_unit = total_minutes / 60.0 * proc["rate_per_hr"]

    setup_unit = proc["setup_cost"] / quantity
    unit_cost = mat_unit + mach_unit + setup_unit
    total_mid = unit_cost * quantity
    spread = CATALOG["range_pct"]

    assumptions = [
        (f"Estimate only — not a binding quote; range is ±{int(spread * 100)}% "
         "on the mid value."),
        (f"Material: {mat['display']} at "
         f"${CATALOG['material_cost_per_kg'][material_key]:.2f}/kg with a "
         f"{CATALOG['waste_factor']}× stock waste factor."),
        (f"{process.upper()} machining at ${proc['rate_per_hr']:.0f}/hr "
         f"({total_minutes:.0f} min/unit) plus ${proc['setup_cost']:.0f} setup "
         f"amortized over {quantity} unit(s)."),
        "Lead time, finishing (anodize/plating), tolerances and inspection are "
        "NOT included — state them separately or add surcharges explicitly.",
        "Feature times are standard minute values; complex geometry needs a "
        "CAM review before order confirmation.",
    ]

    return {
        "status": "draft_pending_approval",
        "part_name": str(req.get("part_name") or "Unnamed part"),
        "process": process,
        "material": material_key,
        "material_display": mat["display"],
        "quantity": quantity,
        "mass_g": round(mass_g, 2),
        "currency": CATALOG["currency"],
        "unit_cost": round(unit_cost, 2),
        "total_low": round(total_mid * (1 - spread), 2),
        "total_mid": round(total_mid, 2),
        "total_high": round(total_mid * (1 + spread), 2),
        "breakdown": {
            "material_per_unit": round(mat_unit, 2),
            "machining_per_unit": round(mach_unit, 2),
            "setup_per_unit": round(setup_unit, 2),
            "machining_minutes_per_unit": round(total_minutes, 1),
            "feature_breakdown": feature_breakdown,
        },
        "assumptions": assumptions,
    }
