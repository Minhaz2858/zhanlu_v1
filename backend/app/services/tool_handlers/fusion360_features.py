"""Fusion 360 feature + primitive tools — the second half of the granular layer.

Adds the operations the base granular/advanced sets don't cover: quick
primitives (box/cylinder/sphere/torus), sweep/loft/shell/chamfer/hole,
rectangular pattern, boolean combine, construction planes, and body move.

Every recipe here was verified live against Fusion 360 (2026-08-19). Each tool
builds a VERIFIED adsk Python snippet internally and runs it over the same
socket bridge as ``fusion360_execute_python``. Units: mm -> cm internally.

``component_index`` (inherited from the shared ``_schema``) lets any of these
operate inside a component returned by ``fusion360_component``.
"""

from __future__ import annotations

from app.services.tool_registry import registry
from app.services.tool_handlers.fusion360_granular import (
    MM,
    _PLANES,
    _apply_component,
    _bad,
    _last_int,
    _mm_expr,
    _run,
    _schema,
)

_OPERATIONS = {
    "new": "adsk.fusion.FeatureOperations.NewBodyFeatureOperation",
    "join": "adsk.fusion.FeatureOperations.JoinFeatureOperation",
    "cut": "adsk.fusion.FeatureOperations.CutFeatureOperation",
    "intersect": "adsk.fusion.FeatureOperations.IntersectFeatureOperation",
}

_AXES = {
    "x": "root.xConstructionAxis",
    "y": "root.yConstructionAxis",
    "z": "root.zConstructionAxis",
}

_COMMON = "Units are MILLIMETRES for all *_mm params. The bridge converts to Fusion's cm internally."


def _offset_plane(base: str, offset_mm: float) -> str:
    """Emit code that binds ``plane`` to a construction plane (offset if needed)."""
    if offset_mm:
        return (
            "inp = root.constructionPlanes.createInput()\n"
            f"inp.setByOffset({base}, mm({offset_mm}))\n"
            "plane = root.constructionPlanes.add(inp)\n"
        )
    return f"plane = {base}\n"


# ---------------------------------------------------------------------------
# 1. box
# ---------------------------------------------------------------------------
def _fusion360_box(args, db, user_id):
    w = float(args.get("width_mm", 0) or 0) / 10.0
    d = float(args.get("depth_mm", 0) or 0) / 10.0
    h = float(args.get("height_mm", 0) or 0) / 10.0
    cx = float(args.get("cx_mm", 0) or 0) / 10.0
    cy = float(args.get("cy_mm", 0) or 0) / 10.0
    cz = float(args.get("cz_mm", 0) or 0) / 10.0
    if w <= 0 or d <= 0 or h <= 0:
        return _bad("width_mm, depth_mm, height_mm must all be > 0")
    code = (
        MM
        + _offset_plane("root.xYConstructionPlane", cz * 10 - h * 10 / 2)
        + "sk = root.sketches.add(plane)\n"
        + f"sk.sketchCurves.sketchLines.addTwoPointRectangle(adsk.core.Point3D.create({cx - w/2}, {cy - d/2}, 0), adsk.core.Point3D.create({cx + w/2}, {cy + d/2}, 0))\n"
        + "e = root.features.extrudeFeatures.createInput(sk.profiles.item(0), adsk.fusion.FeatureOperations.NewBodyFeatureOperation)\n"
        + f"e.setDistanceExtent(False, mm({h * 10}))\n"
        + "root.features.extrudeFeatures.add(e)\n"
        + "print('BODY_INDEX', root.bRepBodies.count - 1)\n"
    )
    code = _apply_component(code, args.get("component_index"))
    r = _run(code, db)
    if not r["success"]:
        return r
    return {"success": True, "body_index": _last_int(r["stdout"], "BODY_INDEX"), "stdout": r["stdout"]}


# ---------------------------------------------------------------------------
# 2. cylinder
# ---------------------------------------------------------------------------
def _fusion360_cylinder(args, db, user_id):
    dia = float(args.get("diameter_mm", 0) or 0) / 10.0
    h = float(args.get("height_mm", 0) or 0) / 10.0
    cx = float(args.get("cx_mm", 0) or 0) / 10.0
    cy = float(args.get("cy_mm", 0) or 0) / 10.0
    cz = float(args.get("cz_mm", 0) or 0) / 10.0
    axis = args.get("axis", "z")
    if dia <= 0 or h <= 0:
        return _bad("diameter_mm and height_mm must be > 0")
    if axis != "z":
        return _bad("axis must be 'z' (for x/y-axis cylinders, sketch a circle on the yz/xz plane and extrude)")
    code = (
        MM
        + _offset_plane("root.xYConstructionPlane", cz * 10 - h * 10 / 2)
        + "sk = root.sketches.add(plane)\n"
        + f"sk.sketchCurves.sketchCircles.addByCenterRadius(adsk.core.Point3D.create({cx}, {cy}, 0), {dia/2})\n"
        + "e = root.features.extrudeFeatures.createInput(sk.profiles.item(0), adsk.fusion.FeatureOperations.NewBodyFeatureOperation)\n"
        + f"e.setDistanceExtent(False, mm({h * 10}))\n"
        + "root.features.extrudeFeatures.add(e)\n"
        + "print('BODY_INDEX', root.bRepBodies.count - 1)\n"
    )
    code = _apply_component(code, args.get("component_index"))
    r = _run(code, db)
    if not r["success"]:
        return r
    return {"success": True, "body_index": _last_int(r["stdout"], "BODY_INDEX"), "stdout": r["stdout"]}


