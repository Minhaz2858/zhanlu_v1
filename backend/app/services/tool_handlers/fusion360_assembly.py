"""Fusion 360 assembly tools — components, joints, limits, physical properties.

Adds the ASSEMBLY half of the granular layer: create named components, connect
them with rigid/revolute/slider joints, set joint limits, and inspect physical
properties. These are what make a multi-part model (e.g. a shock absorber) a
true assembly of separate components with relative motion.

All joint recipes verified live against Fusion 360 (2026-08-19):
- JointGeometry.createByCylinderOrConeFace(face, JointQuadrantAngleTypes.X,
  JointKeyPointTypes.EndKeyPoint)  (CenterKeyPoint is INVALID for cylinder faces)
- JointInput.setAsSliderJointMotion(JointDirections.ZAxisJointDirection, geom)
- joint.jointMotion: jointType 0=rigid 1=revolute 2=slider; slider -> slideLimits,
  revolute -> rotationLimits.

Units: mm -> cm internally. ``fusion360_measure`` is computed in the backend
(no Fusion call).
"""

from __future__ import annotations

import math

from app.services.tool_registry import registry
from app.services.tool_handlers.fusion360_granular import (
    MM,
    _apply_component,
    _bad,
    _last_int,
    _run,
    _schema,
)

_COMMON = "Units are MILLIMETRES for all *_mm params. The bridge converts to Fusion's cm internally."

_JOINT_DIR = {
    "x": "adsk.fusion.JointDirections.XAxisJointDirection",
    "y": "adsk.fusion.JointDirections.YAxisJointDirection",
    "z": "adsk.fusion.JointDirections.ZAxisJointDirection",
}

# Snippet: find the first cylindrical face on component N's first body -> `face`.
_CYL_FACE = (
    "def _cylface(oc):\n"
    "    b = oc.component.bRepBodies.item(0)\n"
    "    for f in b.faces:\n"
    "        if f.geometry.objectType == adsk.core.Cylinder.classType():\n"
    "            return f\n"
    "    return None\n"
)

# Snippet: find the first planar face (and one of its edges) -> `face`, `edge`.
_PLANAR_FACE = (
    "def _plface(oc):\n"
    "    b = oc.component.bRepBodies.item(0)\n"
    "    for f in b.faces:\n"
    "        if f.geometry.objectType.endswith('Plane'):\n"
    "            for e in f.edges:\n"
    "                return f, e\n"
    "    return None, None\n"
)


def _occurrence(idx: int) -> str:
    return f"root.occurrences.item({idx})"


# ---------------------------------------------------------------------------
# 1. component
# ---------------------------------------------------------------------------
def _fusion360_component(args, db, user_id):
    name = (args.get("name") or "Component").strip().replace("'", "")
    if not name:
        return _bad("name is required")
    code = (
        MM
        + "occ = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())\n"
        + f"occ.component.name = '{name}'\n"
        + "print('COMPONENT_INDEX', root.occurrences.count - 1)\n"
    )
    r = _run(code, db)
    if not r["success"]:
        return r
    return {"success": True, "component_index": _last_int(r["stdout"], "COMPONENT_INDEX"), "stdout": r["stdout"]}


