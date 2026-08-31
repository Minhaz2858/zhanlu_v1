"""Advanced Fusion 360 granular tools — revolve, helix-swept coil, parameters, patterns.

Extends the granular (anti-hallucination) layer beyond the simple prismatic
tools (sketch/extrude/fillet/thread) to the operations a complex parametric
model needs — shock absorbers, coil springs, housings, mounts, flanges.

Each tool builds a VERIFIED adsk Python snippet internally (units: mm → cm)
and runs it over the same socket bridge as ``fusion360_execute_python``. The
agent composes typed parameters — it never writes raw adsk for these ops.

Verified live against Fusion 360 (2026-08-18): helix-swept coil, revolve,
user parameters (incl. expressions like ``40 + stroke_pos * 110``), and
circular pattern all produce correct geometry.

Units note: every ``*_mm`` parameter is MILLIMETRES and converted to cm
internally (the Fusion API is cm). ``UserParameter.value`` is returned in cm.
"""

from __future__ import annotations

from app.services.tool_registry import registry
from app.services.tool_handlers.fusion360_granular import (
    _apply_component,
    _bad,
    _last_int,
    _run,
    _schema,
)

MM = "def mm(v):\n    return adsk.core.ValueInput.createByReal(v / 10.0)\n"

_OPERATIONS = {
    "new": "adsk.fusion.FeatureOperations.NewBodyFeatureOperation",
    "join": "adsk.fusion.FeatureOperations.JoinFeatureOperation",
    "cut": "adsk.fusion.FeatureOperations.CutFeatureOperation",
    "intersect": "adsk.fusion.FeatureOperations.IntersectFeatureOperation",
}

# The root component's default construction axes are stable properties (NOT
# members of root.constructionAxes — verified live), so they are always safe
# to use as revolve/pattern axes and survive fusion360_clear.
_AXES = {
    "x": "root.xConstructionAxis",
    "y": "root.yConstructionAxis",
    "z": "root.zConstructionAxis",
}


# ---------------------------------------------------------------------------
# 1. user_parameter
# ---------------------------------------------------------------------------
def _fusion360_user_parameter(args, db, user_id):
    name = (args.get("name") or "").strip()
    if not name:
        return _bad("name is required")
    if "'" in name:
        return _bad("name must not contain a single quote")
    value = args.get("value")
    if value is None:
        return _bad("value is required (a number, or an expression string like '40 + stroke_pos * 110')")
    units = (args.get("units", "mm") or "")
    if "'" in str(value):
        return _bad("value must not contain a single quote")

    if isinstance(value, (int, float)):
        expr = str(value)
    else:
        expr = str(value).strip()

    code = (
        "import adsk.core, adsk.fusion\n"
        + f"up = design.userParameters\n"
        + f"p = up.itemByName('{name}')\n"
        + "if p:\n"
        + f"    p.expression = '{expr}'\n"
        + f"    print('PARAM_UPDATED', '{name}', p.expression)\n"
        + "else:\n"
        + f"    p = up.add('{name}', adsk.core.ValueInput.createByString('{expr}'), '{units}', '')\n"
        + f"    print('PARAM_CREATED', '{name}', p.expression)\n"
        + f"print('PARAM_VALUE', '{name}', p.value)\n"
    )
    return _run(_apply_component(code, args.get("component_index")), db)


# ---------------------------------------------------------------------------
# 2. revolve
# ---------------------------------------------------------------------------
def _fusion360_revolve(args, db, user_id):
    si = args.get("sketch_index")
    if si is None:
        return _bad("sketch_index is required")
    axis = args.get("axis", "z")
    if axis not in _AXES:
        return _bad(f"axis must be one of {sorted(_AXES)}")
    angle = args.get("angle_deg", 360)
    operation = args.get("operation", "new")
    if operation not in _OPERATIONS:
        return _bad(f"operation must be one of {sorted(_OPERATIONS)}")
    profile = int(args.get("profile_index", 0) or 0)
    op = _OPERATIONS[operation]

    if isinstance(angle, str):
        # parameter-name string -> createByString (parameter must be in deg)
        angle_vi = f"adsk.core.ValueInput.createByString('{angle.strip().replace(chr(39), '')}')"
    else:
        angle_vi = f"adsk.core.ValueInput.createByString('{float(angle)} deg')"

    code = (
        MM
        + f"sk = root.sketches.item({int(si)})\n"
        + f"rin = root.features.revolveFeatures.createInput(sk.profiles.item({profile}), {_AXES[axis]}, {op})\n"
        + f"rin.setAngleExtent(False, {angle_vi})\n"
        + "root.features.revolveFeatures.add(rin)\n"
        + "print('BODY_INDEX', root.bRepBodies.count - 1)\n"
    )
    code = _apply_component(code, args.get("component_index"))
    r = _run(code, db)
    if not r["success"]:
        return r
    return {"success": True, "body_index": _last_int(r["stdout"], "BODY_INDEX"), "stdout": r["stdout"]}