# ---------------------------------------------------------------------------
# 3. sphere (semicircle revolved about Z)
# ---------------------------------------------------------------------------
def _fusion360_sphere(args, db, user_id):
    dia = float(args.get("diameter_mm", 0) or 0) / 10.0
    cx = float(args.get("cx_mm", 0) or 0) / 10.0
    cy = float(args.get("cy_mm", 0) or 0) / 10.0
    cz = float(args.get("cz_mm", 0) or 0) / 10.0
    if dia <= 0:
        return _bad("diameter_mm must be > 0")
    R = dia / 2.0
    # Profile plane = xz plane (contains Z). Offset by cy to centre at y=cy.
    # Sketch-local Y on the xz plane maps to WORLD -Z, so sketch centre = (cx, -cz).
    code = (
        MM
        + _offset_plane("root.xZConstructionPlane", cy * 10)
        + "sk = root.sketches.add(plane)\n"
        + f"sk.sketchCurves.sketchArcs.addByThreePoints(adsk.core.Point3D.create({cx}, {-cz - R}, 0), adsk.core.Point3D.create({cx + R}, {-cz}, 0), adsk.core.Point3D.create({cx}, {-cz + R}, 0))\n"
        + f"sk.sketchCurves.sketchLines.addByTwoPoints(adsk.core.Point3D.create({cx}, {-cz + R}, 0), adsk.core.Point3D.create({cx}, {-cz - R}, 0))\n"
        + "rin = root.features.revolveFeatures.createInput(sk.profiles.item(0), root.zConstructionAxis, adsk.fusion.FeatureOperations.NewBodyFeatureOperation)\n"
        + "rin.setAngleExtent(False, adsk.core.ValueInput.createByString('360 deg'))\n"
        + "root.features.revolveFeatures.add(rin)\n"
        + "print('BODY_INDEX', root.bRepBodies.count - 1)\n"
    )
    code = _apply_component(code, args.get("component_index"))
    r = _run(code, db)
    if not r["success"]:
        return r
    return {"success": True, "body_index": _last_int(r["stdout"], "BODY_INDEX"), "stdout": r["stdout"]}


# ---------------------------------------------------------------------------
# 4. torus (circle offset from Z axis, revolved about Z)
# ---------------------------------------------------------------------------
def _fusion360_torus(args, db, user_id):
    major = float(args.get("major_dia_mm", 0) or 0) / 10.0
    minor = float(args.get("minor_dia_mm", 0) or 0) / 10.0
    cx = float(args.get("cx_mm", 0) or 0) / 10.0
    cy = float(args.get("cy_mm", 0) or 0) / 10.0
    cz = float(args.get("cz_mm", 0) or 0) / 10.0
    if major <= 0 or minor <= 0:
        return _bad("major_dia_mm and minor_dia_mm must be > 0")
    R = major / 2.0
    r = minor / 2.0
    code = (
        MM
        + _offset_plane("root.xZConstructionPlane", cy * 10)
        + "sk = root.sketches.add(plane)\n"
        + f"sk.sketchCurves.sketchCircles.addByCenterRadius(adsk.core.Point3D.create({cx + R}, {-cz}, 0), {r})\n"
        + "rin = root.features.revolveFeatures.createInput(sk.profiles.item(0), root.zConstructionAxis, adsk.fusion.FeatureOperations.NewBodyFeatureOperation)\n"
        + "rin.setAngleExtent(False, adsk.core.ValueInput.createByString('360 deg'))\n"
        + "root.features.revolveFeatures.add(rin)\n"
        + "print('BODY_INDEX', root.bRepBodies.count - 1)\n"
    )
    code = _apply_component(code, args.get("component_index"))
    r = _run(code, db)
    if not r["success"]:
        return r
    return {"success": True, "body_index": _last_int(r["stdout"], "BODY_INDEX"), "stdout": r["stdout"]}


# ---------------------------------------------------------------------------
# 5. sweep (profile + path)
# ---------------------------------------------------------------------------
def _fusion360_sweep(args, db, user_id):
    psk = args.get("profile_sketch_index")
    path_sk = args.get("path_sketch_index")
    if psk is None or path_sk is None:
        return _bad("profile_sketch_index and path_sketch_index are required")
    profile = int(args.get("profile_index", 0) or 0)
    path_idx = int(args.get("path_curve_index", 0) or 0)
    path_type = args.get("path_type", "line")
    if path_type not in ("line", "spline"):
        return _bad("path_type must be 'line' or 'spline'")
    if path_type == "line":
        curve = f"root.sketches.item({int(path_sk)}).sketchCurves.sketchLines.item({path_idx})"
    else:
        curve = f"root.sketches.item({int(path_sk)}).sketchCurves.sketchFittedSplines.item({path_idx})"
    operation = args.get("operation", "new")
    if operation not in _OPERATIONS:
        return _bad(f"operation must be one of {sorted(_OPERATIONS)}")
    code = (
        MM
        + f"sk = root.sketches.item({int(psk)})\n"
        + f"path = adsk.fusion.Path.create({curve}, adsk.fusion.ChainedCurveOptions.connectedChainedCurves)\n"
        + f"swin = root.features.sweepFeatures.createInput(sk.profiles.item({profile}), path, {_OPERATIONS[operation]})\n"
        + "root.features.sweepFeatures.add(swin)\n"
        + "print('BODY_INDEX', root.bRepBodies.count - 1)\n"
    )
    code = _apply_component(code, args.get("component_index"))
    r = _run(code, db)
    if not r["success"]:
        return r
    return {"success": True, "body_index": _last_int(r["stdout"], "BODY_INDEX"), "stdout": r["stdout"]}


