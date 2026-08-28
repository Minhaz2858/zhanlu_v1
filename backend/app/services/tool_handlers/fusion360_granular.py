"""Granular Fusion 360 modeling tools — the anti-hallucination layer.

Instead of writing raw ``adsk`` Python (where an LLM invents names like
``adsk.core.Cylinder3D``), the CAD Agent calls these *validated* operations.
Each tool builds a verified Python snippet internally and runs it over the same
socket bridge as ``fusion360_execute_python`` (kept as the escape hatch for
anything these tools don't cover).

State flows explicitly through integer indices (``sketch_index`` /
``body_index``) returned in each tool's output — there is no hidden cross-call
state. Index semantics: ``root.sketches`` and ``root.bRepBodies`` are ordered
collections, so a freshly created sketch/body is ``item(count - 1)``.
"""

from __future__ import annotations

from app.services.tool_registry import registry
from app.services.tool_handlers.fusion360_tool import _call
from app.services.tool_handlers.fusion360_sketch_common import _CLOSE_SKETCH

MM = "def mm(v):\n    return adsk.core.ValueInput.createByReal(v / 10.0)\n"

_PLANES = {
    "xy": "root.xYConstructionPlane",
    "xz": "root.xZConstructionPlane",
    "yz": "root.yZConstructionPlane",
}

_OPERATIONS = {
    "new": "adsk.fusion.FeatureOperations.NewBodyFeatureOperation",
    "join": "adsk.fusion.FeatureOperations.JoinFeatureOperation",
    "cut": "adsk.fusion.FeatureOperations.CutFeatureOperation",
    "intersect": "adsk.fusion.FeatureOperations.IntersectFeatureOperation",
}


def _mm_expr(v, half: bool = False) -> str:
    """Emit a ValueInput code string for a dimension argument.

    A number is MILLIMETRES (converted to cm via the ``mm()`` helper). A string
    is a user-parameter NAME (or unit expression) passed verbatim to
    ``ValueInput.createByString`` so the feature tracks the parameter — reference
    parameters BY NAME (their own units apply); a bare literal in a string would
    be interpreted in Fusion's internal cm. ``half=True`` halves the value (used
    for symmetric extents, whose API takes a per-side distance).
    """
    if isinstance(v, str):
        s = v.strip().replace("'", "")
        if half:
            s = f"({s}) / 2"
        return f"adsk.core.ValueInput.createByString('{s}')"
    f = float(v)
    if half:
        f = f / 2.0
    return f"mm({f})"


def _run(code: str, db=None) -> dict:
    """Run a snippet over the bridge; return a standard tool-result dict."""
    r = _call(code, db)
    if r.get("error"):
        return {"success": False, "error": r["error"], "retryable": True}
    return {"success": True, "stdout": r.get("result", "")}


def _bad(msg: str) -> dict:
    return {"success": False, "error": msg, "retryable": True}


def _apply_component(code: str, component_index) -> str:
    """Route generated code at a component instead of root.

    Existing code-gen uniformly references ``root.<collection>``. When
    ``component_index`` is provided (an occurrence index from
    ``fusion360_component``), rewrite those references to ``comp.`` (that
    occurrence's component) and prepend the resolution line. When omitted,
    behaviour is unchanged (root). Body/sketch indices become COMPONENT-scoped
    in that mode.
    """
    if component_index is None or component_index == "":
        return code
    body = code.replace("root.", "comp.")
    return f"comp = root.occurrences.item({int(component_index)}).component\n" + body


_COMPONENT_PROP = {
    "component_index": {
        "type": "integer",
        "description": "Optional: build/operate inside a component (index from fusion360_component) instead of root.",
    }
}


# ---------------------------------------------------------------------------
# 1. clear
# ---------------------------------------------------------------------------
def _fusion360_clear(args, db, user_id):
    code = (
        "def dc(coll):\n"
        "    g = 0\n"
        "    while coll.count > 0 and g < 300:\n"
        "        g += 1\n"
        "        n = coll.count\n"
        "        ok = False\n"
        "        for idx in (0, n - 1):\n"
        "            if coll.count == 0: break\n"
        "            try:\n"
        "                coll.item(min(idx, coll.count - 1)).deleteMe(); ok = True; break\n"
        "            except Exception: continue\n"
        "        if not ok: break\n"
        "dc(root.joints)\n"
        "dc(root.asBuiltJoints)\n"
        "dc(root.occurrences)\n"
        "dc(root.features.circularPatternFeatures)\n"
        "dc(root.features.rectangularPatternFeatures)\n"
        "dc(root.features.pathPatternFeatures)\n"
        "dc(root.features.mirrorFeatures)\n"
        "dc(root.features.threadFeatures)\n"
        "dc(root.features.chamferFeatures)\n"
        "dc(root.features.filletFeatures)\n"
        "dc(root.features.holeFeatures)\n"
        "dc(root.features.sweepFeatures)\n"
        "dc(root.features.loftFeatures)\n"
        "dc(root.features.revolveFeatures)\n"
        "dc(root.features.coilFeatures)\n"
        "dc(root.features.extrudeFeatures)\n"
        "dc(root.sketches)\n"
        "dc(root.constructionPlanes)\n"
        "dc(root.constructionAxes)\n"
        "print('CLEARED bodies:', root.bRepBodies.count)\n"
    )
    return _run(_apply_component(code, args.get("component_index")), db)


