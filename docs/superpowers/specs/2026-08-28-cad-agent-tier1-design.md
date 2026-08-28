# CAD Agent — Tier 1: Parametric, Contract-Driven, Constraint-Closed Models

**Date:** 2026-08-28
**Status:** Draft for review
**Scope:** Tier 1 only (per approved recommendation). Tier 2 (geometry validity checks) and Tier 3 (capability breadth) are out of scope.

## 1. Problem Statement

The CAD Agent currently "draws shapes" rather than "engineers models":

1. **Baked dimensions.** Every feature writes numbers into Fusion (`ValueInput.createByReal` via the `mm()` helper). The user's spec ("8mm across-flats, 10mm tall") exists only in chat history, not in the model. Updating a dimension = drawing a new feature on top (feature stacking), not editing a parameter.
2. **No design intent.** The model's geometry is not tied to a declarative spec. `verify_build` trusts the args passed at verify time — nothing stops the agent declaring a contract that matches the geometry while being the wrong part.
3. **Unclosed sketches.** The hexagon is drawn as 6 loose lines (`addByTwoPoints`). When endpoints don't form a topologically closed profile, `extrude` silently produces **no body** — observed live in testing (Extrude5 with zero bodies) while the tool returned success and `verify_build` still passed. This is the worst failure mode: silent wrong geometry.

## 2. Goals

- G1: Every generated dimension is a **named, editable parameter** in Fusion. "Make it 14mm" = change one parameter value.
- G2: The agent declares a **design-intent contract** per build; the backend stores it and `verify_build` validates geometry against the **stored** contract, not the model's word.
- G3: Sketches are **constraint-closed** before extrude; a non-closed profile is a loud, retryable error, never a silent empty body.
- G4: Non-regression: existing builds (hex prism bolt + nut scenario) keep working; the golden smoke suite is extended to cover all three new behaviors.

Non-goals: feature-tree editing of existing timeline items, tolerance/GD&T, patterns/bolt-circle recipes, screenshot visual loop, concurrency. (These are Tier 2/3.)

## 3. Current Behavior (evidence)

- `fusion360_granular._mm_expr(v)`: number → `mm(N)` (baked cm conversion); **string → `ValueInput.createByString('name')`** (parameter reference). The parameter plumbing already exists — it is simply never taught or used.
- `fusion360_extrude`: `sk.profiles.item(profile)` — no guard; if the sketch has no closed profile the extrude fails or creates nothing.
- `fusion360_sketch_polygon`: 6 × `sketchLines.addByTwoPoints(pts[i], pts[i+1])` — no coincident constraints.
- `fusion360_verify_build`: checks `expected_params` (name existence only), `expected_dimensions` (bbox/kind math), `expected_probes`; no parameter-value check, no stored contract.
- `fusion360_user_parameter` exists (create/read).

## 4. Design

Three components, one integrated workstream (they compose: parameters carry the spec, the contract records it, closed sketches make it build).

### Component A — Parameter-Driven Dimensions

**A1. Codegen change (fusion360_granular.py + fusion360_features.py):**
Every `distance_mm` / `diameter_mm` / `height_mm` / `radius_mm` / `thickness_mm` / `offset_mm` arg already flows through `_mm_expr`. No codegen change is required to *support* parameters — the string branch is live. The change is:
- Tool **schemas** document that dimension args accept either a number (mm) **or a parameter name** (string), and instruct the agent to prefer names for any dimension the user's spec names.
- New tool **`fusion360_params_list`** — lists document parameters (name, expression, value in mm) so the agent can read its own parameter table before updates.
- `verify_build` **`expected_params`** gains optional **value** checks: `{"name": "head_height", "value": 8}` — backend compares against the live parameter value (via the existing VPARAMS output, extended to print `name=value`).

**A2. Prompt change (system prompt, agent_apps row):**
Teach the workflow: on BUILD — (1) define parameters for every spec dimension (`fusion360_user_parameter`), (2) pass parameter NAMES to feature tools, (3) verify with `expected_params` values, (4) on UPDATE — change the parameter value (new tool `fusion360_parameter_set`, or extend user_parameter with an update path) and re-verify; never stack a new feature to change a dimension.

**A3. `fusion360_parameter_set`:** update an existing parameter's expression by name (wraps `userParameters.itemByName(name).expression = ...`). Distinct from create.

### Component B — Design-Intent Contract

**B1. New tool `fusion360_declare_spec`** (args: `{part: str, features: [{kind, dims...}], notes?: str}`):
Backend stores the contract for the conversation and returns a `contract_id`. Storage: new table **`cad_build_contracts`**:
- `id` (uuid pk), `org_id`, `app_id`, `conversation_id`, `agent_id`, `contract_json` (jsonb), `created_at`.
Contract schema (v1): `{part, features: [{kind, across_flats?, height?, diameter?, ...}]}` — feature kinds mirror `verify_build`'s `expected_dimensions` kinds (hex, box, cylinder, sphere, torus, cone, tube) plus named dims.