# ---------------------------------------------------------------------------
# 6. loft (multiple profiles)
# ---------------------------------------------------------------------------
def _fusion360_loft(args, db, user_id):
    sketches = args.get("sketch_indices") or args.get("profile_sketch_indices")
    if not sketches or len(sketches) < 2:
        return _bad("sketch_indices must list >= 2 sketch indices to loft between")
    profile = int(args.get("profile_index", 0) or 0)
    operation = args.get("operation", "new")
    if operation not in _OPERATIONS:
        return _bad(f"operation must be one of {sorted(_OPERATIONS)}")
    lines = []
    for si in sketches:
        lines.append(f"lin.loftSections.add(root.sketches.item({int(si)}).profiles.item({profile}))")
    code = (
        MM
        + f"lin = root.features.loftFeatures.createInput({_OPERATIONS[operation]})\n"
        + "\n".join(lines) + "\n"
        + "root.features.loftFeatures.add(lin)\n"
        + "print('BODY_INDEX', root.bRepBodies.count - 1)\n"
    )
    code = _apply_component(code, args.get("component_index"))
    r = _run(code, db)
    if not r["success"]:
        return r
    return {"success": True, "body_index": _last_int(r["stdout"], "BODY_INDEX"), "stdout": r["stdout"]}


# ---------------------------------------------------------------------------
# 7. shell (remove one face, hollow the rest)
# ---------------------------------------------------------------------------
def _fusion360_shell(args, db, user_id):
    bi = args.get("body_index")
    if bi is None:
        return _bad("body_index is required")
    thickness = float(args.get("thickness_mm", 0) or 0)
    if thickness <= 0:
        return _bad("thickness_mm must be > 0")
    face = args.get("remove_face", "top")
    directions = {
        "top": ("z", ">"),
        "bottom": ("z", "<"),
        "front": ("x", ">"),
        "back": ("x", "<"),
        "left": ("y", "<"),
        "right": ("y", ">"),
    }
    if face not in directions:
        return _bad(f"remove_face must be one of {sorted(directions)}")
    axis, cmp_ = directions[face]
    code = (
        MM
        + f"body = root.bRepBodies.item({int(bi)})\n"
        + "chosen = None\n"
        + "for f in body.faces:\n"
        + "    if f.geometry.objectType.endswith('Plane'):\n"
        + f"        if chosen is None or f.centroid.{axis} {cmp_} chosen.centroid.{axis}:\n"
        + "            chosen = f\n"
        + "if chosen is None:\n"
        + "    print('ERROR: no planar face found')\n"
        + "else:\n"
        + "    coll = adsk.core.ObjectCollection.create()\n"
        + "    coll.add(chosen)\n"
        + "    sin = root.features.shellFeatures.createInput(coll, False)\n"
        + f"    sin.insideThickness = mm({thickness})\n"
        + "    root.features.shellFeatures.add(sin)\n"
        + "    print('OK shell')\n"
    )
    code = _apply_component(code, args.get("component_index"))
    return _run(code, db)


# ---------------------------------------------------------------------------
# 8. chamfer (all straight edges, equal distance; per-edge fallback)
# ---------------------------------------------------------------------------
def _fusion360_chamfer(args, db, user_id):
    bi = args.get("body_index")
    if bi is None:
        return _bad("body_index is required")
    dist = float(args.get("distance_mm", 0) or 0)
    if dist <= 0:
        return _bad("distance_mm must be > 0")
    # First try batching all line edges; on failure, fall back per-edge.
    code = (
        MM
        + f"body = root.bRepBodies.item({int(bi)})\n"
        + "edges = adsk.core.ObjectCollection.create()\n"
        + "for edge in body.edges:\n"
        + "    if edge.geometry.objectType.endswith('Line3D'):\n"
        + "        edges.add(edge)\n"
        + "if edges.count == 0:\n"
        + "    print('ERROR: no straight edges to chamfer')\n"
        + "else:\n"
        + "    try:\n"
        + f"        cin = root.features.chamferFeatures.createInput(edges, True)\n"
        + f"        cin.setToEqualDistance(mm({dist}))\n"
        + "        root.features.chamferFeatures.add(cin)\n"
        + "        print('OK chamfer batch')\n"
        + "    except Exception:\n"
        + "        done = 0\n"
        + "        for i in range(edges.count):\n"
        + "            one = adsk.core.ObjectCollection.create()\n"
        + "            one.add(edges.item(i))\n"
        + "            try:\n"
        + f"                cin = root.features.chamferFeatures.createInput(one, True)\n"
        + f"                cin.setToEqualDistance(mm({dist}))\n"
        + "                root.features.chamferFeatures.add(cin)\n"
        + "                done += 1\n"
        + "            except Exception:\n"
        + "                pass\n"
        + "        print('OK chamfer per-edge', done)\n"
    )
    code = _apply_component(code, args.get("component_index"))
    return _run(code, db)