# ---------------------------------------------------------------------------
# 2. sketch_create
# ---------------------------------------------------------------------------
def _fusion360_sketch_create(args, db, user_id):
    plane = args.get("plane", "xy")
    if plane not in _PLANES:
        return _bad(f"plane must be one of {sorted(_PLANES)}")
    offset = float(args.get("offset_mm", 0) or 0)
    base = _PLANES[plane]

    if offset:
        code = (
            MM
            + "inp = root.constructionPlanes.createInput()\n"
            + f"inp.setByOffset({base}, mm({offset}))\n"
            + "plane = root.constructionPlanes.add(inp)\n"
        )
    else:
        code = MM + f"plane = {base}\n"
    code += (
        "sk = root.sketches.add(plane)\n"
        "print('SKETCH_INDEX', root.sketches.count - 1)\n"
    )
    code = _apply_component(code, args.get("component_index"))
    r = _run(code, db)
    if not r["success"]:
        return r
    return {"success": True, "sketch_index": _last_int(r["stdout"], "SKETCH_INDEX"), "stdout": r["stdout"]}


# ---------------------------------------------------------------------------
# 3. sketch_circle
# ---------------------------------------------------------------------------
def _fusion360_sketch_circle(args, db, user_id):
    si = args.get("sketch_index")
    if si is None:
        return _bad("sketch_index is required")
    cx = float(args.get("cx_mm", 0) or 0) / 10.0
    cy = float(args.get("cy_mm", 0) or 0) / 10.0
    r = float(args.get("radius_mm", 0) or 0) / 10.0
    code = (
        MM
        + f"sk = root.sketches.item({int(si)})\n"
        + f"sk.sketchCurves.sketchCircles.addByCenterRadius(adsk.core.Point3D.create({cx}, {cy}, 0), {r})\n"
        + _CLOSE_SKETCH + "\n"
        + "print('OK circle')\n"
    )
    return _run(_apply_component(code, args.get("component_index")), db)


# ---------------------------------------------------------------------------
# 4. sketch_rectangle
# ---------------------------------------------------------------------------
def _fusion360_sketch_rectangle(args, db, user_id):
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
        + f"sk.sketchCurves.sketchLines.addTwoPointRectangle(adsk.core.Point3D.create({x1}, {y1}, 0), adsk.core.Point3D.create({x2}, {y2}, 0))\n"
        + _CLOSE_SKETCH + "\n"
        + "print('OK rectangle')\n"
    )
    return _run(_apply_component(code, args.get("component_index")), db)


# ---------------------------------------------------------------------------
# 5. sketch_polygon (regular polygon; radius = circumradius centre->vertex)
# ---------------------------------------------------------------------------
def _fusion360_sketch_polygon(args, db, user_id):
    si = args.get("sketch_index")
    if si is None:
        return _bad("sketch_index is required")
    sides = int(args.get("sides", 6) or 6)
    if sides < 3:
        return _bad("sides must be >= 3")
    cx = float(args.get("cx_mm", 0) or 0) / 10.0
    cy = float(args.get("cy_mm", 0) or 0) / 10.0
    r = float(args.get("circumradius_mm", 0) or 0) / 10.0
    code = (
        MM
        + "import math\n"
        + f"sk = root.sketches.item({int(si)})\n"
        + f"R, cx, cy, n = {r}, {cx}, {cy}, {sides}\n"
        + "lines = sk.sketchCurves.sketchLines\n"
        + "pts = [adsk.core.Point3D.create(cx + R * math.cos(math.radians(a)), cy + R * math.sin(math.radians(a)), 0) for a in range(0, 360, 360 // n)]\n"
        + "for i in range(n):\n"
        + "    lines.addByTwoPoints(pts[i], pts[(i + 1) % n])\n"
        + _CLOSE_SKETCH + "\n"
        + "print('OK polygon')\n"
    )
    return _run(_apply_component(code, args.get("component_index")), db)


# ---------------------------------------------------------------------------
# 6. extrude
# ---------------------------------------------------------------------------
def _extrude_guard_code(sketch_index: int) -> str:
    """Pre-flight: bail with a clear error if the sketch has no closed profile."""
    return (
        f"sk = root.sketches.item({int(sketch_index)})\n"
        + "if sk.profiles.count == 0:\n"
        + "    print('NO_PROFILE_ERROR')\n"
        + "    raise SystemExit(1)\n"
    )


def _fusion360_extrude(args, db, user_id):
    si = args.get("sketch_index")
    if si is None:
        return _bad("sketch_index is required")
    direction = args.get("direction", "pos")
    if direction not in ("pos", "neg", "sym"):
        return _bad("direction must be one of pos/neg/sym")
    operation = args.get("operation", "new")
    if operation not in _OPERATIONS:
        return _bad(f"operation must be one of {sorted(_OPERATIONS)}")
    distance = args.get("distance_mm", 0) or 0
    overlap = args.get("overlap_mm", 0) or 0
    profile = int(args.get("profile_index", 0) or 0)
    op = _OPERATIONS[operation]

    code = MM + _extrude_guard_code(si)
    code += f"e = root.features.extrudeFeatures.createInput(sk.profiles.item({profile}), {op})\n"
    if direction == "pos":
        code += f"e.setTwoSidesDistanceExtent({_mm_expr(distance)}, {_mm_expr(overlap)})\n"
    elif direction == "neg":
        code += f"e.setTwoSidesDistanceExtent({_mm_expr(overlap)}, {_mm_expr(distance)})\n"
    else:  # sym — distance_mm is the TOTAL width; API takes a per-side distance
        code += f"e.setDistanceExtent(True, {_mm_expr(distance, half=True)})\n"
    code += (
        "root.features.extrudeFeatures.add(e)\n"
        "print('BODY_INDEX', root.bRepBodies.count - 1)\n"
    )
    code = _apply_component(code, args.get("component_index"))
    r = _run(code, db)
    if not r["success"]:
        if "NO_PROFILE_ERROR" in (r.get("stdout") or ""):
            return _bad(
                "the sketch has no closed profile — its curves are not joined into "
                "a loop. Re-run the sketch tool (it auto-closes endpoints), then "
                "retry the extrude. Do NOT draw a new sketch on top."
            )
        return r
    return {"success": True, "body_index": _last_int(r["stdout"], "BODY_INDEX"), "stdout": r["stdout"]}


