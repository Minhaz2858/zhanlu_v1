"""Fusion 360 measurement probes — REAL internal-geometry truth (2026-08-28).

The bounding-box verifier cannot see inside a body: a Ø12 washer with a Ø5
through-hole has the SAME bounding box as a solid puck. Production sign-off
needs measurements of the real features. ``fusion360_probe`` measures the LIVE
geometry with Fusion's actual API:

- ``bore``  — find cylindrical faces (holes) inside a body by approximate
  diameter, and report each hole's real diameter + depth.
- ``face``  — find a named face (top/bottom/front/back/left/right — by the
  extreme centroid along that axis) and report its area + edge count.
- ``mass``  — volume (mm³) + mass (g) from the body's physical properties.

Each query returns measured values the AGENT compares to the spec, and
``fusion360_verify_build(expected_probes=...)`` compares them DETERMINISTICALLY
in the backend (imported lazily to avoid a circular import with granular).

The snippet runs inside Fusion over the same socket bridge as every other
granular tool; units are mm in the JSON, cm inside the adsk code.
"""

from __future__ import annotations

import json

from app.services.tool_registry import registry
from app.services.tool_handlers.fusion360_granular import (
    _apply_component,
    _run,
    _schema,
)

# ---------------------------------------------------------------------------
# Snippet builder (shared with fusion360_verify_build's expected_probes)
# ---------------------------------------------------------------------------
def _probe_snippet(queries: list[dict]) -> str:
    """Build the adsk Python that measures each query and prints PROBE <json>."""
    qj = json.dumps(queries, ensure_ascii=True)
    return (
        "import adsk.core, adsk.fusion, json, math\n"
        f"_queries = json.loads('{qj}')\n"
        "_bs = {}\n"
        "for _i in range(root.bRepBodies.count):\n"
        "    _bs[_i] = root.bRepBodies.item(_i)\n"
        "_out = []\n"
        "for _q in _queries:\n"
        "    try:\n"
        "        _bi = _q.get('body_index')\n"
        "        _b = _bs.get(_bi)\n"
        "        if _b is None:\n"
        "            _out.append({'ok': False, 'query': _q, 'error': 'body %s not found' % _bi})\n"
        "            continue\n"
        "        _t = _q.get('type')\n"
        "        if _t == 'bore':\n"
        "            _approx = float(_q.get('approx_dia_mm', 5.0)) / 10.0\n"
        "            _tol = float(_q.get('tolerance_mm', 1.0)) / 10.0\n"
        "            _mind = float(_q.get('min_depth_mm', 0.0)) / 10.0\n"
        "            _hits = []\n"
        "            for _f in _b.faces:\n"
        "                _g = _f.geometry\n"
        "                if _g.objectType == adsk.core.Cylinder.classType():\n"
        "                    _r = _g.radius\n"
        "                    _dia = _r * 2.0\n"
        "                    if abs(_dia - _approx) <= _tol:\n"
        "                        _depth = _f.area / (2.0 * math.pi * _r) if _r > 1e-9 else 0.0\n"
        "                        if _depth >= _mind:\n"
        "                            _hits.append({'dia_mm': round(_dia * 10.0, 2), 'depth_mm': round(_depth * 10.0, 2)})\n"
        "            _out.append({'ok': True, 'query': _q, 'bores': _hits, 'count': len(_hits)})\n"
        "        elif _t == 'face':\n"
        "            _which = _q.get('which', 'top')\n"
        "            _best = None; _best_score = None; _best_area = 0.0; _best_cent = None\n"
        "            for _f in _b.faces:\n"
        "                _c = _f.centroid\n"
        "                _score = {'top': _c.z, 'bottom': -_c.z, 'front': _c.y,\n"
        "                          'back': -_c.y, 'right': _c.x, 'left': -_c.x}.get(_which)\n"
        "                if _score is None:\n"
        "                    continue\n"
        "                if _best is None or _score > _best_score:\n"
        "                    _best = _f; _best_score = _score\n"
        "                    _best_area = _f.area; _best_cent = _c\n"
        "            if _best is not None:\n"
        "                _loops = 0\n"
        "                for _lp in _best.loops:\n"
        "                    _loops += _lp.edges.count\n"
        "                _out.append({'ok': True, 'query': _q, 'face': {\n"
        "                    'area_mm2': round(_best_area * 100.0, 2),\n"
        "                    'edges': _loops,\n"
        "                    'centroid_mm': [round(_best_cent.x * 10.0, 2), round(_best_cent.y * 10.0, 2), round(_best_cent.z * 10.0, 2)]}})\n"
        "            else:\n"
        "                _out.append({'ok': False, 'query': _q, 'error': 'no %s face found' % _which})\n"
        "        elif _t == 'mass':\n"
        "            _pp = _b.physicalProperties\n"
        "            _out.append({'ok': True, 'query': _q, 'mass': {\n"
        "                'volume_mm3': round(_pp.volume * 1000.0, 2),\n"
        "                'mass_g': round(_pp.mass * 1000.0, 3)}})\n"
        "        else:\n"
        "            _out.append({'ok': False, 'query': _q, 'error': 'unknown probe type %s' % _t})\n"
        "    except Exception as _e:\n"
        "        _out.append({'ok': False, 'query': _q, 'error': str(_e)})\n"
        "print('PROBE ' + json.dumps(_out))\n"
    )