# ---------------------------------------------------------------------------
# 8b. edge_chamfer — chamfer SPECIFIC edges of ONE face (targeted, 2026-08-28)
# ---------------------------------------------------------------------------
# Why: fusion360_chamfer takes EVERY straight edge of a body — a batch chamfer
# on a bolt+nut scene chamfered the WRONG body (nut instead of bolt head), and
# deepseek fell back to raw adsk and hit ChamferFeatures_createInput signature
# errors. This tool selects edges by FACE + POSITION on that face:
#   face = 'top'|'bottom'|'front'|'back'|'left'|'right'  (extreme-centroid
#   classification, same as fusion360_probe — no fragile normal math)
#   edge = 'all' (default) | 'longest' | 'shortest' | 'front'|'back'|'left'|'right'
#          (position = the boundary edge whose MIDPOINT is extreme along that axis)
# Verified live 2026-08-28: chamfer the top rim of a hex bolt head → exactly the
# 6 rim edges chamfered, sides/nut untouched.
def _fusion360_edge_chamfer(args, db, user_id):
    bi = args.get("body_index")
    if bi is None:
        return _bad("body_index is required")
    dist = float(args.get("distance_mm", 0) or 0)
    if dist <= 0:
        return _bad("distance_mm must be > 0")
    face = str(args.get("face", "top") or "top")
    if face not in ("top", "bottom", "front", "back", "left", "right"):
        return _bad("face must be top|bottom|front|back|left|right")
    edge = str(args.get("edge", "all") or "all")
    if edge not in ("all", "longest", "shortest", "front", "back", "left", "right"):
        return _bad("edge must be all|longest|shortest|front|back|left|right")
    code = (
        MM
        + f"body = root.bRepBodies.item({int(bi)})\n"
        + f"TARGET_FACE = '{face}'\n"
        + f"TARGET_EDGE = '{edge}'\n"
        + "target = None\n"
        + "best = None\n"
        + "for f in body.faces:\n"
        + "    c = f.centroid\n"
        + "    s = {'top': c.z, 'bottom': -c.z, 'front': c.y, 'back': -c.y, 'right': c.x, 'left': -c.x}.get(TARGET_FACE)\n"
        + "    if s is None:\n"
        + "        continue\n"
        + "    if best is None or s > best:\n"
        + "        best = s\n"
        + "        target = f\n"
        + "if target is None:\n"
        + "    print('ERROR: no %s face found on body %d' % (TARGET_FACE, bodyIndex))\n"
        + "else:\n"
        + "    elist = []\n"
        + "    for loop in target.loops:\n"
        + "        if loop.isOuter:\n"
        + "            for e in loop.edges:\n"
        + "                elist.append(e)\n"
        + "    selected = []\n"
        + "    if TARGET_EDGE == 'all':\n"
        + "        selected = elist\n"
        + "    elif TARGET_EDGE in ('longest', 'shortest'):\n"
        + "        pick = None\n"
        + "        pk = None\n"
        + "        for e in elist:\n"
        + "            L = e.length\n"
        + "            if pk is None or (TARGET_EDGE == 'longest' and L > pk) or (TARGET_EDGE == 'shortest' and L < pk):\n"
        + "                pk = L\n"
        + "                pick = e\n"
        + "        if pick is not None:\n"
        + "            selected = [pick]\n"
        + "    else:\n"
        + "        pick = None\n"
        + "        pk = None\n"
        + "        for e in elist:\n"
        + "            a = e.startVertex.geometry\n"
        + "            b = e.endVertex.geometry\n"
        + "            m = ((a.x + b.x) / 2.0, (a.y + b.y) / 2.0, (a.z + b.z) / 2.0)\n"
        + "            s = {'front': m[1], 'back': -m[1], 'right': m[0], 'left': -m[0]}[TARGET_EDGE]\n"
        + "            if pk is None or s > pk:\n"
        + "                pk = s\n"
        + "                pick = e\n"
        + "        if pick is not None:\n"
        + "            selected = [pick]\n"
        + "    if not selected:\n"
        + "        print('ERROR: no edges to chamfer on %s face' % TARGET_FACE)\n"
        + "    else:\n"
        + "        coll = adsk.core.ObjectCollection.create()\n"
        + "        for e in selected:\n"
        + "            coll.add(e)\n"
        + "        try:\n"
        + f"            cin = root.features.chamferFeatures.createInput(coll, True)\n"
        + f"            cin.setToEqualDistance(mm({dist}))\n"
        + "            root.features.chamferFeatures.add(cin)\n"
        + "            print('CHAMFERED', coll.count, 'edge(s) on', TARGET_FACE)\n"
        + "        except Exception:\n"
        + "            done = 0\n"
        + "            for i in range(coll.count):\n"
        + "                one = adsk.core.ObjectCollection.create()\n"
        + "                one.add(coll.item(i))\n"
        + "                try:\n"
        + f"                    cin = root.features.chamferFeatures.createInput(one, True)\n"
        + f"                    cin.setToEqualDistance(mm({dist}))\n"
        + "                    root.features.chamferFeatures.add(cin)\n"
        + "                    done += 1\n"
        + "                except Exception:\n"
        + "                    pass\n"
        + "            print('CHAMFERED per-edge', done)\n"
    )
    code = _apply_component(code, args.get("component_index"))
    r = _run(code, db)
    if not r["success"]:
        return r
    return {
        "success": True,
        "edges_chamfered": _last_int(r["stdout"], "CHAMFERED"),
        "stdout": r["stdout"],
    }