# ---------------------------------------------------------------------------
# 7. fillet
# ---------------------------------------------------------------------------
def _fusion360_fillet(args, db, user_id):
    bi = args.get("body_index")
    if bi is None:
        return _bad("body_index is required (from the last extrude result)")
    radius = args.get("radius_mm", 0) or 0
    code = (
        MM
        + f"body = root.bRepBodies.item({int(bi)})\n"
        + "fedges = adsk.core.ObjectCollection.create()\n"
        + "for edge in body.edges:\n"
        + "    fedges.add(edge)\n"
        + "fin = root.features.filletFeatures.createInput()\n"
        + f"fin.addConstantRadiusEdgeSet(fedges, {_mm_expr(radius)}, True)\n"
        + "root.features.filletFeatures.add(fin)\n"
        + "print('OK fillet')\n"
    )
    return _run(_apply_component(code, args.get("component_index")), db)


# ---------------------------------------------------------------------------
# 8. mirror — symmetric copy of a body across a plane
# ---------------------------------------------------------------------------
def _fusion360_mirror(args, db, user_id):
    bi = args.get("body_index")
    if bi is None:
        return _bad("body_index is required (the body to mirror)")
    plane = args.get("plane", "yz")
    if plane not in _PLANES:
        return _bad(f"plane must be one of {sorted(_PLANES)}")
    offset = float(args.get("offset_mm", 0) or 0)
    base = _PLANES[plane]

    if offset:
        code = (
            MM
            + "inp = root.constructionPlanes.createInput()\n"
            + f"inp.setByOffset({base}, mm({offset}))\n"
            + "plane = root.constructionPlanes.add(inp)\n"
        )
    else:
        code = MM + f"plane = {base}\n"
    code += (
        f"body = root.bRepBodies.item({int(bi)})\n"
        + "coll = adsk.core.ObjectCollection.create()\n"
        + "coll.add(body)\n"
        + "min_ = root.features.mirrorFeatures.createInput(coll, plane)\n"
        + "root.features.mirrorFeatures.add(min_)\n"
        + "print('BODY_COUNT', root.bRepBodies.count)\n"
    )
    code = _apply_component(code, args.get("component_index"))
    r = _run(code, db)
    if not r["success"]:
        return r
    return {"success": True, "body_count": _last_int(r["stdout"], "BODY_COUNT"), "stdout": r["stdout"]}


# ---------------------------------------------------------------------------
# 9. thread (modeled thread on a cylindrical face)
# ---------------------------------------------------------------------------
def _fusion360_thread(args, db, user_id):
    bi = args.get("body_index")
    if bi is None:
        return _bad("body_index is required (from the last extrude result)")
    designation = str(args.get("designation", "M6x1"))
    is_internal = bool(args.get("is_internal", False))
    thread_class = "6H" if is_internal else "6g"
    code = (
        MM
        + f"body = root.bRepBodies.item({int(bi)})\n"
        + "shank_face = None\n"
        + "for face in body.faces:\n"
        + "    if face.geometry.objectType == adsk.core.Cylinder.classType():\n"
        + "        shank_face = face\n"
        + "        break\n"
        + "if shank_face is None:\n"
        + "    print('ERROR: no cylindrical face found on body')\n"
        + "else:\n"
        + "    tf = root.features.threadFeatures\n"
        + f"    td = tf.createThreadInfo({is_internal}, 'ISO Metric profile', '{designation}', '{thread_class}')\n"
        + "    td.isRightHanded = True\n"
        + "    ti = tf.createInput(shank_face, td)\n"
        + "    ti.isModeled = True\n"
        + "    ti.isFullLength = True\n"
        + "    tf.add(ti)\n"
        + "    print('OK thread', root.features.threadFeatures.count)\n"
    )
    return _run(_apply_component(code, args.get("component_index")), db)


# ---------------------------------------------------------------------------
# 10. info
# ---------------------------------------------------------------------------
def _fusion360_info(args, db, user_id):
    code = (
        "print('COMPONENTS:', root.occurrences.count)\n"
        "for i in range(root.occurrences.count):\n"
        "    oc = root.occurrences.item(i)\n"
        "    print('component', i, repr(oc.component.name), 'bodies', oc.component.bRepBodies.count)\n"
        "print('BODIES:', root.bRepBodies.count)\n"
        "for i in range(root.bRepBodies.count):\n"
        "    b = root.bRepBodies.item(i)\n"
        "    bb = b.boundingBox\n"
        "    print('body', i, 'faces', b.faces.count, 'bbox', [round(x, 2) for x in bb.minPoint.asArray()], [round(x, 2) for x in bb.maxPoint.asArray()])\n"
        "print('SKETCHES:', root.sketches.count)\n"
        "print('PLANES:', root.constructionPlanes.count, 'AXES:', root.constructionAxes.count)\n"
        "tf = root.features\n"
        "feats = []\n"
        "for name in ('extrudeFeatures','revolveFeatures','coilFeatures','sweepFeatures','loftFeatures','mirrorFeatures','circularPatternFeatures','rectangularPatternFeatures','pathPatternFeatures','threadFeatures','chamferFeatures','filletFeatures','holeFeatures'):\n"
        "    c = getattr(tf, name)\n"
        "    if c.count: feats.append(name + '=' + str(c.count))\n"
        "print('FEATURES:', ', '.join(feats) if feats else 'none')\n"
        "ups = design.userParameters\n"
        "pl = []\n"
        "for i in range(ups.count):\n"
        "    p = ups.item(i)\n"
        "    pl.append(p.name + '=' + p.expression)\n"
        "print('PARAMS:', ', '.join(pl) if pl else 'none')\n"
    )
    return _run(_apply_component(code, args.get("component_index")), db)