def _parse_probe_stdout(stdout: str) -> list[dict]:
    """Parse the PROBE <json> line into the measured list (order preserved)."""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if line.startswith("PROBE "):
            try:
                parsed = json.loads(line[6:])
                if isinstance(parsed, list):
                    return parsed
            except (ValueError, TypeError):
                return []
    return []


def _compare_probe(q: dict, m: dict, issues: list[str]) -> None:
    """Deterministic spec-vs-measured comparison for one probe query."""
    tol = float(q.get("tolerance_mm", 0.5))
    if not m.get("ok"):
        issues.append("probe %s: %s" % (q.get("type"), m.get("error", "unknown")))
        return
    t = q.get("type")
    if t == "bore":
        exp_count = q.get("count")
        exp_dia = q.get("dia_mm")
        if exp_count is not None and m.get("count") != int(exp_count):
            issues.append("probe bore: %d hole(s) found, expected %s" % (m.get("count"), exp_count))
        if exp_dia is not None:
            for h in (m.get("bores") or []):
                if abs(h["dia_mm"] - float(exp_dia)) > tol:
                    issues.append("probe bore: Ø%.2fmm != expected Ø%sm (±%s)" % (h["dia_mm"], exp_dia, tol))
    elif t == "face":
        exp_area = q.get("area_mm2")
        f = m.get("face") or {}
        if exp_area is not None and abs(f.get("area_mm2", 0.0) - float(exp_area)) > max(tol, float(exp_area) * 0.05):
            issues.append("probe face %s: area %.1fmm² != expected %smm²" % (q.get("which"), f.get("area_mm2"), exp_area))
    elif t == "mass":
        exp_vol = q.get("volume_mm3")
        ms = m.get("mass") or {}
        if exp_vol is not None and abs(ms.get("volume_mm3", 0.0) - float(exp_vol)) > max(tol * 100, float(exp_vol) * 0.05):
            issues.append("probe mass: volume %.0fmm³ != expected %smm³" % (ms.get("volume_mm3"), exp_vol))


# ---------------------------------------------------------------------------
# Tool: fusion360_probe
# ---------------------------------------------------------------------------
def _fusion360_probe(args, db, user_id):
    queries = args.get("queries") or []
    if not queries:
        from app.services.tool_handlers.fusion360_granular import _bad
        return _bad("queries is required: [ {type:'bore'|'face'|'mass', body_index, ...} ]")
    code = _apply_component(_probe_snippet(queries), args.get("component_index"))
    r = _run(code, db)
    if not r["success"]:
        return r
    measured = _parse_probe_stdout(r.get("stdout", ""))
    return {
        "success": True,
        "probes": measured,
        "summary": (
            "; ".join(
                "%s body%s: %s" % (
                    (p.get("query") or {}).get("type", "?"),
                    (p.get("query") or {}).get("body_index", "?"),
                    json.dumps({k: v for k, v in p.items() if k not in ("query", "ok")}, ensure_ascii=False),
                )
                for p in measured
            )
        ),
    }


registry.register(
    name="fusion360_probe",
    schema=_schema(
        "fusion360_probe",
        "Measure REAL features of the live model with Fusion's geometry API (not bounding boxes): "
        "bore = find cylindrical faces (holes) in a body by approximate diameter and report each "
        "hole's actual diameter + depth; face = find a named face (top/bottom/front/back/left/right "
        "by extreme centroid along that axis) and report area + edges; mass = volume (mm3) + mass (g). "
        "Use this to verify features the bbox can't see — holes, bores, face areas. Pass several "
        "queries at once: {\"queries\":[{...},{...}]}.",
        {
            "queries": {
                "type": "array",
                "description": "Probe queries. bore: {type:'bore', body_index, approx_dia_mm, tolerance_mm?}. face: {type:'face', body_index, which:'top'|'bottom'|'front'|'back'|'left'|'right'}. mass: {type:'mass', body_index}.",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["bore", "face", "mass"]},
                        "body_index": {"type": "integer"},
                        "approx_dia_mm": {"type": "number", "description": "Bore: expected hole diameter in mm (search tolerance)."},
                        "tolerance_mm": {"type": "number", "description": "Search/comparison tolerance in mm (default 0.5)."},
                        "min_depth_mm": {"type": "number", "description": "Bore: ignore cylindrical faces shallower than this (default 0). Use ~2mm+ to skip Fusion thread-representation faces so a threaded hole counts as ONE bore."},
                        "which": {"type": "string", "enum": ["top", "bottom", "front", "back", "left", "right"]},
                    },
                    "required": ["type", "body_index"],
                },
            },
        },
        ["queries"],
    ),
    handler=_fusion360_probe,
    category="cad", toolset="cad", enabled_by_default=True,
    description="Measure real features (bores, faces, mass) of the live model.", emoji="📐",
)
