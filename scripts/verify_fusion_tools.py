"""Live verification of every new Fusion 360 granular tool.

Runs inside the backend container (reach host.docker.internal:9876) and drives
the REAL handlers against the REAL Fusion bridge, so any code-gen bug surfaces
here rather than in a user conversation.

    docker exec -e PYTHONPATH=/app zhanlu-backend python /app/scripts/verify_fusion_tools.py
"""

from app.services.tool_handlers.fusion360_granular import (
    _fusion360_clear,
    _fusion360_extrude,
    _fusion360_info,
    _fusion360_sketch_circle,
    _fusion360_sketch_create,
)
from app.services.tool_handlers.fusion360_features import (
    _fusion360_box,
    _fusion360_chamfer,
    _fusion360_cylinder,
    _fusion360_combine,
    _fusion360_construction_plane,
    _fusion360_hole,
    _fusion360_loft,
    _fusion360_move,
    _fusion360_rectangular_pattern,
    _fusion360_shell,
    _fusion360_sphere,
    _fusion360_sweep,
    _fusion360_torus,
)
from app.services.tool_handlers.fusion360_sketch2 import (
    _fusion360_sketch_arc,
    _fusion360_sketch_arc_3point,
    _fusion360_sketch_line,
    _fusion360_sketch_spline,
)
from app.services.tool_handlers.fusion360_assembly import (
    _fusion360_component,
    _fusion360_joint_limits,
    _fusion360_measure,
    _fusion360_physical_properties,
    _fusion360_revolute_joint,
    _fusion360_rigid_joint,
    _fusion360_slider_joint,
)

RESULTS = []


def run(name, fn, args):
    try:
        r = fn(args, None, None)
        ok = bool(r.get("success"))
        tail = (r.get("stdout") or r.get("error") or "").strip().replace("\n", " | ")[-140:]
        RESULTS.append((ok, name, tail))
        print(("PASS " if ok else "FAIL ") + name + " :: " + tail)
    except Exception as e:  # noqa: BLE001
        RESULTS.append((False, name, repr(e)))
        print("EXC  " + name + " :: " + type(e).__name__ + " " + str(e)[:200])


# --- Group 1: primitives ---
run("clear", _fusion360_clear, {})
run("box", _fusion360_box, {"width_mm": 20, "depth_mm": 30, "height_mm": 40})
run("cylinder", _fusion360_cylinder, {"diameter_mm": 10, "height_mm": 50, "cx_mm": 30})
run("sphere", _fusion360_sphere, {"diameter_mm": 20, "cx_mm": -30})
run("torus", _fusion360_torus, {"major_dia_mm": 30, "minor_dia_mm": 10, "cz_mm": -40})

# --- Group 2: sketch curves ---
run("clear", _fusion360_clear, {})
r = _fusion360_sketch_create({"plane": "xy"}, None, None)
si = r.get("sketch_index")
run("sketch_create", _fusion360_sketch_create, {"plane": "xy"})
run("sketch_line", _fusion360_sketch_line, {"sketch_index": si, "x1_mm": 0, "y1_mm": 0, "x2_mm": 10, "y2_mm": 20})
run("sketch_arc", _fusion360_sketch_arc, {"sketch_index": si, "cx_mm": 0, "cy_mm": 0, "radius_mm": 5, "start_deg": 0, "sweep_deg": 180})
run("sketch_arc_3point", _fusion360_sketch_arc_3point, {"sketch_index": si, "points": [[0, 0], [5, 5], [10, 0]]})
run("sketch_spline", _fusion360_sketch_spline, {"sketch_index": si, "points": [[-10, 0], [-5, 8], [0, 0], [5, -8]]})

# --- Group 3: features on a box ---
run("clear", _fusion360_clear, {})
r = _fusion360_box({"width_mm": 40, "depth_mm": 40, "height_mm": 40}, None, None)
b0 = r.get("body_index")
run("shell", _fusion360_shell, {"body_index": b0, "thickness_mm": 2, "remove_face": "top"})
run("chamfer", _fusion360_chamfer, {"body_index": b0, "distance_mm": 2})
run("hole", _fusion360_hole, {"body_index": b0, "diameter_mm": 8, "cx_mm": 10, "cy_mm": 0, "cz_mm": 0})
run("rect_pattern", _fusion360_rectangular_pattern, {"body_index": b0, "axis_1": "x", "quantity_1": 3, "distance_1_mm": 50})
run("box2", _fusion360_box, {"width_mm": 10, "depth_mm": 10, "height_mm": 10, "cx_mm": 0, "cy_mm": 0, "cz_mm": 20})
r2 = _fusion360_box({"width_mm": 10, "depth_mm": 10, "height_mm": 10, "cx_mm": 0, "cy_mm": 0, "cz_mm": 20}, None, None)
run("combine", _fusion360_combine, {"target_body_index": b0, "tool_body_index": r2.get("body_index"), "operation": "cut"})