# ---------------------------------------------------------------------------
# 8c. extend_face — extend a body along a face's outward normal, re-projecting
#     the face's own profile (2026-08-28). Fixes "hex became a cylinder" updates.
# ---------------------------------------------------------------------------
# The classic update failure: "make the hex head 3mm taller" → the model draws a
# NEW sketch (often a circle) + join extrude → the head grows but becomes a Ø5
# cylinder cap on the hex. This tool keeps the PROFILE: it creates an offset
# construction plane on the target face, PROJECTS the face's outline into a new
# sketch, and extrudes the projected (closed) profile with join/cut. A hex stays
# a hex; a washer stays an annulus (the projected inner bore loop is respected).
# Direction: outward from the body (top/front/right = +axis, bottom/back/left =
# -axis of the matching default construction plane). operation='cut' inverts so
# material is removed INTO the body (e.g. deepen a pocket from the top face).
def _fusion360_extend_face(args, db, user_id):
    bi = args.get("body_index")
    if bi is None:
        return _bad("body_index is required")
    dist = float(args.get("distance_mm", 0) or 0)
    if dist <= 0:
        return _bad("distance_mm must be > 0")
    face = str(args.get("face", "top") or "top")
    if face not in ("top", "bottom", "front", "back", "left", "right"):
        return _bad("face must be top|bottom|front|back|left|right")
    op = str(args.get("operation", "join") or "join")
    if op not in ("join", "cut"):
        return _bad("operation must be join|cut")
    # Outward direction per face relative to the offset plane's +normal
    # (planes: top/bottom -> xY (+Z), front/back -> xZ (+Y), left/right -> yZ (+X)).
    outward_neg = {"top": False, "bottom": True, "front": False, "back": True,
                   "right": False, "left": True}[face]
    negative = (not outward_neg) if op == "cut" else outward_neg
    op_expr = _OPERATIONS[op] if op in _OPERATIONS else _OPERATIONS["join"]
    code = (
        MM
        + f"body = root.bRepBodies.item({int(bi)})\n"
        + f"TARGET_FACE = '{face}'\n"
        + f"NEGATIVE = {str(negative)}\n"
        + "target = None\n"
        + "best = None\n"
        + "for f in body.faces:\n"
        + "    c = f.centroid\n"
        + "    s = {'top': c.z, 'bottom': -c.z, 'front': c.y, 'back': -c.y, 'right': c.x, 'left': -c.x}.get(TARGET_FACE)\n"
        + "    if s is None:\n"
        + "        continue\n"
        + "    if best is None or s > best:\n"
        + "        best = s\n"
        + "        target = f\n"
        + "if target is None:\n"
        + "    print('ERROR: no %s face found' % TARGET_FACE)\n"
        + "else:\n"
        + "    cc = target.centroid\n"
        + "    base = {'top': root.xYConstructionPlane, 'bottom': root.xYConstructionPlane, 'front': root.xZConstructionPlane, 'back': root.xZConstructionPlane, 'right': root.yZConstructionPlane, 'left': root.yZConstructionPlane}[TARGET_FACE]\n"
        + "    off = {'top': cc.z, 'bottom': cc.z, 'front': cc.y, 'back': cc.y, 'right': cc.x, 'left': cc.x}[TARGET_FACE]\n"
        + "    pin = root.constructionPlanes.createInput()\n"
        + "    pin.setByOffset(base, adsk.core.ValueInput.createByReal(off))\n"
        + "    pl = root.constructionPlanes.add(pin)\n"
        + "    sk = root.sketches.add(pl)\n"
        + "    sk.project(target)\n"
        + "    prof = None\n"
        + "    pbest = -1.0\n"
        + "    for p in sk.profiles:\n"
        + "        bb = p.boundingBox\n"
        + "        a = (bb.maxPoint.x - bb.minPoint.x) * (bb.maxPoint.y - bb.minPoint.y)\n"
        + "        if a > pbest:\n"
        + "            pbest = a\n"
        + "            prof = p\n"
        + "    if prof is None:\n"
        + "        print('ERROR: no closed profile projected from %s face' % TARGET_FACE)\n"
        + "    else:\n"
        + f"        op = {op_expr}\n"
        + "        ext = root.features.extrudeFeatures.createInput(prof, op)\n"
        + f"        ext.setDistanceExtent(NEGATIVE, mm({dist}))\n"
        + "        root.features.extrudeFeatures.add(ext)\n"
        + f"        print('EXTENDED', root.bRepBodies.item({int(bi)}).faces.count)\n"
    )
    code = _apply_component(code, args.get("component_index"))
    r = _run(code, db)
    if not r["success"]:
        return r
    return {
        "success": True,
        "body_index": bi,
        "faces_after": _last_int(r["stdout"], "EXTENDED"),
        "stdout": r["stdout"],
    }