# ---------------------------------------------------------------------------
# 2. slider joint
# ---------------------------------------------------------------------------
def _fusion360_slider_joint(args, db, user_id):
    ca = args.get("component_a")
    cb = args.get("component_b")
    if ca is None or cb is None:
        return _bad("component_a and component_b (component indices) are required")
    direction = args.get("direction", "z")
    if direction not in _JOINT_DIR:
        return _bad(f"direction must be one of {sorted(_JOINT_DIR)}")
    min_mm = float(args.get("min_mm", 0) or 0)
    max_mm = float(args.get("max_mm", 0) or 0)
    limits = []
    if min_mm != 0:
        limits.append(f"jm.slideLimits.isMinimumValueEnabled = True\njm.slideLimits.minimumValue = {min_mm / 10.0}")
    if max_mm != 0:
        limits.append(f"jm.slideLimits.isMaximumValueEnabled = True\njm.slideLimits.maximumValue = {max_mm / 10.0}")
    limits_code = "\n".join(limits)
    code = (
        MM
        + _CYL_FACE
        + f"f1 = _cylface({_occurrence(int(ca))})\n"
        + f"f2 = _cylface({_occurrence(int(cb))})\n"
        + "if f1 is None or f2 is None:\n"
        + "    print('ERROR: both components need a cylindrical body for a slider joint')\n"
        + "else:\n"
        + "    Q = adsk.fusion.JointQuadrantAngleTypes.MiddleJointQuadrantAngleType\n"
        + "    K = adsk.fusion.JointKeyPointTypes.EndKeyPoint\n"
        + "    jg1 = adsk.fusion.JointGeometry.createByCylinderOrConeFace(f1, Q, K)\n"
        + "    jg2 = adsk.fusion.JointGeometry.createByCylinderOrConeFace(f2, Q, K)\n"
        + "    ji = root.joints.createInput(jg1, jg2)\n"
        + f"    ji.setAsSliderJointMotion({_JOINT_DIR[direction]}, jg1)\n"
        + "    joint = root.joints.add(ji)\n"
        + "    jm = joint.jointMotion\n"
        + (("\n    " + limits_code.replace("\n", "\n    ")) if limits_code else "")
        + "\n    print('JOINT_INDEX', root.joints.count - 1, 'jointType', jm.jointType)\n"
    )
    r = _run(code, db)
    if not r["success"]:
        return r
    return {"success": True, "joint_index": _last_int(r["stdout"], "JOINT_INDEX"), "stdout": r["stdout"]}


# ---------------------------------------------------------------------------
# 3. revolute joint
# ---------------------------------------------------------------------------
def _fusion360_revolute_joint(args, db, user_id):
    ca = args.get("component_a")
    cb = args.get("component_b")
    if ca is None or cb is None:
        return _bad("component_a and component_b (component indices) are required")
    direction = args.get("direction", "z")
    if direction not in _JOINT_DIR:
        return _bad(f"direction must be one of {sorted(_JOINT_DIR)}")
    min_deg = float(args.get("min_deg", 0) or 0)
    max_deg = float(args.get("max_deg", 0) or 0)
    limits = []
    if min_deg != 0:
        limits.append(f"jm.rotationLimits.isMinimumValueEnabled = True\njm.rotationLimits.minimumValue = {math.radians(min_deg)}")
    if max_deg != 0:
        limits.append(f"jm.rotationLimits.isMaximumValueEnabled = True\njm.rotationLimits.maximumValue = {math.radians(max_deg)}")
    limits_code = "\n".join(limits)
    code = (
        MM
        + _CYL_FACE
        + f"f1 = _cylface({_occurrence(int(ca))})\n"
        + f"f2 = _cylface({_occurrence(int(cb))})\n"
        + "if f1 is None or f2 is None:\n"
        + "    print('ERROR: both components need a cylindrical body for a revolute joint')\n"
        + "else:\n"
        + "    Q = adsk.fusion.JointQuadrantAngleTypes.MiddleJointQuadrantAngleType\n"
        + "    K = adsk.fusion.JointKeyPointTypes.EndKeyPoint\n"
        + "    jg1 = adsk.fusion.JointGeometry.createByCylinderOrConeFace(f1, Q, K)\n"
        + "    jg2 = adsk.fusion.JointGeometry.createByCylinderOrConeFace(f2, Q, K)\n"
        + "    ji = root.joints.createInput(jg1, jg2)\n"
        + f"    ji.setAsRevoluteJointMotion({_JOINT_DIR[direction]}, jg1)\n"
        + "    joint = root.joints.add(ji)\n"
        + "    jm = joint.jointMotion\n"
        + (("\n    " + limits_code.replace("\n", "\n    ")) if limits_code else "")
        + "\n    print('JOINT_INDEX', root.joints.count - 1, 'jointType', jm.jointType)\n"
    )
    r = _run(code, db)
    if not r["success"]:
        return r
    return {"success": True, "joint_index": _last_int(r["stdout"], "JOINT_INDEX"), "stdout": r["stdout"]}