# --- Group 4: sweep + loft ---
run("clear", _fusion360_clear, {})
rp = _fusion360_sketch_create({"plane": "xy"}, None, None)
sp = rp.get("sketch_index")
run("sweep_profile", _fusion360_sketch_circle, {"sketch_index": sp, "cx_mm": 20, "cy_mm": 0, "radius_mm": 5})
rp2 = _fusion360_sketch_create({"plane": "xz"}, None, None)
spath = rp2.get("sketch_index")
run("sweep_path_line", _fusion360_sketch_line, {"sketch_index": spath, "x1_mm": 20, "y1_mm": 0, "x2_mm": 20, "y2_mm": 50})
run("sweep", _fusion360_sweep, {"profile_sketch_index": sp, "path_sketch_index": spath, "path_type": "line"})
r = _fusion360_sketch_create({"plane": "xy", "offset_mm": 30}, None, None)
run("loft_sk0", _fusion360_sketch_create, {"plane": "xy"})
run("loft_sk0_circle", _fusion360_sketch_circle, {"sketch_index": 0, "cx_mm": 0, "cy_mm": 0, "radius_mm": 10})
run("loft_sk1", _fusion360_sketch_create, {"plane": "xy", "offset_mm": 40})
run("loft_sk1_circle", _fusion360_sketch_circle, {"sketch_index": 1, "cx_mm": 0, "cy_mm": 0, "radius_mm": 15})
# need to clear to make sketch indices deterministic for loft
run("clear_for_loft", _fusion360_clear, {})
run("loft_s0", _fusion360_sketch_create, {"plane": "xy"})
run("loft_c0", _fusion360_sketch_circle, {"sketch_index": 0, "cx_mm": 0, "cy_mm": 0, "radius_mm": 10})
run("loft_s1", _fusion360_sketch_create, {"plane": "xy", "offset_mm": 40})
run("loft_c1", _fusion360_sketch_circle, {"sketch_index": 1, "cx_mm": 0, "cy_mm": 0, "radius_mm": 15})
run("loft", _fusion360_loft, {"sketch_indices": [0, 1]})

# --- Group 5: plane + move ---
run("clear", _fusion360_clear, {})
r = _fusion360_box({"width_mm": 20, "depth_mm": 20, "height_mm": 20}, None, None)
b1 = r.get("body_index")
run("construction_plane", _fusion360_construction_plane, {"plane": "xy", "offset_mm": 25})
run("move", _fusion360_move, {"body_index": b1, "dx_mm": 15, "dy_mm": 0, "dz_mm": 0})

# --- Group 6: component + joints ---
run("clear", _fusion360_clear, {})
run("component_tube", _fusion360_component, {"name": "TUBE"})
run("component_rod", _fusion360_component, {"name": "ROD"})
r = _fusion360_sketch_create({"plane": "xy", "component_index": 0}, None, None)
tsk = r.get("sketch_index")
run("tube_sketch", _fusion360_sketch_circle, {"sketch_index": tsk, "cx_mm": 0, "cy_mm": 0, "radius_mm": 12, "component_index": 0})
run("tube_extrude", _fusion360_extrude, {"sketch_index": tsk, "distance_mm": 80, "direction": "pos", "component_index": 0})
r = _fusion360_sketch_create({"plane": "xy", "component_index": 1}, None, None)
rsk = r.get("sketch_index")
run("rod_sketch", _fusion360_sketch_circle, {"sketch_index": rsk, "cx_mm": 0, "cy_mm": 0, "radius_mm": 8, "component_index": 1})
run("rod_extrude", _fusion360_extrude, {"sketch_index": rsk, "distance_mm": 100, "direction": "pos", "component_index": 1})
run("slider_joint", _fusion360_slider_joint, {"component_a": 0, "component_b": 1, "direction": "z", "min_mm": 0, "max_mm": 40})
run("joint_limits", _fusion360_joint_limits, {"joint_index": 0, "min_mm": 0, "max_mm": 30})

# revolute on a FRESH pair (Fusion rejects a 2nd joint between the same two components)
run("clear", _fusion360_clear, {})
run("component_r1", _fusion360_component, {"name": "R1"})
run("component_r2", _fusion360_component, {"name": "R2"})
for ci in (0, 1):
    r = _fusion360_sketch_create({"plane": "xy", "component_index": ci}, None, None)
    run(f"r{ci}_sketch", _fusion360_sketch_circle, {"sketch_index": r.get("sketch_index"), "cx_mm": 0, "cy_mm": 0, "radius_mm": 10, "component_index": ci})
    run(f"r{ci}_extrude", _fusion360_extrude, {"sketch_index": r.get("sketch_index"), "distance_mm": 30, "direction": "pos", "component_index": ci})
run("revolute_joint", _fusion360_revolute_joint, {"component_a": 0, "component_b": 1, "direction": "z", "min_deg": 0, "max_deg": 180})

# rigid on another fresh pair
run("clear", _fusion360_clear, {})
run("component_g1", _fusion360_component, {"name": "G1"})
run("component_g2", _fusion360_component, {"name": "G2"})
for ci in (0, 1):
    r = _fusion360_sketch_create({"plane": "xy", "component_index": ci}, None, None)
    run(f"g{ci}_sketch", _fusion360_sketch_circle, {"sketch_index": r.get("sketch_index"), "cx_mm": 0, "cy_mm": 0, "radius_mm": 10, "component_index": ci})
    run(f"g{ci}_extrude", _fusion360_extrude, {"sketch_index": r.get("sketch_index"), "distance_mm": 30, "direction": "pos", "component_index": ci})
run("rigid_joint", _fusion360_rigid_joint, {"component_a": 0, "component_b": 1})

# --- Group 7: physical props + measure ---
r = _fusion360_box({"width_mm": 10, "depth_mm": 10, "height_mm": 10}, None, None)
run("physical_properties", _fusion360_physical_properties, {"body_index": r.get("body_index")})
run("measure", _fusion360_measure, {"p1": [0, 0, 0], "p2": [30, 40, 50]})

# --- Group 8: info (components + bodies) ---
run("info", _fusion360_info, {})

print()
failed = [x for x in RESULTS if not x[0]]
print("==== SUMMARY: %d/%d passed ====" % (len(RESULTS) - len(failed), len(RESULTS)))
for ok, name, tail in failed:
    print("FAILED:", name, "::", tail)