# ---------------------------------------------------------------------------
# 3. coil — helix-swept spring (Fusion's Coil feature is NOT scriptable)
# ---------------------------------------------------------------------------
def _fusion360_coil(args, db, user_id):
    mean_dia = float(args.get("mean_dia_mm", 0) or 0)
    wire_dia = float(args.get("wire_dia_mm", 0) or 0)
    coils = float(args.get("coils", 6) or 6)
    pitch = float(args.get("pitch_mm", 0) or 0)
    z_start = float(args.get("z_start_mm", 0) or 0)
    direction = args.get("direction", "pos")
    if direction not in ("pos", "neg"):
        return _bad("direction must be 'pos' (+Z) or 'neg' (-Z)")
    if mean_dia <= 0 or wire_dia <= 0 or coils <= 0 or pitch <= 0:
        return _bad("mean_dia_mm, wire_dia_mm, coils, pitch_mm must all be > 0")
    if pitch <= wire_dia:
        return _bad(f"pitch_mm ({pitch}) must be > wire_dia_mm ({wire_dia}) — coils would self-intersect")

    sign = -1.0 if direction == "neg" else 1.0
    R = mean_dia / 2.0 / 10.0
    r = wire_dia / 2.0 / 10.0
    p = pitch / 10.0
    steps = int(coils * 24) + 1
    z0 = z_start / 10.0  # helix CENTERLINE start (bottom of wire = z0 - r)

    code = (
        MM
        + "import math\n"
        + f"R, r, p, coils, steps, sgn, z0 = {R}, {r}, {p}, {coils}, {steps}, {sign}, {z0}\n"
        + "sk = root.sketches.add(root.xYConstructionPlane)\n"
        + "sk.isComputeDeferred = True\n"
        + "pts = adsk.core.ObjectCollection.create()\n"
        + "for i in range(steps):\n"
        + "    t = (i / float(steps - 1)) * coils * 2.0 * math.pi\n"
        + "    pts.add(adsk.core.Point3D.create(R * math.cos(t), R * math.sin(t), z0 + sgn * (t / (2.0 * math.pi)) * p))\n"
        + "sk.sketchCurves.sketchFittedSplines.add(pts)\n"
        + "sk.isComputeDeferred = False\n"
        + "spline = sk.sketchCurves.sketchFittedSplines.item(0)\n"
        + "path = adsk.fusion.Path.create(spline, adsk.fusion.ChainedCurveOptions.connectedChainedCurves)\n"
        + "pin = root.constructionPlanes.createInput()\n"
        + "pin.setByDistanceOnPath(path, adsk.core.ValueInput.createByReal(0))\n"
        + "cplane = root.constructionPlanes.add(pin)\n"
        + "sk2 = root.sketches.add(cplane)\n"
        + "sk2.sketchCurves.sketchCircles.addByCenterRadius(adsk.core.Point3D.create(0, 0, 0), r)\n"
        + "swin = root.features.sweepFeatures.createInput(sk2.profiles.item(0), path, adsk.fusion.FeatureOperations.NewBodyFeatureOperation)\n"
        + "root.features.sweepFeatures.add(swin)\n"
        + "print('BODY_INDEX', root.bRepBodies.count - 1)\n"
    )
    code = _apply_component(code, args.get("component_index"))
    r = _run(code, db)
    if not r["success"]:
        return r
    return {"success": True, "body_index": _last_int(r["stdout"], "BODY_INDEX"), "stdout": r["stdout"]}


# ---------------------------------------------------------------------------
# 4. circular_pattern — N copies of a body about an axis
# ---------------------------------------------------------------------------
def _fusion360_circular_pattern(args, db, user_id):
    bi = args.get("body_index")
    if bi is None:
        return _bad("body_index is required (the body to pattern)")
    count = int(args.get("count", 3) or 3)
    axis = args.get("axis", "z")
    if axis not in _AXES:
        return _bad(f"axis must be one of {sorted(_AXES)}")
    if count < 2:
        return _bad("count must be >= 2")

    code = (
        MM
        + f"body = root.bRepBodies.item({int(bi)})\n"
        + "coll = adsk.core.ObjectCollection.create()\n"
        + "coll.add(body)\n"
        + f"pin = root.features.circularPatternFeatures.createInput(coll, {_AXES[axis]})\n"
        + f"pin.quantity = adsk.core.ValueInput.createByReal({count})\n"
        + "pin.totalAngle = adsk.core.ValueInput.createByString('360 deg')\n"
        + "root.features.circularPatternFeatures.add(pin)\n"
        + "print('BODY_COUNT', root.bRepBodies.count)\n"
    )
    code = _apply_component(code, args.get("component_index"))
    r = _run(code, db)
    if not r["success"]:
        return r
    return {"success": True, "body_count": _last_int(r["stdout"], "BODY_COUNT"), "stdout": r["stdout"]}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
