# CAD P1 — DFM Rule Engine Implementation Plan

**Goal:** deterministic manufacturability checking (CNC + sheet metal) over
measured values — issue → reason → repair JSON — exposed as the
`fusion360_dfm_check` tool and nudged in the v3 loop after a PASS verify.

**Architecture:** pure-Python rule functions in `backend/app/services/dfm/`
(NEVER calls the LLM; measurements come from probe/info tool results or the
agent's tool-call history). The tool handler is a thin validator + delegator.
Rules are unit-tested without a Fusion bridge.

**Rule output contract:**
```json
{"rule": str, "severity": "error|warning|info", "measured": str,
 "required": str, "reason": str, "repair": {"option": str, ...} | null,
 "auto_fixable": bool}
```

Tasks:
1. `backend/tests/test_dfm_rules.py` — RED (rules module missing)
2. `backend/app/services/dfm/{__init__,rules}.py` — material profiles + CNC
   rules (corner radius, pocket depth ratio, hole-to-edge, wall thickness) — GREEN
3. Sheet-metal rules (bend radius factor, flange length, hole-to-bend, stock
   thickness) + verdict summary — GREEN
4. `backend/tests/test_fusion360_dfm_check.py` — RED; then
   `backend/app/services/tool_handlers/fusion360_dfm.py` handler + schema +
   registry — GREEN
5. v3-loop DFM nudge (mirror the verify nudge; source-level guard test in
   test_chat_regenerate_citations-style style — nudge reason "dfm_nudge")
6. Restart + full CAD regression suite

Run command (all): `docker exec -e PYTHONPATH=/app zhanlu-backend python -m pytest tests/test_dfm_rules.py tests/test_fusion360_dfm_check.py -q`