# ---------------------------------------------------------------------------
# 9. hole (sketch circle + through cut)
# ---------------------------------------------------------------------------
def _fusion360_hole(args, db, user_id):
    bi = args.get("body_index")
    if bi is None:
        return _bad("body_index is required")
    dia = float(args.get("diameter_mm", 0) or 0)
    cx = float(args.get("cx_mm", 0) or 0) / 10.0
    cy = float(args.get("cy_mm", 0) or 0) / 10.0
    cz = float(args.get("cz_mm", 0) or 0) / 10.0
    if dia <= 0:
        return _bad("diameter_mm must be > 0")
    r = dia / 2.0 / 10.0
    # Sketch on the xY plane offset to cz (so the hole is at that Z), cut through.
    code = (
        MM
        + _offset_plane("root.xYConstructionPlane", cz * 10)
        + "sk = root.sketches.add(plane)\n"
        + f"sk.sketchCurves.sketchCircles.addByCenterRadius(adsk.core.Point3D.create({cx}, {cy}, 0), {r})\n"
        + "e = root.features.extrudeFeatures.createInput(sk.profiles.item(0), adsk.fusion.FeatureOperations.CutFeatureOperation)\n"
        + "e.setDistanceExtent(True, mm(500))\n"
        + "root.features.extrudeFeatures.add(e)\n"
        + "print('OK hole')\n"
    )
    code = _apply_component(code, args.get("component_index"))
    return _run(code, db)


# ---------------------------------------------------------------------------
# 10. rectangular pattern (1D or 2D)
# ---------------------------------------------------------------------------
def _fusion360_rectangular_pattern(args, db, user_id):
    bi = args.get("body_index")
    if bi is None:
        return _bad("body_index is required")
    axis1 = args.get("axis_1", "x")
    if axis1 not in _AXES:
        return _bad(f"axis_1 must be one of {sorted(_AXES)}")
    qty1 = int(args.get("quantity_1", 1) or 1)
    dist1 = float(args.get("distance_1_mm", 0) or 0)
    if qty1 < 1:
        return _bad("quantity_1 must be >= 1")
    axis2 = args.get("axis_2")
    code = (
        MM
        + f"body = root.bRepBodies.item({int(bi)})\n"
        + "coll = adsk.core.ObjectCollection.create()\n"
        + "coll.add(body)\n"
        + f"rin = root.features.rectangularPatternFeatures.createInput(coll, {_AXES[axis1]}, adsk.core.ValueInput.createByReal({qty1}), mm({dist1}), adsk.fusion.PatternDistanceType.SpacingPatternDistanceType)\n"
    )
    if axis2:
        if axis2 not in _AXES:
            return _bad(f"axis_2 must be one of {sorted(_AXES)}")
        qty2 = int(args.get("quantity_2", 1) or 1)
        dist2 = float(args.get("distance_2_mm", 0) or 0)
        code += f"rin.setDirectionTwo({_AXES[axis2]}, adsk.core.ValueInput.createByReal({qty2}), mm({dist2}))\n"
    code += (
        "root.features.rectangularPatternFeatures.add(rin)\n"
        + "print('BODY_COUNT', root.bRepBodies.count)\n"
    )
    code = _apply_component(code, args.get("component_index"))
    r = _run(code, db)
    if not r["success"]:
        return r
    return {"success": True, "body_count": _last_int(r["stdout"], "BODY_COUNT"), "stdout": r["stdout"]}


# ---------------------------------------------------------------------------
# 11. combine (boolean)
# ---------------------------------------------------------------------------
def _fusion360_combine(args, db, user_id):
    target = args.get("target_body_index")
    tool = args.get("tool_body_index")
    if target is None or tool is None:
        return _bad("target_body_index and tool_body_index are required")
    operation = args.get("operation", "cut")
    if operation not in _OPERATIONS:
        return _bad(f"operation must be one of {sorted(_OPERATIONS)}")
    code = (
        MM
        + f"target = root.bRepBodies.item({int(target)})\n"
        + f"tool = root.bRepBodies.item({int(tool)})\n"
        + "coll = adsk.core.ObjectCollection.create()\n"
        + "coll.add(tool)\n"
        + f"ci = root.features.combineFeatures.createInput(target, coll)\n"
        + f"ci.operation = {_OPERATIONS[operation]}\n"
        + "root.features.combineFeatures.add(ci)\n"
        + "print('OK combine')\n"
    )
    code = _apply_component(code, args.get("component_index"))
    return _run(code, db)


# ---------------------------------------------------------------------------
# 12. construction plane (offset)
# ---------------------------------------------------------------------------
def _fusion360_construction_plane(args, db, user_id):
    plane = args.get("plane", "xy")
    if plane not in _PLANES:
        return _bad(f"plane must be one of {sorted(_PLANES)}")
    offset = float(args.get("offset_mm", 0) or 0)
    code = (
        MM
        + _offset_plane(_PLANES[plane], offset)
        + "print('PLANE_INDEX', root.constructionPlanes.count - 1)\n"
    )
    code = _apply_component(code, args.get("component_index"))
    r = _run(code, db)
    if not r["success"]:
        return r
    return {"success": True, "plane_index": _last_int(r["stdout"], "PLANE_INDEX"), "stdout": r["stdout"]}