# ---------------------------------------------------------------------------
# 4. rigid joint
# ---------------------------------------------------------------------------
def _fusion360_rigid_joint(args, db, user_id):
    ca = args.get("component_a")
    cb = args.get("component_b")
    if ca is None or cb is None:
        return _bad("component_a and component_b (component indices) are required")
    code = (
        MM
        + _CYL_FACE
        + _PLANAR_FACE
        + f"f1 = _cylface({_occurrence(int(ca))})\n"
        + f"f2 = _cylface({_occurrence(int(cb))})\n"
        + "if f1 is not None and f2 is not None:\n"
        + "    Q = adsk.fusion.JointQuadrantAngleTypes.MiddleJointQuadrantAngleType\n"
        + "    K = adsk.fusion.JointKeyPointTypes.EndKeyPoint\n"
        + "    jg1 = adsk.fusion.JointGeometry.createByCylinderOrConeFace(f1, Q, K)\n"
        + "    jg2 = adsk.fusion.JointGeometry.createByCylinderOrConeFace(f2, Q, K)\n"
        + "else:\n"
        + "    K = adsk.fusion.JointKeyPointTypes.CenterKeyPoint\n"
        + f"    p1, e1 = _plface({_occurrence(int(ca))})\n"
        + f"    p2, e2 = _plface({_occurrence(int(cb))})\n"
        + "    jg1 = adsk.fusion.JointGeometry.createByPlanarFace(p1, e1, K)\n"
        + "    jg2 = adsk.fusion.JointGeometry.createByPlanarFace(p2, e2, K)\n"
        + "ji = root.joints.createInput(jg1, jg2)\n"
        + "ji.setAsRigidJointMotion()\n"
        + "joint = root.joints.add(ji)\n"
        + "print('JOINT_INDEX', root.joints.count - 1, 'jointType', joint.jointMotion.jointType)\n"
    )
    r = _run(code, db)
    if not r["success"]:
        return r
    return {"success": True, "joint_index": _last_int(r["stdout"], "JOINT_INDEX"), "stdout": r["stdout"]}


# ---------------------------------------------------------------------------
# 5. joint limits (update an existing joint's min/max)
# ---------------------------------------------------------------------------
def _fusion360_joint_limits(args, db, user_id):
    ji = args.get("joint_index")
    if ji is None:
        return _bad("joint_index is required (from the joint tool result)")
    min_v = args.get("min_mm")
    max_v = args.get("max_mm")
    code = (
        MM
        + f"jm = root.joints.item({int(ji)}).jointMotion\n"
        + "jt = jm.jointType\n"
    )
    code += "if jt == 2:\n"
    lines = []
    if min_v is not None:
        lines.append(f"    jm.slideLimits.isMinimumValueEnabled = True\n    jm.slideLimits.minimumValue = {float(min_v) / 10.0}")
    if max_v is not None:
        lines.append(f"    jm.slideLimits.isMaximumValueEnabled = True\n    jm.slideLimits.maximumValue = {float(max_v) / 10.0}")
    if lines:
        code += "\n".join(lines) + "\n"
    code += "elif jt == 1:\n"
    lines = []
    if min_v is not None:
        lines.append(f"    jm.rotationLimits.isMinimumValueEnabled = True\n    jm.rotationLimits.minimumValue = {math.radians(float(min_v))}")
    if max_v is not None:
        lines.append(f"    jm.rotationLimits.isMaximumValueEnabled = True\n    jm.rotationLimits.maximumValue = {math.radians(float(max_v))}")
    if lines:
        code += "\n".join(lines) + "\n"
    code += (
        "print('JOINT_LIMITS', jt, 'slide', getattr(jm, 'slideValue', None), 'rotation', getattr(jm, 'rotationValue', None))\n"
    )
    return _run(code, db)


# ---------------------------------------------------------------------------
# 6. physical properties
# ---------------------------------------------------------------------------
def _fusion360_physical_properties(args, db, user_id):
    bi = args.get("body_index")
    if bi is None:
        return _bad("body_index is required")
    code = (
        MM
        + f"body = root.bRepBodies.item({int(bi)})\n"
        + "pp = body.physicalProperties\n"
        + "print('VOLUME_CM3', round(pp.volume, 4))\n"
        + "print('MASS_KG', round(pp.mass, 6))\n"
        + "com = pp.centerOfMass\n"
        + "print('COM_CM', [round(x, 4) for x in com.asArray()])\n"
    )
    code = _apply_component(code, args.get("component_index"))
    return _run(code, db)