_COMMON = "Units are MILLIMETRES for all *_mm params. The bridge converts to Fusion's cm internally."

registry.register(
    name="fusion360_user_parameter",
    schema=_schema(
        "fusion360_user_parameter",
        "Create or update a named user parameter. value may be a number OR an expression string "
        "referencing other parameters (e.g. '40 + stroke_pos * 110'). Units default to 'mm'; use "
        "units='' for a unitless factor (e.g. stroke_pos). Updating an existing name re-evaluates "
        "all features that reference it by name (parametric rebuild).",
        {
            "name": {"type": "string", "description": "Parameter name (no spaces/single-quotes)."},
            "value": {"type": "string", "description": "Number or expression string, e.g. 12.5 or '40 + stroke_pos * 110'."},
            "units": {"type": "string", "description": "'mm' (default), 'deg', or '' for unitless."},
        },
        ["name", "value"],
    ),
    handler=_fusion360_user_parameter,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Create/update a named user parameter (supports expressions).", emoji="🔢",
)

registry.register(
    name="fusion360_revolve",
    schema=_schema(
        "fusion360_revolve",
        f"Revolve a sketch profile about the X/Y/Z axis into a solid (cylinders, tubes, tapered bodies). "
        f"The profile must be a closed region drawn in a sketch on a plane CONTAINING the axis. {_COMMON}",
        {
            "sketch_index": {"type": "integer", "description": "From fusion360_sketch_create."},
            "profile_index": {"type": "integer", "description": "Usually 0."},
            "axis": {"type": "string", "enum": ["x", "y", "z"], "description": "Axis to revolve about."},
            "angle_deg": {"type": ["number", "string"], "description": "Revolution angle in degrees (360 = full), or a parameter-name string (parameter must be in deg)."},
            "operation": {"type": "string", "enum": ["new", "join", "cut", "intersect"]},
        },
        ["sketch_index"],
    ),
    handler=_fusion360_revolve,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Revolve a sketch profile about an axis; returns body_index.", emoji="🔄",
)

registry.register(
    name="fusion360_coil",
    schema=_schema(
        "fusion360_coil",
        f"Build a coil spring as a swept solid (helix path + circular wire). This is how you make "
        f"springs — Fusion's Coil feature is NOT scriptable, so this does the 3D-sketch spline + "
        f"sweep for you. mean_dia_mm = spring mean (centerline) diameter; wire_dia_mm = wire "
        f"diameter; coils = number of active coils; pitch_mm = center-to-center coil spacing "
        f"(MUST be > wire_dia_mm); z_start_mm = Z of the first coil's centerline. {_COMMON}",
        {
            "mean_dia_mm": {"type": "number"},
            "wire_dia_mm": {"type": "number"},
            "coils": {"type": "number", "description": "Active coil count (e.g. 6)."},
            "pitch_mm": {"type": "number", "description": "Coil spacing, center-to-center. Must be > wire_dia_mm."},
            "z_start_mm": {"type": "number", "description": "Z of the first coil centerline (mm)."},
            "direction": {"type": "string", "enum": ["pos", "neg"], "description": "Helix handedness along Z (pos = up)."},
        },
        ["mean_dia_mm", "wire_dia_mm", "coils", "pitch_mm"],
    ),
    handler=_fusion360_coil,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Build a helix-swept coil spring; returns body_index.", emoji="🌀",
)

registry.register(
    name="fusion360_circular_pattern",
    schema=_schema(
        "fusion360_circular_pattern",
        f"Create N equally-spaced copies of a body around the X/Y/Z axis (e.g. bolt-circle studs). "
        f"count includes the original body. {_COMMON}",
        {
            "body_index": {"type": "integer", "description": "The body to copy (from the last extrude/revolve result)."},
            "count": {"type": "integer", "description": "Total instances including the original (e.g. 3)."},
            "axis": {"type": "string", "enum": ["x", "y", "z"]},
        },
        ["body_index", "count"],
    ),
    handler=_fusion360_circular_pattern,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Circular-pattern a body about an axis; returns total body count.", emoji="🔁",
)