# ---------------------------------------------------------------------------
# 13. move (translate a body)
# ---------------------------------------------------------------------------
def _fusion360_move(args, db, user_id):
    bi = args.get("body_index")
    if bi is None:
        return _bad("body_index is required")
    dx = float(args.get("dx_mm", 0) or 0) / 10.0
    dy = float(args.get("dy_mm", 0) or 0) / 10.0
    dz = float(args.get("dz_mm", 0) or 0) / 10.0
    code = (
        MM
        + f"body = root.bRepBodies.item({int(bi)})\n"
        + "coll = adsk.core.ObjectCollection.create()\n"
        + "coll.add(body)\n"
        + "tf = adsk.core.Matrix3D.create()\n"
        + f"tf.translation = adsk.core.Vector3D.create({dx}, {dy}, {dz})\n"
        + "minp = root.features.moveFeatures.createInput(coll, tf)\n"
        + "root.features.moveFeatures.add(minp)\n"
        + "print('OK move')\n"
    )
    code = _apply_component(code, args.get("component_index"))
    return _run(code, db)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
registry.register(
    name="fusion360_box",
    schema=_schema("fusion360_box", f"Create a solid box primitive centered at (cx_mm, cy_mm, cz_mm). {_COMMON}", {"width_mm": {"type": "number"}, "depth_mm": {"type": "number"}, "height_mm": {"type": "number"}, "cx_mm": {"type": "number"}, "cy_mm": {"type": "number"}, "cz_mm": {"type": "number"}}, ["width_mm", "depth_mm", "height_mm"]),
    handler=_fusion360_box,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Create a solid box primitive; returns body_index.", emoji="📦",
)

registry.register(
    name="fusion360_cylinder",
    schema=_schema("fusion360_cylinder", f"Create a solid cylinder (axis = Z) centered at (cx_mm, cy_mm, cz_mm). {_COMMON}", {"diameter_mm": {"type": "number"}, "height_mm": {"type": "number"}, "cx_mm": {"type": "number"}, "cy_mm": {"type": "number"}, "cz_mm": {"type": "number"}, "axis": {"type": "string", "enum": ["z"]}}, ["diameter_mm", "height_mm"]),
    handler=_fusion360_cylinder,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Create a solid cylinder (Z axis); returns body_index.", emoji="🥫",
)

registry.register(
    name="fusion360_sphere",
    schema=_schema("fusion360_sphere", f"Create a solid sphere centered at (cx_mm, cy_mm, cz_mm). {_COMMON}", {"diameter_mm": {"type": "number"}, "cx_mm": {"type": "number"}, "cy_mm": {"type": "number"}, "cz_mm": {"type": "number"}}, ["diameter_mm"]),
    handler=_fusion360_sphere,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Create a solid sphere; returns body_index.", emoji="⚪",
)

registry.register(
    name="fusion360_torus",
    schema=_schema("fusion360_torus", f"Create a solid torus (donut) centered at (cx_mm, cy_mm, cz_mm), axis = Z. {_COMMON}", {"major_dia_mm": {"type": "number"}, "minor_dia_mm": {"type": "number"}, "cx_mm": {"type": "number"}, "cy_mm": {"type": "number"}, "cz_mm": {"type": "number"}}, ["major_dia_mm", "minor_dia_mm"]),
    handler=_fusion360_torus,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Create a solid torus; returns body_index.", emoji="🍩",
)

registry.register(
    name="fusion360_sweep",
    schema=_schema("fusion360_sweep", f"Sweep a profile along a path (a line or fitted-spline in another sketch). {_COMMON}", {"profile_sketch_index": {"type": "integer"}, "path_sketch_index": {"type": "integer"}, "profile_index": {"type": "integer"}, "path_curve_index": {"type": "integer"}, "path_type": {"type": "string", "enum": ["line", "spline"]}, "operation": {"type": "string", "enum": ["new", "join", "cut", "intersect"]}}, ["profile_sketch_index", "path_sketch_index"]),
    handler=_fusion360_sweep,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Sweep a profile along a path; returns body_index.", emoji="🌀",
)

registry.register(
    name="fusion360_loft",
    schema=_schema("fusion360_loft", f"Loft a solid through >=2 sketch profiles (each sketch's profiles.item(profile_index)). {_COMMON}", {"sketch_indices": {"type": "array", "items": {"type": "integer"}}, "profile_index": {"type": "integer"}, "operation": {"type": "string", "enum": ["new", "join", "cut", "intersect"]}}, ["sketch_indices"]),
    handler=_fusion360_loft,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Loft through multiple profiles; returns body_index.", emoji="🪢",
)

registry.register(
    name="fusion360_shell",
    schema=_schema("fusion360_shell", f"Hollow a body to a wall thickness by removing one planar face (remove_face = top/bottom/front/back/left/right). {_COMMON}", {"body_index": {"type": "integer"}, "thickness_mm": {"type": "number"}, "remove_face": {"type": "string", "enum": ["top", "bottom", "front", "back", "left", "right"]}}, ["body_index", "thickness_mm"]),
    handler=_fusion360_shell,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Hollow a body (remove one face).", emoji="🥣",
)

registry.register(
    name="fusion360_chamfer",
    schema=_schema("fusion360_chamfer", f"Chamfer every straight edge of a body with an equal distance. Falls back to per-edge on complex geometry. {_COMMON}", {"body_index": {"type": "integer"}, "distance_mm": {"type": ["number", "string"]}}, ["body_index", "distance_mm"]),
    handler=_fusion360_chamfer,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Chamfer all straight edges of a body.",
    emoji="✂️",
)

