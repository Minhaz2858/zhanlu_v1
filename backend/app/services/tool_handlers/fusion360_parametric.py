"""Parametric + design-intent tools for the CAD agent (Tier 1).

Two granular ``fusion360_*`` tools (same bridge/pattern as the modeling tools):

- ``fusion360_params_list`` — read the document's userParameter table
  (name=value) as the model sees it, with values converted from Fusion's
  internal cm to mm. Call BEFORE an update to learn the existing parameter
  names, and AFTER defining parameters to confirm they exist.
- ``fusion360_declare_spec`` — store a design-intent contract for THIS
  conversation (part + features + notes). The backend persists it as a
  ``CadBuildContract`` row keyed to the conversation, and
  ``fusion360_verify_build`` validates the LIVE geometry against the stored
  contract — the model cannot fudge the spec after the fact.

The returned ``param_prefix`` (``c`` + first 8 chars of the contract id) is
the namespace the agent must use for parameter names (``<prefix>_<name>``),
so every parameter can be traced back to the contract that declared it.
"""

from __future__ import annotations

import uuid

from app.models.cad_build_contract import CadBuildContract
from app.services.tool_registry import registry
from app.services.tool_handlers.fusion360_granular import (
    MM,
    _apply_component,
    _bad,
    _run,
)


def _params_list_code() -> str:
    """Fusion snippet: print every user parameter as ``PARAM <name>=<value>``.

    Uses the same ``mm()`` units helper as the granular modeling tools. Fusion
    stores parameter values in internal cm; the backend converts to mm when
    parsing the output. Lines are printed as ``PARAM <name>=<value>`` plus a
    final ``PARAM_COUNT n``.
    """
    return (
        MM
        + "ups = design.userParameters\n"
        + "PARAM_NAME = 'PARAM'\n"
        + "for i in range(ups.count):\n"
        + "    p = ups.item(i)\n"
        + "    PARAM_VALUE = str(p.value)\n"
        + "    print(PARAM_NAME + ' ' + p.name + '=' + PARAM_VALUE)\n"
        + "print('PARAM_COUNT ' + str(ups.count))\n"
    )


def _fusion360_params_list(args, db, user_id):
    """List every user parameter in the document (name + value in mm)."""
    code = _apply_component(_params_list_code(), args.get("component_index"))
    r = _run(code, db)
    if not r.get("success"):
        return r
    stdout = r.get("stdout", "") or ""
    params = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("PARAM "):
            continue
        rest = line[len("PARAM "):]
        name, _, raw = rest.partition("=")
        name = name.strip()
        try:
            # Fusion's internal unit is cm; the UI/agent world is mm.
            value_mm = round(float(raw) * 10.0, 4)
        except (TypeError, ValueError):
            value_mm = raw  # non-numeric expression — surface the raw value
        params.append({"name": name, "value_mm": value_mm})
    return {"success": True, "params": params, "stdout": stdout, "count": len(params)}


def _validate_spec(args: dict) -> str | None:
    """Validate a design-intent contract spec. Returns an error string or None."""
    part = str(args.get("part") or "").strip()
    if not part:
        return "part is required (non-empty string naming the part to build)"
    features = args.get("features")
    if not isinstance(features, list) or not features:
        return "features must be a non-empty list of feature objects"
    # Contract verification (_feature_matches_body) only implements these
    # kinds — declare-time validation must match verify-time capability.
    supported = {"hex", "box", "cylinder"}
    for i, feat in enumerate(features):
        if not isinstance(feat, dict) or not str(feat.get("kind") or "").strip():
            return f"features[{i}] must be an object with a non-empty 'kind' (hex, box, cylinder)"
        kind = str(feat.get("kind") or "").strip().lower()
        if kind not in supported:
            return f"unsupported contract kind: {kind} (contract verification supports hex, box, cylinder)"
    return None


def _contract_prefix(contract_id: str) -> str:
    """Stable short prefix for parameter names: 'c' + first 8 hex chars of id."""
    compact = contract_id.replace("-", "")
    return "c" + compact[:8]