def _fusion360_verify_build(args, db, user_id):
    """Deterministic spec-vs-reality check. The BACKEND compares the live
    scene against what the agent claims to have built, so a wrong body count,
    duplicated bodies, missing parameters, or (2026-08-28) WRONG DIMENSIONS
    can never be silently reported as success by the model."""
    expected_bodies = args.get("expected_body_count")
    expected_params = args.get("expected_params") or []
    expected_dims = args.get("expected_dimensions") or []
    expected_probes = args.get("expected_probes") or []
    code = (
        "print('VERIFY_BODIES:', root.bRepBodies.count)\n"
        "for i in range(root.bRepBodies.count):\n"
        "    b = root.bRepBodies.item(i)\n"
        "    bb = b.boundingBox\n"
        "    mn = bb.minPoint; mx = bb.maxPoint\n"
        "    print('VBODY', i, round(mn.x,4), round(mn.y,4), round(mn.z,4), round(mx.x,4), round(mx.y,4), round(mx.z,4), b.faces.count)\n"
        "ups = design.userParameters\n"
        "names = ','.join(ups.item(i).name for i in range(ups.count))\n"
        "print('VPARAMS:', names)\n"
        "for i in range(ups.count):\n"
        "    p = ups.item(i)\n"
        "    print('VPARAM', p.name, p.value)\n"
    )
    r = _run(_apply_component(code, args.get("component_index")), db)
    if not r["success"]:
        return r
    stdout = r.get("stdout", "") or ""
    body_count = None
    bodies = []
    params = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("VERIFY_BODIES:"):
            try:
                body_count = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif line.startswith("VBODY "):
            parts = line.split()
            try:
                # VBODY idx minx miny minz maxx maxy maxz faces  (all cm)
                idx = int(parts[1])
                vals = [float(x) for x in parts[2:8]]
                faces = int(parts[8])
                bodies.append({"index": idx, "min": vals[:3], "max": vals[3:6], "faces": faces})
            except (ValueError, IndexError):
                continue
        elif line.startswith("VPARAMS:"):
            raw = line.split(":", 1)[1].strip()
            params = [p for p in raw.split(",") if p]
        elif line.startswith("VPARAM "):
            continue  # consumed by _parse_vparam_values below
    # duplicate detection — identical bounding boxes (within 0.05 cm = 0.5 mm)
    duplicate_pairs = []
    for i in range(len(bodies)):
        for j in range(i + 1, len(bodies)):
            a, b = bodies[i], bodies[j]
            if all(abs(a["min"][k] - b["min"][k]) < 0.05 for k in range(3)) and \
               all(abs(a["max"][k] - b["max"][k]) < 0.05 for k in range(3)):
                duplicate_pairs.append([a["index"], b["index"]])
    issues = []
    if expected_bodies is not None:
        try:
            if body_count is not None and body_count != int(expected_bodies):
                issues.append(f"body count {body_count} != expected {expected_bodies}")
        except (ValueError, TypeError):
            pass
    if duplicate_pairs:
        issues.append(f"{len(duplicate_pairs)} duplicate body pair(s): {duplicate_pairs}")
    # ── Parameter-truth check (2026-08-28) ────────────────────────────
    # expected_params entries may be plain name strings (legacy: name must
    # exist) or {name, value} dicts (value in MM, compared with 5% tolerance
    # against the LIVE user-parameter value read back from Fusion).
    param_values = _parse_vparam_values(stdout)
    issues += _check_expected_params(expected_params, param_values)
    # ── Dimension-truth check (2026-08-28) ────────────────────────────
    # The agent declares per-body expectations (kind + dims in MM) from its
    # own plan; the backend measures the LIVE bbox and compares. Catches the
    # "verified but Ø11.5 instead of Ø12" class of silent failures.
    dim_checks = []
    for exp in expected_dims:
        if not isinstance(exp, dict):
            continue
        try:
            bi = int(exp.get("body_index", -1))
            kind = exp.get("kind", "box")
            dims = exp.get("dims") or {}
            tol_mm = float(exp.get("tolerance_mm", 0.5))
            body = next((b for b in bodies if b["index"] == bi), None)
            if body is None:
                dim_checks.append({"body_index": bi, "ok": False,
                                   "reason": f"body {bi} not found in scene"})
                issues.append(f"dimension: body {bi} not found (expected a {kind})")
                continue
            w = abs(body["max"][0] - body["min"][0]) * 10.0  # cm -> mm
            d = abs(body["max"][1] - body["min"][1]) * 10.0
            h = abs(body["max"][2] - body["min"][2]) * 10.0
            if kind == "cylinder":
                # The two near-equal bbox dims = diameter; the odd one = length.
                dims_mm = [w, d, h]
                best = min(((0, 1), (0, 2), (1, 2)),
                           key=lambda p: abs(dims_mm[p[0]] - dims_mm[p[1]]))
                dia_actual = (dims_mm[best[0]] + dims_mm[best[1]]) / 2.0
                len_actual = dims_mm[3 - best[0] - best[1]]
                exp_dia = dims.get("dia")
                exp_len = dims.get("len")
                errs = []
                if exp_dia is not None and abs(dia_actual - float(exp_dia)) > tol_mm:
                    errs.append(f"dia {dia_actual:.1f}mm != expected {exp_dia}mm (±{tol_mm})")
                if exp_len is not None and abs(len_actual - float(exp_len)) > tol_mm:
                    errs.append(f"len {len_actual:.1f}mm != expected {exp_len}mm (±{tol_mm})")
                ok = not errs
                dim_checks.append({"body_index": bi, "kind": "cylinder", "ok": ok,
                                   "actual": {"dia": round(dia_actual, 1),
                                              "len": round(len_actual, 1)}})
                if not ok:
                    issues.append(f"body {bi} ({kind}): " + "; ".join(errs))
            elif kind == "hex":
                # Hex prism: the bbox measures corner-to-corner on one axis and
                # across-flats on the other (rotation-dependent), so across-flats
                # is ALWAYS min(w,d) for a regular hexagon. Orientation-proof.
                exp_af = dims.get("across_flats")
                exp_h = dims.get("height")
                exp_faces = dims.get("faces")
                af_actual = min(w, d)
                errs = []
                if exp_af is not None and abs(af_actual - float(exp_af)) > tol_mm:
                    errs.append(f"across-flats {af_actual:.1f}mm != expected {exp_af}mm (±{tol_mm})")
                if exp_h is not None and abs(h - float(exp_h)) > tol_mm:
                    errs.append(f"height {h:.1f}mm != expected {exp_h}mm (±{tol_mm})")
                if exp_faces is not None and body["faces"] != int(exp_faces):
                    errs.append(f"faces {body['faces']} != expected {exp_faces}")
                ok = not errs
                dim_checks.append({"body_index": bi, "kind": "hex", "ok": ok,
                                   "actual": {"across_flats": round(af_actual, 1),
                                              "height": round(h, 1),
                                              "faces": body["faces"]}})
                if not ok:
                    issues.append(f"body {bi} ({kind}): " + "; ".join(errs))
            else:
                exp_w, exp_d2, exp_h = dims.get("w"), dims.get("d"), dims.get("h")
                errs = []
                if exp_w is not None and abs(w - float(exp_w)) > tol_mm:
                    errs.append(f"w {w:.1f}mm != expected {exp_w}mm (±{tol_mm})")
                if exp_d2 is not None and abs(d - float(exp_d2)) > tol_mm:
                    errs.append(f"d {d:.1f}mm != expected {exp_d2}mm (±{tol_mm})")
                if exp_h is not None and abs(h - float(exp_h)) > tol_mm:
                    errs.append(f"h {h:.1f}mm != expected {exp_h}mm (±{tol_mm})")
                ok = not errs
                dim_checks.append({"body_index": bi, "kind": "box", "ok": ok,
                                   "actual": {"w": round(w, 1), "d": round(d, 1),
                                              "h": round(h, 1)}})
                if not ok:
                    issues.append(f"body {bi} ({kind}): " + "; ".join(errs))
        except (ValueError, TypeError):
            continue
    # ── Probe checks (2026-08-28): REAL feature measurements ─────────
    # expected_probes runs the same fusion360_probe snippet and compares
    # deterministically — holes/bores/face-areas/mass the bbox cannot see.
    probe_checks = []
    if expected_probes:
        from app.services.tool_handlers.fusion360_probe import (  # lazy: avoids circular import
            _compare_probe,
            _parse_probe_stdout,
            _probe_snippet,
        )
        pr = _run(_apply_component(_probe_snippet(expected_probes), args.get("component_index")), db)
        measured = _parse_probe_stdout(pr.get("stdout", "")) if pr.get("success") else []
        for q, m in zip(expected_probes, measured):
            if not isinstance(q, dict) or not isinstance(m, dict):
                continue
            _compare_probe(q, m, issues)
            probe_checks.append({"query": q, "measured": m, "ok": m.get("ok", False)})
    # ── Contract mode (2026-08-28): validate against the STORED spec ────
    # When contract_id is passed ('use_last' = newest contract for this
    # conversation, or an explicit id), the backend compares the LIVE geometry
    # against the contract persisted by fusion360_declare_spec — never against
    # the model's hand-picked verify args. Greedy per-feature bbox matching.
    contract_arg = args.get("contract_id")
    if contract_arg:
        try:
            from app.services.agent_tools import TOOL_CONTEXT  # plain global dict
            # TOOL_CONTEXT is a plain dict — dict.get() REQUIRES a key arg
            # (zero-arg .get() raises TypeError and silently killed use_last).
            _conversation_id = (
                TOOL_CONTEXT.get("conversation_id")
                if isinstance(TOOL_CONTEXT, dict)
                else None
            )
        except Exception:  # noqa: BLE001 — context plumbing must never break the tool
            _conversation_id = None
        contract = _load_contract(db, contract_arg, _conversation_id)
        if contract is None:
            issues.append(
                "no stored contract found for contract_id=%r — call "
                "fusion360_declare_spec first" % contract_arg
            )
        else:
            try:
                issues += _contract_issues(contract, bodies)
            except Exception as e:  # noqa: BLE001 — contract check must never crash the tool
                issues.append(f"contract check failed: {e}")
    ok = not issues
    return {
        "success": True,
        "ok": ok,
        "body_count": body_count,
        "expected_body_count": expected_bodies,
        "bodies": [{"index": b["index"], "faces": b["faces"],
                    "bbox_mm": [round(v * 10, 1) for v in (b["min"] + b["max"])]} for b in bodies],
        "duplicate_pairs": duplicate_pairs,
        "params": params,
        "param_values": param_values,
        "dimension_checks": dim_checks,
        "probe_checks": probe_checks,
        "issues": issues,
        "summary": ("PASS — " if ok else "FAIL — ") + ("; ".join(issues) if issues else "body count, uniqueness, parameters, dimensions and probes all check out"),
    }