registry.register(
    name="fusion360_edge_chamfer",
    schema=_schema("fusion360_edge_chamfer", f"Chamfer SPECIFIC edges of ONE face of a body (targeted — never touches other bodies or other faces). face selects the face by extreme centroid: top/bottom/front/back/left/right. edge selects which boundary edges of that face: all (default, e.g. the whole top rim of a bolt head), longest/shortest, or front/back/left/right (the boundary edge whose MIDPOINT is extreme along that axis, e.g. the front top edge). Use this instead of fusion360_chamfer when a design has multiple bodies/faces and you only want ONE rim bevelled. {_COMMON}", {"body_index": {"type": "integer"}, "distance_mm": {"type": ["number", "string"]}, "face": {"type": "string", "enum": ["top", "bottom", "front", "back", "left", "right"]}, "edge": {"type": "string", "enum": ["all", "longest", "shortest", "front", "back", "left", "right"]}}, ["body_index", "distance_mm"]),
    handler=_fusion360_edge_chamfer,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Chamfer specific edges of one face (targeted).",
    emoji="🔪",
)

registry.register(
    name="fusion360_extend_face",
    schema=_schema("fusion360_extend_face", f"Extend a body along one face's OUTWARD normal by re-projecting that face's own profile — a hex stays a hex, a washer stays an annulus (never a circle cap). face = which face to extend from (top/bottom/front/back/left/right, default top) — top = the face with the HIGHEST centroid z (+Z max), bottom = the LOWEST (-Z min). CHOOSE THE FACE OF THE FEATURE THE USER NAMED: in a bolt, the HEX HEAD is the top (+Z max) face and the round shank end is the bottom (-Z min) face, so 'make the hex head taller' = face 'top'; 'make the shank longer' = face 'bottom'. operation = 'join' (default: add material outward, e.g. make the head 3mm taller) or 'cut' (remove material INTO the body from that face, e.g. deepen a pocket). USE THIS for 'make it taller/wider/thicker/deeper' updates instead of drawing a new sketch — the new sketch loses the exact profile (a circle gets drawn for a hex head). AFTER extending, call fusion360_probe({{type:'face', body_index, which:'top'}}) and check centroid_mm moved to the NEW height — this confirms the RIGHT feature grew (the bbox cannot tell WHERE the growth happened). Returns faces_after. {_COMMON}", {"body_index": {"type": "integer"}, "distance_mm": {"type": ["number", "string"]}, "face": {"type": "string", "enum": ["top", "bottom", "front", "back", "left", "right"]}, "operation": {"type": "string", "enum": ["join", "cut"]}}, ["body_index", "distance_mm"]),
    handler=_fusion360_extend_face,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Extend a body from a face, keeping its exact profile.",
    emoji="📏",
)
registry.register(
    name="fusion360_hole",
    schema=_schema("fusion360_hole", f"Drill a through-hole (circle + cut) at (cx_mm, cy_mm, cz_mm) on the XY plane through the model. {_COMMON}", {"body_index": {"type": "integer"}, "diameter_mm": {"type": "number"}, "cx_mm": {"type": "number"}, "cy_mm": {"type": "number"}, "cz_mm": {"type": "number"}}, ["body_index", "diameter_mm"]),
    handler=_fusion360_hole,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Drill a through-hole.", emoji="🕳️",
)

registry.register(
    name="fusion360_rectangular_pattern",
    schema=_schema("fusion360_rectangular_pattern", f"Rectangular-pattern a body along 1 or 2 axes (axis_1/axis_2 = x/y/z). quantity includes the original; distance is spacing. {_COMMON}", {"body_index": {"type": "integer"}, "axis_1": {"type": "string", "enum": ["x", "y", "z"]}, "quantity_1": {"type": "integer"}, "distance_1_mm": {"type": "number"}, "axis_2": {"type": "string", "enum": ["x", "y", "z"]}, "quantity_2": {"type": "integer"}, "distance_2_mm": {"type": "number"}}, ["body_index", "axis_1", "quantity_1", "distance_1_mm"]),
    handler=_fusion360_rectangular_pattern,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Rectangular-pattern a body; returns total body count.", emoji="▦",
)

registry.register(
    name="fusion360_combine",
    schema=_schema("fusion360_combine", f"Boolean-combine two bodies: cut/join/intersect the tool body into the target body. {_COMMON}", {"target_body_index": {"type": "integer"}, "tool_body_index": {"type": "integer"}, "operation": {"type": "string", "enum": ["cut", "join", "intersect"]}}, ["target_body_index", "tool_body_index"]),
    handler=_fusion360_combine,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Boolean combine two bodies.", emoji="🧬",
)

registry.register(
    name="fusion360_construction_plane",
    schema=_schema("fusion360_construction_plane", f"Create an offset construction plane and return its plane_index (for sketches on custom planes). {_COMMON}", {"plane": {"type": "string", "enum": ["xy", "xz", "yz"]}, "offset_mm": {"type": "number"}}, []),
    handler=_fusion360_construction_plane,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Create an offset construction plane; returns plane_index.", emoji="📐",
)

registry.register(
    name="fusion360_move",
    schema=_schema("fusion360_move", f"Translate a body by (dx_mm, dy_mm, dz_mm). {_COMMON}", {"body_index": {"type": "integer"}, "dx_mm": {"type": "number"}, "dy_mm": {"type": "number"}, "dz_mm": {"type": "number"}}, ["body_index"]),
    handler=_fusion360_move,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Move (translate) a body.", emoji="↔️",
)
