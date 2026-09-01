"""Fusion 360 sketch-curve tools — line, arc, fitted spline.

Complements the base sketch tools (circle/rectangle/polygon) with the curves
needed for arbitrary closed profiles (which feed extrude/revolve/sweep/loft).

All recipes verified live against Fusion 360 (2026-08-19). Units mm -> cm.
"""

from __future__ import annotations

from app.services.tool_registry import registry
from app.services.tool_handlers.fusion360_granular import (
    MM,
    _apply_component,
    _bad,
    _run,
    _schema,
)
from app.services.tool_handlers.fusion360_sketch_common import _CLOSE_SKETCH

_COMMON = "Units are MILLIMETRES for all *_mm params. The bridge converts to Fusion's cm internally."


# ---------------------------------------------------------------------------
# 1. line
# ---------------------------------------------------------------------------
def _fusion360_sketch_line(args, db, user_id):
    si = args.get("sketch_index")
    if si is None:
        return _bad("sketch_index is required")
    x1 = float(args.get("x1_mm", 0) or 0) / 10.0
    y1 = float(args.get("y1_mm", 0) or 0) / 10.0
    x2 = float(args.get("x2_mm", 0) or 0) / 10.0
    y2 = float(args.get("y2_mm", 0) or 0) / 10.0
    code = (
        MM
        + f"sk = root.sketches.item({int(si)})\n"
        + f"sk.sketchCurves.sketchLines.addByTwoPoints(adsk.core.Point3D.create({x1}, {y1}, 0), adsk.core.Point3D.create({x2}, {y2}, 0))\n"
        + _CLOSE_SKETCH + "\n"
        + "print('OK line')\n"
    )
    return _run(_apply_component(code, args.get("component_index")), db)


# ---------------------------------------------------------------------------
# 2. arc (center + start + sweep angle)
# ---------------------------------------------------------------------------
def _fusion360_sketch_arc(args, db, user_id):
    si = args.get("sketch_index")
    if si is None:
        return _bad("sketch_index is required")
    cx = float(args.get("cx_mm", 0) or 0) / 10.0
    cy = float(args.get("cy_mm", 0) or 0) / 10.0
    radius = float(args.get("radius_mm", 0) or 0) / 10.0
    start = float(args.get("start_deg", 0) or 0)
    sweep = float(args.get("sweep_deg", 90) or 90)
    code = (
        MM
        + "import math\n"
        + f"sk = root.sketches.item({int(si)})\n"
        + f"cx, cy, r, sdeg, swdeg = {cx}, {cy}, {radius}, {start}, {sweep}\n"
        + "srad = math.radians(sdeg)\n"
        + "sp = adsk.core.Point3D.create(cx + r * math.cos(srad), cy + r * math.sin(srad), 0)\n"
        + "sk.sketchCurves.sketchArcs.addByCenterStartSweep(adsk.core.Point3D.create(cx, cy, 0), sp, math.radians(swdeg))\n"
        + _CLOSE_SKETCH + "\n"
        + "print('OK arc')\n"
    )
    return _run(_apply_component(code, args.get("component_index")), db)


# ---------------------------------------------------------------------------
# 3. three-point arc
# ---------------------------------------------------------------------------
def _fusion360_sketch_arc_3point(args, db, user_id):
    si = args.get("sketch_index")
    if si is None:
        return _bad("sketch_index is required")
    p = args.get("points")
    if not p or len(p) != 3:
        return _bad("points must be [[x_mm,y_mm], [x_mm,y_mm], [x_mm,y_mm]] (3 points)")
    pts = []
    for pt in p:
        pts.append((float(pt[0]) / 10.0, float(pt[1]) / 10.0))
    code = (
        MM
        + f"sk = root.sketches.item({int(si)})\n"
        + "sk.sketchCurves.sketchArcs.addByThreePoints(\n"
        + f"    adsk.core.Point3D.create({pts[0][0]}, {pts[0][1]}, 0),\n"
        + f"    adsk.core.Point3D.create({pts[1][0]}, {pts[1][1]}, 0),\n"
        + f"    adsk.core.Point3D.create({pts[2][0]}, {pts[2][1]}, 0))\n"
        + _CLOSE_SKETCH + "\n"
        + "print('OK arc3')\n"
    )
    return _run(_apply_component(code, args.get("component_index")), db)


# ---------------------------------------------------------------------------
# 4. fitted spline through points
# ---------------------------------------------------------------------------
def _fusion360_sketch_spline(args, db, user_id):
    si = args.get("sketch_index")
    if si is None:
        return _bad("sketch_index is required")
    p = args.get("points")
    if not p or len(p) < 2:
        return _bad("points must be a list of >= 2 [x_mm, y_mm] pairs")
    pts = [(float(pt[0]) / 10.0, float(pt[1]) / 10.0) for pt in p]
    pt_lines = ",\n".join(f"    adsk.core.Point3D.create({x}, {y}, 0)" for x, y in pts)
    code = (
        MM
        + f"sk = root.sketches.item({int(si)})\n"
        + "pts = adsk.core.ObjectCollection.create()\n"
        + "for pt in [\n"
        + pt_lines + "\n"
        + "]:\n"
        + "    pts.add(pt)\n"
        + "sk.sketchCurves.sketchFittedSplines.add(pts)\n"
        + _CLOSE_SKETCH + "\n"
        + "print('OK spline')\n"
    )
    return _run(_apply_component(code, args.get("component_index")), db)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
registry.register(
    name="fusion360_sketch_line",
    schema=_schema("fusion360_sketch_line", f"Draw a line segment on a sketch. {_COMMON}", {"sketch_index": {"type": "integer"}, "x1_mm": {"type": "number"}, "y1_mm": {"type": "number"}, "x2_mm": {"type": "number"}, "y2_mm": {"type": "number"}}, ["sketch_index", "x1_mm", "y1_mm", "x2_mm", "y2_mm"]),
    handler=_fusion360_sketch_line,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Draw a line on a sketch.", emoji="📏",
)

registry.register(
    name="fusion360_sketch_arc",
    schema=_schema("fusion360_sketch_arc", f"Draw an arc on a sketch by center, radius, start angle and sweep angle (degrees, CCW from +X). {_COMMON}", {"sketch_index": {"type": "integer"}, "cx_mm": {"type": "number"}, "cy_mm": {"type": "number"}, "radius_mm": {"type": "number"}, "start_deg": {"type": "number"}, "sweep_deg": {"type": "number"}}, ["sketch_index", "radius_mm"]),
    handler=_fusion360_sketch_arc,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Draw a center-point arc on a sketch.", emoji="🌙",
)

registry.register(
    name="fusion360_sketch_arc_3point",
    schema=_schema("fusion360_sketch_arc_3point", f"Draw an arc through 3 points on a sketch. {_COMMON}", {"sketch_index": {"type": "integer"}, "points": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}, "description": "[[x_mm,y_mm], [x_mm,y_mm], [x_mm,y_mm]]"}}, ["sketch_index", "points"]),
    handler=_fusion360_sketch_arc_3point,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Draw a 3-point arc on a sketch.", emoji="🌉",
)

registry.register(
    name="fusion360_sketch_spline",
    schema=_schema("fusion360_sketch_spline", f"Draw a fitted spline through a list of points on a sketch. {_COMMON}", {"sketch_index": {"type": "integer"}, "points": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}, "description": "[[x_mm,y_mm], ...]"}}, ["sketch_index", "points"]),
    handler=_fusion360_sketch_spline,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Draw a fitted spline on a sketch.", emoji="〰️",
)