def _parse_vparam_values(stdout: str) -> dict[str, float]:
    """Parse VPARAM name <value_cm> lines into {name: value_mm}."""
    out: dict[str, float] = {}
    for line in (stdout or "").splitlines():
        line = line.strip()
        if line.startswith("VPARAM "):
            parts = line.split()
            if len(parts) >= 3:
                try:
                    out[parts[1]] = round(float(parts[2]) * 10.0, 4)
                except ValueError:
                    continue
    return out


def _check_expected_params(expected_params: list, values: dict[str, float]) -> list[str]:
    """Each entry is a name str (exists check, legacy) or {name, value} dict.
    value is in mm; compared with 5% tolerance."""
    issues: list[str] = []
    for ep in expected_params or []:
        if isinstance(ep, str):
            if ep not in values:
                issues.append(f"missing parameter: {ep}")
        elif isinstance(ep, dict):
            name = ep.get("name")
            if not name:
                issues.append("expected_params entry missing 'name'")
                continue
            if name not in values:
                issues.append(f"missing parameter: {name}")
                continue
            expected = ep.get("value")
            if expected is None:
                continue
            try:
                expected_f = float(expected)
            except (TypeError, ValueError):
                issues.append(f"parameter {name} has non-numeric expected value {expected!r}")
                continue
            actual = values[name]
            tol = max(abs(expected_f) * 0.05, 0.01)
            if abs(actual - expected_f) > tol + 1e-9:
                issues.append(
                    f"parameter {name} = {actual}mm != expected {expected}mm"
                )
    return issues