**B2. `verify_build` contract mode:**
New arg `contract_id` (or `"use_last"`). When set, the backend loads the stored contract, maps each feature to a live body (nearest bbox match), and checks every declared dimension against it. The verify result reports per-feature PASS/FAIL with the actual measured values. The model can no longer pass a hand-picked dimension list that contradicts what it declared.

**B3. Prompt change:** on every BUILD, declare the spec FIRST (before any tool call), then build, then verify with `use_last`. On UPDATE, re-declare (new contract for the updated state) before modifying.

### Component C — Constraint-Closed Sketches

**C1. Closure pass in sketch codegen (shared helper `_CLOSE_SKETCH`):**
Appended to every sketch-entity tool output (`sketch_line`, `sketch_polygon`, `sketch_rectangle`, `sketch_circle`, spline/arc):
- Snap-merge: for every sketch line/arc endpoint pair within 1e-6 cm, add a **coincident constraint** (`sketchGeometricConstraints.addCoincident`) so the loop is topologically closed.
- Print the **profile count**: `PROFILES <n>`.
- If `n == 0` after drawing: print `WARN_NO_PROFILE` — the tool returns `success: True` with the warning (the agent can react), but extrude is guarded (C2) so the failure is never silent.

**C2. Extrude guard:**
`fusion360_extrude` runs a pre-check snippet: if `sk.profiles.count == 0`, return `{success: False, error: "sketch has no closed profile; endpoints are not joined — re-run the sketch tool (auto-close) or fix the geometry", retryable: True}` — **before** attempting the extrude. A non-closed sketch becomes a loud retryable error.

**C3. Optional implied constraints:** rectangles get horizontal/vertical constraints on their four edges (cheap, prevents skew on later param edits). Polygons stay angle-free (regular by construction).

### Data flow (one BUILD turn)

```
user: "hex bolt, 8mm across-flats head, 5mm shank, 20mm long"
→ fusion360_declare_spec {part: hex_bolt, features:[{kind:hex, across_flats:8},
    {kind:cylinder, diameter:5, height:20}]}      → stored, contract_id returned
→ fusion360_user_parameter head_w=8mm, shank_d=5mm, shank_len=20mm
→ sketch_create → sketch_polygon(sides=6, circumradius=head_w/1.732)
    → closure pass → PROFILES 1
→ extrude(distance_mm=head_w_shank...) with parameter names → body created
→ verify_build(contract_id=use_last, expected_params:[{name:shank_len, value:20}])
    → PASS per-feature
UPDATE: "make the shank 25mm" → fusion360_parameter_set shank_len=25mm
→ verify_build(use_last) → PASS (geometry re-solved)
```

## 5. Error Handling

| Condition | Behavior |
|---|---|
| Contract declared, build doesn't match it | `verify_build` FAIL per-feature with measured vs declared values; agent must fix or re-declare |
| Sketch has no closed profile | Extrude guarded → loud retryable error; closure pass warning surfaced |
| Parameter name referenced but undefined | Fusion errors on createByString; tool returns retryable error with the param name |
| `parameter_set` on missing name | retryable error listing available params (via params_list) |
| No contract declared and `use_last` requested | retryable error telling the agent to declare first |

## 6. Testing (golden suite extension)

Extend the throwaway-document smoke test (scripts/cad_smoke.py — to be created as part of this work) with:
- T1: hex bolt built entirely via parameters → assert params exist with correct values (`params_list`), geometry matches contract (`verify_build use_last` PASS), profile closed (body count 1, extrude no warning).
- T2: update via `parameter_set` (shank 20→25mm) → re-verify PASS; assert NO new body created (feature stacking did not happen).
- T3: negative — draw an intentionally unclosed sketch (polygon with one side missing via raw snippet) → extrude must FAIL with the no-profile error, not create a body.
- T4: contract mismatch — declare spec with wrong across_flats → verify must FAIL with measured-vs-declared gap.
- T5: existing hex-prism scenario still passes (non-regression).
- Test guard: DB-agnostic rule — no hardcoded demo identifiers in new code.

## 7. Files Touched (expected)

- `backend/app/services/tool_handlers/fusion360_granular.py` (closure helper, extrude guard, schema docs, params_list? no — new file)
- `backend/app/services/tool_handlers/fusion360_parametric.py` (NEW: params_list, parameter_set, declare_spec)
- `backend/app/services/tool_handlers/fusion360_validation.py` (verify_build: value checks + contract mode)
- `backend/app/models/*` + migration (cad_build_contracts table)
- `backend/app/routers/agents.py` (ACTIVITY_STEP_DESCRIPTIONS for the 3 new tools)
- System prompt (agent_apps row for the CAD agent)
- `scripts/cad_smoke.py` (golden suite, throwaway doc)
- Skill: fusion360-mcp (pitfalls + workflow)

## 8. Open Questions

1. Parameter naming: agent-generated names ("head_w") could collide across builds in the same document. Since Tier-1 keeps the single-document model, prefix parameters per contract (`c<contract_id>_head_w`)? Recommend: yes, prefix with a short build id; params_list makes them readable.
2. Contract table lifecycle: keep rows forever (audit) vs purge with conversation cascade. Recommend: cascade with conversation (matches the user's hard-delete rule).