def _fusion360_declare_spec(args, db, user_id):
    """Store the design-intent contract for the current build/conversation."""
    err = _validate_spec(args)
    if err is not None:
        return _bad(err)
    # Runtime context (conversation_id, agent_app_id, org_id, app_id) is
    # populated by execute_tool into the TOOL_CONTEXT global. Read it lazily
    # and defensively — never let context plumbing break the tool.
    ctx = {}
    try:
        from app.services.agent_tools import TOOL_CONTEXT
        ctx = TOOL_CONTEXT or {}
    except Exception:  # noqa: BLE001
        ctx = {}
    contract_id = str(uuid.uuid4())
    row = CadBuildContract(
        id=contract_id,
        # org_id / app_id are NOT NULL on the base model — never pass None.
        org_id=ctx.get("org_id") or "default-org",
        app_id=ctx.get("agent_app_id") or ctx.get("app_id") or "default-app",
        conversation_id=ctx.get("conversation_id"),
        agent_id=ctx.get("agent_app_id"),
        created_by_id=user_id,
        contract_json={
            "part": args.get("part"),
            "features": args.get("features"),
            "notes": args.get("notes"),
        },
    )
    db.add(row)
    try:
        db.commit()
    except Exception as e:  # noqa: BLE001 — e.g. FK violation on a bad conversation_id
        db.rollback()
        return _bad(f"could not store contract: {e}")
    prefix = _contract_prefix(contract_id)
    return {
        "success": True,
        "contract_id": contract_id,
        "param_prefix": prefix,
        "message": (
            f"Contract {prefix[1:]} stored. Define parameters with the "
            f"'{prefix}_<name>' prefix (e.g. {prefix}_head_width) via "
            "fusion360_user_parameter, then verify with "
            "fusion360_verify_build(contract_id='use_last')."
        ),
    }


# ---------------------------------------------------------------------------
# Schemas & registration
# ---------------------------------------------------------------------------

FUSION360_PARAMS_LIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "fusion360_params_list",
        "description": (
            "List every user parameter currently defined in the Fusion document "
            "(name + value in mm). Call this BEFORE an update to see the parameter "
            "names to change, and after defining parameters to confirm they exist."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "component_index": {
                    "type": "integer",
                    "description": "Optional: read parameters inside a component (index from fusion360_component) instead of root.",
                },
            },
            "required": [],
        },
    },
}

FUSION360_DECLARE_SPEC_SCHEMA = {
    "type": "function",
    "function": {
        "name": "fusion360_declare_spec",
        "description": (
            "Declare the design-intent contract for the CURRENT build BEFORE any "
            "modeling tool call. features = one object per sub-part/body with 'kind' "
            "(hex, box, cylinder) plus its dimensions in mm "
            "(e.g. {kind:'hex', across_flats:8}, {kind:'cylinder', diameter:5, "
            "height:20}). The backend stores the contract and verify_build validates "
            "the live geometry against it — the model cannot fudge the spec. Returns "
            "contract_id + param_prefix; use param_prefix for parameter names."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "part": {
                    "type": "string",
                    "description": "Name of the part being built (e.g. 'coilover shock').",
                },
                "features": {
                    "type": "array",
                    "description": "One object per sub-part/body: {kind, ...dims in mm}.",
                    "items": {"type": "object"},
                },
                "notes": {
                    "type": "string",
                    "description": "Optional design-intent notes (materials, tolerances, assembly hints).",
                },
            },
            "required": ["part", "features"],
        },
    },
}

registry.register(
    name="fusion360_params_list",
    schema=FUSION360_PARAMS_LIST_SCHEMA,
    handler=_fusion360_params_list,
    category="cad",
    toolset="cad",
    enabled_by_default=True,
    description="List document user parameters (name, value_mm).",
    emoji="🔢",
)

registry.register(
    name="fusion360_declare_spec",
    schema=FUSION360_DECLARE_SPEC_SCHEMA,
    handler=_fusion360_declare_spec,
    category="cad",
    toolset="cad",
    enabled_by_default=True,
    description="Store the design-intent contract for the current build.",
    emoji="📐",
)