def _load_contract(db, contract_id: str, conversation_id: str | None) -> dict | None:
    """Resolve contract_id ('use_last' → newest for this conversation)."""
    from app.models.cad_build_contract import CadBuildContract
    q = db.query(CadBuildContract)
    if contract_id == "use_last":
        if not conversation_id:
            return None
        row = (
            q.filter(CadBuildContract.conversation_id == conversation_id)
            .order_by(CadBuildContract.created_date.desc())
            .first()
        )
    else:
        row = q.filter(CadBuildContract.id == contract_id).first()
    if row is None or row.contract_json is None:
        return None
    # Return a COPY — never hand the live ORM-held dict to callers (a later
    # mutation/expire on the session would corrupt the stored contract).
    return dict(row.contract_json)


def _num(value, name: str, issues: list[str]) -> float | None:
    """Defensively convert a declared dim to float; record an issue if not."""
    try:
        return float(value)
    except (TypeError, ValueError):
        issues.append(f"feature dim not numeric: {name}={value!r}")
        return None


def _feature_matches_body(feature: dict, body: dict, tol_mm: float = 0.5) -> list[str]:
    """Compare ONE declared feature against ONE body's bbox (mm, from cm).
    kind hex: across_flats = min(width, depth); height = z span.
    kind cylinder: diameter = the two near-equal bbox dims; height = z span.
    kind box: width/height/depth map to the bbox w/h/d axes."""
    issues: list[str] = []
    kind = (feature.get("kind") or "").strip().lower()
    w = (body["max"][0] - body["min"][0]) * 10.0
    d = (body["max"][1] - body["min"][1]) * 10.0
    h = (body["max"][2] - body["min"][2]) * 10.0
    if kind == "hex":
        across = min(w, d)
        if "across_flats" in feature:
            exp = _num(feature["across_flats"], "across_flats", issues)
            if exp is not None and abs(across - exp) > tol_mm:
                issues.append(f"hex across_flats {across:.1f}mm != declared {feature['across_flats']}mm")
        if "height" in feature:
            exp = _num(feature["height"], "height", issues)
            if exp is not None and abs(h - exp) > tol_mm:
                issues.append(f"hex height {h:.1f}mm != declared {feature['height']}mm")
    elif kind == "cylinder":
        # The two near-equal bbox dims = diameter; the odd one = length.
        # (same logic as expected_dimensions: rotation/axis-proof)
        dims_mm = [w, d, h]
        best = min(((0, 1), (0, 2), (1, 2)),
                   key=lambda p: abs(dims_mm[p[0]] - dims_mm[p[1]]))
        dia = (dims_mm[best[0]] + dims_mm[best[1]]) / 2.0
        if "diameter" in feature:
            exp = _num(feature["diameter"], "diameter", issues)
            if exp is not None and abs(dia - exp) > tol_mm:
                issues.append(f"cylinder diameter {dia:.1f}mm != declared {feature['diameter']}mm")
        if "height" in feature:
            exp = _num(feature["height"], "height", issues)
            if exp is not None and abs(h - exp) > tol_mm:
                issues.append(f"cylinder height {h:.1f}mm != declared {feature['height']}mm")
    elif kind == "box":
        # width (alias: length) -> w, depth -> d, height -> h
        for label, actual, keys in (
            ("width", w, ("width", "length")),
            ("depth", d, ("depth",)),
            ("height", h, ("height",)),
        ):
            for key in keys:
                if key in feature:
                    exp = _num(feature[key], key, issues)
                    if exp is not None and abs(actual - exp) > tol_mm:
                        issues.append(f"box {label} {actual:.1f}mm != declared {feature[key]}mm")
                    break
    else:
        issues.append(f"unsupported contract kind: {kind}")
    return issues


