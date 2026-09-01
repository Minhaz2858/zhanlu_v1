"""Build the 5-body coilover shock (lower eyelet, damper body, shaft, spring, collar).

Driven directly over the Fusion bridge via the verified granular/advanced tool
handlers (same path the CAD Agent uses).  Units: mm (handlers convert to cm).
Shock axis = Z.  Lower eye at Z=0, eye-to-eye 210mm (shaft top at Z=210).
"""
import json, sys
from app.services.tool_handlers.fusion360_granular import (
    _fusion360_sketch_create, _fusion360_sketch_circle, _fusion360_sketch_rectangle,
    _fusion360_extrude, _fusion360_info, _fusion360_verify_build,
)
from app.services.tool_handlers.fusion360_advanced import (
    _fusion360_user_parameter, _fusion360_revolve, _fusion360_coil,
)


def call(fn, args, label):
    try:
        r = fn(args, None, None)
    except Exception as e:
        r = {"success": False, "error": f"{type(e).__name__}: {e}"}
        print(f"XX {label}: {json.dumps(r)}", file=sys.stderr)
        return r
    print(f"{'OK ' if r.get('success') else 'XX '} {label}: {json.dumps(r)}")
    return r


# 1. user parameters (resume hooks + parametric reference)
for name, val in [("eye_to_eye", 210), ("damper_len", 95), ("damper_dia", 32),
                  ("spring_mean_dia", 44), ("wire_dia", 7.5), ("shaft_len", 109),
                  ("eyelet_dia", 22), ("bore_dia", 8)]:
    call(_fusion360_user_parameter, {"name": name, "value": val, "units": "mm"}, f"param {name}")

# 2. lower eyelet — O22 puck centered at Z=0 (Z -6..+6), O8 horizontal cross-bore at Z=0
s0 = call(_fusion360_sketch_create, {"plane": "xy", "offset_mm": -6}, "eyelet puck sketch")
call(_fusion360_sketch_circle, {"sketch_index": s0.get("sketch_index"), "radius_mm": 11}, "eyelet O22 circle")
call(_fusion360_extrude, {"sketch_index": s0.get("sketch_index"), "distance_mm": 12,
                          "direction": "pos", "operation": "new"}, "eyelet puck extrude")
s1 = call(_fusion360_sketch_create, {"plane": "xz", "offset_mm": 0}, "eyelet bore sketch")
call(_fusion360_sketch_circle, {"sketch_index": s1.get("sketch_index"), "radius_mm": 4}, "bore O8 circle")
call(_fusion360_extrude, {"sketch_index": s1.get("sketch_index"), "distance_mm": 30,
                          "direction": "sym", "operation": "cut"}, "bore cross-cut")

# 3. damper body — O32 x 95mm (Z 6..101)
s2 = call(_fusion360_sketch_create, {"plane": "xy", "offset_mm": 6}, "damper sketch")
call(_fusion360_sketch_circle, {"sketch_index": s2.get("sketch_index"), "radius_mm": 16}, "damper O32 circle")
call(_fusion360_extrude, {"sketch_index": s2.get("sketch_index"), "distance_mm": 95,
                          "direction": "pos", "operation": "new"}, "damper extrude")

# 4. shaft — O14 rod, Z 101..210 (eye-to-eye)
s3 = call(_fusion360_sketch_create, {"plane": "xy", "offset_mm": 101}, "shaft sketch")
call(_fusion360_sketch_circle, {"sketch_index": s3.get("sketch_index"), "radius_mm": 7}, "shaft O14 circle")
call(_fusion360_extrude, {"sketch_index": s3.get("sketch_index"), "distance_mm": 109,
                          "direction": "pos", "operation": "new"}, "shaft extrude")

# 5. preload collar — ring O40/O33 x 10mm (Z 6..16), revolved
s4 = call(_fusion360_sketch_create, {"plane": "xz", "offset_mm": 0}, "collar sketch")
call(_fusion360_sketch_rectangle, {"sketch_index": s4.get("sketch_index"),
                                   "x1_mm": 16.5, "y1_mm": -6, "x2_mm": 20, "y2_mm": -16}, "collar rect")
call(_fusion360_revolve, {"sketch_index": s4.get("sketch_index"), "axis": "z",
                          "angle_deg": 360, "operation": "new"}, "collar revolve")

# 6. spring — mean O44, 7.5mm wire, 8 coils
call(_fusion360_coil, {"mean_dia_mm": 44, "wire_dia_mm": 7.5, "coils": 8,
                       "pitch_mm": 10, "z_start_mm": 16, "direction": "pos"}, "spring coil")

# verify
call(_fusion360_info, {}, "info")
v = call(_fusion360_verify_build, {"expected_body_count": 5,
    "expected_params": ["eye_to_eye", "damper_len", "spring_mean_dia", "wire_dia",
                        "shaft_len", "eyelet_dia", "bore_dia"]}, "verify")
print("FINAL_VERIFY:", json.dumps(v))