# ---------------------------------------------------------------------------
# 7. measure (backend distance)
# ---------------------------------------------------------------------------
def _fusion360_measure(args, db, user_id):
    p1 = args.get("p1")
    p2 = args.get("p2")
    if not p1 or not p2 or len(p1) != 3 or len(p2) != 3:
        return _bad("p1 and p2 must be [x_mm, y_mm, z_mm]")
    dx = float(p1[0]) - float(p2[0])
    dy = float(p1[1]) - float(p2[1])
    dz = float(p1[2]) - float(p2[2])
    dist = math.sqrt(dx * dx + dy * dy + dz * dz)
    return {"success": True, "distance_mm": round(dist, 4), "stdout": f"distance {round(dist, 4)} mm"}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
registry.register(
    name="fusion360_component",
    schema=_schema("fusion360_component", "Create a new named component (an occurrence in the root). Returns component_index — pass it as component_index to sketch/extrude/revolve/etc. to build INSIDE that component.", {"name": {"type": "string"}}, ["name"]),
    handler=_fusion360_component,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Create a new component; returns component_index.", emoji="🧩",
)

registry.register(
    name="fusion360_slider_joint",
    schema=_schema("fusion360_slider_joint", f"Create a slider joint (linear motion along direction) between two components that each contain a cylindrical body. Optional min_mm/max_mm set the stroke limits. {_COMMON}", {"component_a": {"type": "integer"}, "component_b": {"type": "integer"}, "direction": {"type": "string", "enum": ["x", "y", "z"]}, "min_mm": {"type": "number"}, "max_mm": {"type": "number"}}, ["component_a", "component_b"]),
    handler=_fusion360_slider_joint,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Create a slider joint between two components.", emoji="↕️",
)

registry.register(
    name="fusion360_revolute_joint",
    schema=_schema("fusion360_revolute_joint", f"Create a revolute (hinge) joint between two components that each contain a cylindrical body. Optional min_deg/max_deg set rotation limits. {_COMMON}", {"component_a": {"type": "integer"}, "component_b": {"type": "integer"}, "direction": {"type": "string", "enum": ["x", "y", "z"]}, "min_deg": {"type": "number"}, "max_deg": {"type": "number"}}, ["component_a", "component_b"]),
    handler=_fusion360_revolute_joint,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Create a revolute (hinge) joint between two components.", emoji="🔩",
)

registry.register(
    name="fusion360_rigid_joint",
    schema=_schema("fusion360_rigid_joint", "Lock two components together with a rigid joint (no relative motion).", {"component_a": {"type": "integer"}, "component_b": {"type": "integer"}}, ["component_a", "component_b"]),
    handler=_fusion360_rigid_joint,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Create a rigid joint between two components.", emoji="🔒",
)

registry.register(
    name="fusion360_joint_limits",
    schema=_schema("fusion360_joint_limits", f"Set the min/max limits on an existing joint (min_mm/max_mm for slider joints — interpreted as degrees for revolute joints). {_COMMON}", {"joint_index": {"type": "integer"}, "min_mm": {"type": "number"}, "max_mm": {"type": "number"}}, ["joint_index"]),
    handler=_fusion360_joint_limits,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Set limits on an existing joint.", emoji="🎚️",
)

registry.register(
    name="fusion360_physical_properties",
    schema=_schema("fusion360_physical_properties", "Report a body's volume (cm^3), mass (kg), and center of mass (cm).", {"body_index": {"type": "integer"}}, ["body_index"]),
    handler=_fusion360_physical_properties,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Report volume/mass/center-of-mass of a body.", emoji="⚖️",
)

registry.register(
    name="fusion360_measure",
    schema=_schema("fusion360_measure", "Distance between two points, computed in the backend (no Fusion call).", {"p1": {"type": "array", "items": {"type": "number"}, "description": "[x_mm, y_mm, z_mm]"}, "p2": {"type": "array", "items": {"type": "number"}, "description": "[x_mm, y_mm, z_mm]"}}, ["p1", "p2"]),
    handler=_fusion360_measure,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Measure distance between two points (mm).", emoji="📏",
)