def _contract_issues(contract: dict, bodies: list[dict]) -> list[str]:
    """Greedy nearest-match: each feature must match at least one body."""
    issues: list[str] = []
    features = contract.get("features") or []
    if not features:
        return ["contract has no features"]
    used: set[int] = set()
    for i, feat in enumerate(features):
        best = None
        for j, body in enumerate(bodies):
            if j in used:
                continue
            fi = _feature_matches_body(feat, body)
            if not fi:
                best = j
                break
        if best is None:
            issues.append(
                f"contract feature[{i}] kind={feat.get('kind')} "
                f"({feat}) has no matching body"
            )
        else:
            used.add(best)
    return issues


def _last_int(stdout: str, marker: str):
    """Return the last int printed after `marker`, or None."""
    val = None
    for line in (stdout or "").splitlines():
        if marker in line:
            parts = line.split()
            for p in parts[1:]:
                try:
                    val = int(float(p))
                except ValueError:
                    continue
    return val


# ---------------------------------------------------------------------------
# Schemas + registration
# ---------------------------------------------------------------------------
def _schema(name, description, properties, required):
    properties = {**properties, **_COMPONENT_PROP}
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


_COMMON = "Units are MILLIMETRES for all *_mm params. The bridge converts to Fusion's cm internally."
_EXPR_NOTE = " This param may also be a user-parameter NAME string (e.g. 'rod_exposed') to drive it parametrically — reference parameters BY NAME so their units apply; a bare numeric literal in a string would be read in cm."


registry.register(
    name="fusion360_clear",
    schema=_schema("fusion360_clear", "Delete every feature, sketch and construction plane (a clean-slate reset, tolerating broken leftovers from failed runs). Call before starting a new model.", {}, []),
    handler=_fusion360_clear,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Reset the Fusion 360 design (clear all bodies/sketches).", emoji="🧹",
)

registry.register(
    name="fusion360_sketch_create",
    schema=_schema("fusion360_sketch_create", f"Create a new sketch on a construction plane and return its sketch_index for later tools. {_COMMON}", {"plane": {"type": "string", "enum": ["xy", "xz", "yz"], "description": "Base plane (xy=front, xz=top, yz=side)."}, "offset_mm": {"type": "number", "description": "Optional offset from the plane along its normal (mm)."}}, ["plane"]),
    handler=_fusion360_sketch_create,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Create a sketch on a plane; returns sketch_index.", emoji="📐",
)

registry.register(
    name="fusion360_sketch_circle",
    schema=_schema("fusion360_sketch_circle", f"Draw a circle on an existing sketch. {_COMMON}", {"sketch_index": {"type": "integer", "description": "From fusion360_sketch_create."}, "cx_mm": {"type": "number"}, "cy_mm": {"type": "number"}, "radius_mm": {"type": "number"}}, ["sketch_index", "radius_mm"]),
    handler=_fusion360_sketch_circle,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Draw a circle on a sketch.", emoji="⭕",
)

registry.register(
    name="fusion360_sketch_rectangle",
    schema=_schema("fusion360_sketch_rectangle", f"Draw an axis-aligned rectangle on an existing sketch. {_COMMON}", {"sketch_index": {"type": "integer"}, "x1_mm": {"type": "number"}, "y1_mm": {"type": "number"}, "x2_mm": {"type": "number"}, "y2_mm": {"type": "number"}}, ["sketch_index", "x2_mm", "y2_mm"]),
    handler=_fusion360_sketch_rectangle,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Draw a rectangle on a sketch.", emoji="▭",
)

registry.register(
    name="fusion360_sketch_polygon",
    schema=_schema("fusion360_sketch_polygon", f"Draw a regular polygon on an existing sketch. circumradius_mm is centre-to-vertex (a hex bolt head across-flats W mm = circumradius W/1.732). {_COMMON}", {"sketch_index": {"type": "integer"}, "cx_mm": {"type": "number"}, "cy_mm": {"type": "number"}, "circumradius_mm": {"type": "number"}, "sides": {"type": "integer", "description": "e.g. 6 for hexagon"}}, ["sketch_index", "circumradius_mm", "sides"]),
    handler=_fusion360_sketch_polygon,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Draw a regular polygon on a sketch.", emoji="⬡",
)

registry.register(
    name="fusion360_extrude",
    schema=_schema("fusion360_extrude", f"Extrude a sketch profile into a solid. direction: pos=+Z, neg=-Z, sym=symmetric (distance_mm is TOTAL width). overlap_mm extrudes that far into the opposite side (use ~1 mm when joining to an existing body so they merge). {_COMMON}{_EXPR_NOTE}", {"sketch_index": {"type": "integer"}, "profile_index": {"type": "integer", "description": "Usually 0."}, "distance_mm": {"type": ["number", "string"], "description": "Extrude distance (mm), or a parameter-name string."}, "direction": {"type": "string", "enum": ["pos", "neg", "sym"]}, "operation": {"type": "string", "enum": ["new", "join", "cut", "intersect"]}, "overlap_mm": {"type": "number"}}, ["sketch_index", "distance_mm", "direction"]),
    handler=_fusion360_extrude,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Extrude a sketch into a solid; returns body_index.", emoji="🧱",
)

registry.register(
    name="fusion360_fillet",
    schema=_schema("fusion360_fillet", f"Fillet every edge of a body with a constant radius. {_COMMON}{_EXPR_NOTE}", {"body_index": {"type": "integer", "description": "From the last extrude result."}, "radius_mm": {"type": ["number", "string"], "description": "Fillet radius (mm), or a parameter-name string."}}, ["body_index", "radius_mm"]),
    handler=_fusion360_fillet,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Fillet all edges of a body.", emoji="⏺️",
)

registry.register(
    name="fusion360_mirror",
    schema=_schema("fusion360_mirror", f"Mirror a body across a plane, creating a symmetric copy (halves the work on any symmetric part). plane=xy/xz/yz origin plane; optional offset_mm offsets the mirror plane along its normal. {_COMMON}", {"body_index": {"type": "integer", "description": "The body to mirror (from the last extrude/revolve result)."}, "plane": {"type": "string", "enum": ["xy", "xz", "yz"], "description": "Mirror plane."}, "offset_mm": {"type": "number", "description": "Optional offset of the mirror plane (mm)."}}, ["body_index"]),
    handler=_fusion360_mirror,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Mirror a body across a plane; returns total body count.", emoji="🪞",
)

registry.register(
    name="fusion360_thread",
    schema=_schema("fusion360_thread", "Add a modeled thread to a cylindrical face on a body (screws/bolts/nuts). designation e.g. 'M6x1'. is_internal=False for a screw shank (external), True for a nut/hole (internal).", {"body_index": {"type": "integer"}, "designation": {"type": "string", "description": "e.g. M6x1, M8x1.25"}, "is_internal": {"type": "boolean"}}, ["body_index", "designation"]),
    handler=_fusion360_thread,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Add a modeled thread to a cylinder.", emoji="🔩",
)

registry.register(
    name="fusion360_info",
    schema=_schema("fusion360_info", "Report the current model state: body count, per-body face counts and bounding boxes (cm).", {}, []),
    handler=_fusion360_info,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Inspect the current model (bodies + bounding boxes).", emoji="🔎",
)

registry.register(
    name="fusion360_verify_build",
    schema=_schema(
        "fusion360_verify_build",
        "Deterministic build verification. The BACKEND compares the live scene against what you were "
        "supposed to build and returns PASS/FAIL — a wrong body count, duplicated (identical) bodies, "
        "missing parameters, or WRONG DIMENSIONS CANNOT be hidden. Call this at the END of every build, "
        "before reporting success, with expected_body_count = the number of parts in your todo plan. "
        "Pass expected_dimensions so the backend MEASURES each body's bbox and compares it to your "
        "declared sizes (this is what catches 'Ø11.5 instead of Ø12' silent failures). If it returns "
        "FAIL, fix the discrepancy (or honestly report it) — never claim success on a FAIL.",
        {
            "expected_body_count": {"type": "integer", "description": "The number of separate bodies your plan calls for (e.g. 5 for the 5-part coilover)."},
            "contract_id": {"type": "string", "description": "'use_last' (newest contract for this conversation) or a specific contract id. Validates live geometry against the STORED contract from fusion360_declare_spec."},
            "expected_params": {"type": "array", "items": {"oneOf": [{"type": "string"}, {"type": "object", "properties": {"name": {"type": "string"}, "value": {"type": "number"}}, "required": ["name", "value"]}]}, "description": "Optional list of user parameters that must exist AND have the right value. Entries may be a plain name string (must exist — e.g. 'eye_to_eye') or {name, value} (value in MILLIMETRES, backend compares the LIVE Fusion parameter with 5% tolerance — e.g. {'name':'eye_to_eye','value':80})."},
            "expected_dimensions": {
                "type": "array",
                "description": "Optional per-body dimension expectations the backend MEASURES and compares (mm). Each entry: {body_index, kind:'box'|'cylinder'|'hex', dims:{w,d,h} for box | {dia,len} for cylinder | {across_flats,height,faces?} for hex, tolerance_mm? default 0.5}. Declare the dims from YOUR plan for every body you built. Box uses the bbox w×d×h; cylinder uses the two near-equal bbox dims as diameter and the third as length; hex computes across-flats as min(w,d) (orientation-proof — the bbox measures corner-to-corner on one axis) and can optionally check faces (=8 for an unmodified hex prism).",
                "items": {
                    "type": "object",
                    "properties": {
                        "body_index": {"type": "integer"},
                        "kind": {"type": "string", "enum": ["box", "cylinder", "hex"]},
                        "dims": {"type": "object"},
                        "tolerance_mm": {"type": "number"},
                    },
                    "required": ["body_index", "kind", "dims"],
                },
            },
            "expected_probes": {
                "type": "array",
                "description": "Optional REAL-feature probes the backend measures inside bodies (holes/bores the bbox cannot see) and compares. Entries are the same as fusion360_probe queries, plus the expected value to compare: bore {type:'bore', body_index, approx_dia_mm, count?, dia_mm?, tolerance_mm?}; face {type:'face', body_index, which, area_mm2?}; mass {type:'mass', body_index, volume_mm3?}. Example: [{type:'bore', body_index:2, approx_dia_mm:5.5, count:1, dia_mm:5.5}].",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["bore", "face", "mass"]},
                        "body_index": {"type": "integer"},
                        "approx_dia_mm": {"type": "number"},
                        "which": {"type": "string", "enum": ["top", "bottom", "front", "back", "left", "right"]},
                        "min_depth_mm": {"type": "number", "description": "Bore: skip cylindrical faces shallower than this so Fusion thread faces don't overcount holes (use ~2mm for threaded holes)."},
                        "count": {"type": "integer"},
                        "dia_mm": {"type": "number"},
                        "area_mm2": {"type": "number"},
                        "volume_mm3": {"type": "number"},
                        "tolerance_mm": {"type": "number"},
                    },
                    "required": ["type", "body_index"],
                },
            },
        },
        ["expected_body_count"],
    ),
    handler=_fusion360_verify_build,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Verify a build against expected body count, params and DIMENSIONS; returns PASS/FAIL.", emoji="✅",
)
