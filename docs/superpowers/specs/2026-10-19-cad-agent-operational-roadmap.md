# CAD Agent — Operational Gap-Closing Roadmap (2026-10-19)

**Thesis:** the commercial opportunity is not "generate a CAD model from text" —
Fusion itself is moving there. It is the operational layer around CAD: quoting,
manufacturability, rework, engineering changes, and the CAD-to-production gap.
This roadmap maps Zhanlu's current CAD agent (audited 2026-10-19) to the eight
industry problems and sequences the gap-closing work.

**Architecture principle (already honored, keep enforcing):**
> LLM for interpretation and planning; deterministic tools for geometry,
> measurement, validation, and release.

---

## 0. Current-state inventory (audited 2026-10-19)

### What EXISTS and is strong
| Asset | Location | Notes |
|---|---|---|
| ~52 granular `fusion360_*` tools over a live FusionMCP bridge | `backend/app/services/tool_handlers/fusion360_{tool,granular,features,advanced,assembly,sketch2,parametric,io,probe,validation}.py` | Real Fusion 360 via TCP add-in (port 9876); validated API manifest, anti-hallucination layer |
| Named parametric model | `fusion360_user_parameter`, `fusion360_params_list`, `_mm_expr` (ValueInput by string) | Parameters creatable + expression-driven (`40 + stroke_pos * 110`) |
| Deterministic verification | `fusion360_verify_build` (`fusion360_granular.py:411-628`), `fusion360_probe` | Body counts, bbox dims, parameter checks (5% tol), bore/face/mass probes, **contract mode** vs stored `CadBuildContract` |
| Acceptance-criteria contract | `backend/app/models/cad_build_contract.py`, `fusion360_declare_spec` | Auditable spec per conversation — the article's "verification layer" |
| Sketch/feature guards | sketch closure (`test_fusion360_sketch_closure.py`), extrude guard, destructive-clear guard, spin guards | Reliability rails already in the v3 loop |
| Exports | `fusion360_io.py` | STEP/STL/OBJ/IGES/SAT/F3D; DXF import; backend-rendered DXF drawing (ezdxf) |
| Traceability | `AgentRunStep`, activity feed, `CadBuildContract`, `UserFile` export provenance | Platform-level; CAD rides on it |
| CAD-aware loop plumbing | `agents.py` `_is_fusion_agent` (13926), budget lift, verify nudge, `_cad_build_fallback` (637-758) | Serial tool execution in `tool_executor.py:70-92` |

### What is MISSING (gap list)
| Gap | Detail |
|---|---|
| `fusion360_parameter_set` | The parameter **update** tool — designed (Tier-1 spec §A3) but never implemented. Blocks parametric edit + rebuild + all change propagation |
| DFM rule engine | Zero DFM rules. `verify_build` answers "did we build the spec", not "can this be manufactured" |
| RFQ intake & quoting | No requirement extraction, no material/process database, no cost model, no quote package |
| BOM generation | No BOM from model (components exist via `fusion360_assembly.py` but no BOM emitter) |
| Revision store | No CAD artifact versioning/revision tracking (0 matches in fusion360_*.py); exports are flat `UserFile` rows |
| CAD approval gate | Platform HITL exists (`approval_tool.py`) but no CAD sign-off step before release |
| CAD skill | Planned `fusion360-mcp` SKILL.md (Tier-1 spec §7) never written |
| Frontend viewer | `three` installed but never imported — no 3D preview of built models |
| Flat pattern / PDF drawing | No sheet-metal flat-pattern export; drawing is DXF-only, no PDF |
| Templates & CTO | No part-family template library; no constraint-checked variant configuration |
| Legacy modernization | No geometry-analysis → parameter-inference → documented-intent flow |

---

## 1. Gap map — 8 industry problems → phases

| # | Industry problem | Current | Phase | Deliverable |
|---|---|---|---|---|
| 3 | Repetitive part design | ⚠️ 50% | **P0** | Parameter update tool + contract re-declare = parametric edit works |
| 2 | Poor manufacturability | ❌ 0% | **P1** | Deterministic DFM rule engine (CNC + sheet metal first) |
| 1 | Slow custom quoting | ❌ 0% | **P2** | RFQ intake → model → DFM → material/process estimate → quote package |
| 4 | Change propagation (ECO) | ⚠️ 30% | **P3** | Rebuild-on-change, impact report, revision store, approval gate |
| 6 | CAD-to-production gap | ❌ 0% | **P4** | BOM emitter, manufacturing notes, procurement/ERP record link |
| 7 | Legacy CAD modernization | ❌ 0% | **P5** | Geometry analysis → parameter inference → documented intent |
| 8 | Configuration complexity | ❌ 0% | **P6** | Template library + constraint-checked variant configuration |
| 5 | Skills shortage | ⚠️ Partial | **cross-cutting** | CAD skill docs, company standards enforcement, escalation flow |

Phase order is dependency-driven: P0 unblocks P3 and P2's rebuild step;
P1's DFM engine is consumed by P2's quote package; P3's revision store is
consumed by P4's BOM/change docs. P2 is the commercial MVP wedge — it ships
revenue value first.

---

## 2. Phase plans

### Phase 0 — Foundation quick wins (est. 2-4 sessions)
Unblocks everything else; low risk, high leverage.

1. **`fusion360_parameter_set`** — implement the designed-but-missing update
   tool: set parameter by name → Fusion re-solve → return new mass/bbox.
   Reuse `_mm_expr` for expressions; route through contract mode (parameter
   must exist in the active `CadBuildContract`, else reject).
   *Test: set thickness 4→5mm on a contract-built plate, verify mass changes.*
2. **`SKILL.md: fusion360-mcp`** — write the planned skill (Tier-1 spec §7):
   pitfalls (sketch closure, positional indices, shared document), the
   declare→build→verify→probe loop, when to use granular tools vs
   `execute_python` (never for geometry the granular layer covers).
3. **PDF drawing export** — extend `fusion360_make_drawing` (ezdxf already
   renders) with a PDF/SVG emitter so quote packages can embed drawings.
4. **Frontend `three` viewer (optional but cheap)** — STEP/STL preview for
   exported `UserFile`s; the library is already in `package.json` (unused).

**Exit criteria:** parameters can be changed and re-verified by name; a CAD
turn follows the skill; a drawing renders as PDF.

### Phase 1 — DFM rule engine (est. 1-2 weeks)
The strongest technical differentiator; deterministic, no LLM geometry.

- **New module** `backend/app/services/dfm/` — pure-Python rules over
  measurement data the agent already collects via probes/physical properties:
  - **CNC mode:** internal corner radius vs tool diameter, pocket depth:tool
    ratio, hole-to-edge distance, min wall thickness, setups estimate
    (feature orientation analysis), 5-axis trigger.
  - **Sheet-metal mode:** bend radius vs thickness (k-factor), min flange
    length, hole-to-bend distance, bend relief, flat-pattern validity,
    thickness in stock range.
  - **3DP / molding modes:** later — same rule interface.
- **Rule output contract** (the article's issue→reason→repair format):
  ```json
  {
    "rule": "cnc_internal_corner_radius",
    "severity": "error|warning|info",
    "measured": "1.0mm", "required": "3.0mm",
    "reason": "Selected 6mm endmill cannot clear a 1mm internal corner",
    "repair": {"option": "increase_radius", "value_mm": 3.0,
               "effect": "avoids 5-axis op; est. -35% machining time"},
    "auto_fixable": true
  }
  ```
- **Integration:** new `fusion360_dfm_check` tool (runs after `verify_build`,
  nudge the loop the same way the verify nudge works); results attached to the
  `CadBuildContract`. Auto-fixable rules can emit a parameter/feature change
  proposal for the agent to apply (human confirms for non-contract edits).
- **What it needs that doesn't exist yet:** feature-tree introspection
  (radii, hole specs, wall thickness). Extend `fusion360_probe` — it already
  measures bore diameter/depth and face areas; add fillet-radius and
  shell-thickness probes. Sheet-metal mode additionally needs the flat-pattern
  API (Fusion `flatPattern`), deferred to P2's export step.

**Exit criteria:** a 3-axis-unmanufacturable bracket gets specific,
reasoned warnings with repairs; a clean part passes with zero warnings.

### Phase 2 — RFQ → Design → DFM → Quote (the MVP wedge, est. 2-4 weeks)
The commercially valuable loop. Everything downstream of intake reuses P0-P1.

1. **Requirement extraction** — a structured `RfqRequirement` contract
   (mirror of `CadBuildContract`): dimensions, material hints, IP/environment,
   mounting, process preference, quantity, target price. Extracted from
   natural language or an uploaded drawing (image → vision model → fields,
   with clarifying questions for the ambiguous ones — the goal-contract
   clarify machinery already exists).
2. **Material & process database** — a small seeded table
   (`material`: name, density, stock thicknesses, machinability, cost/kg;
   `process`: hourly rate, setup cost, min batch, lead time). Start with
   6061/5052 aluminum, mild/stainless steel × {3-axis CNC, sheet metal}.
3. **Design generation** — existing parametric pipeline, seeded from
   `RfqRequirement` instead of ad-hoc chat.
4. **DFM review** — Phase 1 engine, process-aware.
5. **Estimate & quote package** — mass (probes) × material cost +
   process time estimate (feature counts × rates) + setup/NRE =
   quote range. **Artifact type `quote_package`** (HTML/PDF): drawing,
   DFM report, BOM draft, cost breakdown, assumptions & exclusions.
6. **Approval gate** — reuse platform HITL: quote released only after human
   approval.

**Exit criteria (the demo):** *"Create a wall-mounted aluminum enclosure for
a controller, 240mm wide, IP54, two cable glands, four M6 mounting holes,
sheet-metal bending"* → parametric model + bend/clearance checks + thickness
recommendation + DXF/STEP + preliminary BOM + DFM warnings + quote summary,
one turn, human-approved.

### Phase 3 — Engineering-change intelligence (est. 1-2 weeks)
Builds directly on P0 (`parameter_set`) and P1 (rules re-run post-change).

1. **Change application:** `fusion360_parameter_set` + `verify_build` +
   `dfm_check` re-run = rebuild with re-validation.
2. **Impact report:** before/after diff of mass, bbox, probe values, DFM
   status, contract deltas ("thickness 4→5mm: mass +18.2%, M6 hole
   edge-distance now 6.5mm vs 8mm min — PASS→WARNING").
3. **Assembly interference:** `fusion360_assembly.py` has joints +
   physical properties; add an interference/clearance check pass.
4. **Revision store:** new `cad_revisions` table — per release: contract
   snapshot, probe values, DFM report, exports, approval record, parent
   revision. Ties into existing `AgentRunStep` trace.
5. **Approval gate:** revision release requires HITL approval (article's
   step 8: "ask for approval before releasing the new revision").

**Exit criteria:** *"Change the bracket thickness from 4mm to 5mm and tell me
everything affected"* → parameter change → rebuild → interference/mass/DFM
diff → change summary → approval prompt → revision stored.

### Phase 4 — CAD-to-production (est. 2-3 weeks)
1. **BOM emitter** — `fusion360_assembly.py` components + physical properties
   → structured BOM (part no., material, mass, process, source).
2. **Manufacturing notes** — deterministic render of DFM results + process
   plan (setups, tools, ops) into the drawing/quote package.
3. **Procurement link** — connect the quote package to the existing
   `quote_invoice_tools.py` PO pipeline (the platform already has
   approval-gated PO drafts — wire `create_quote_draft` as a downstream
   consumer of the CAD quote package).
4. **ERP connector interface** — stub + adapter pattern; defer real ERP
   integration to customer deployments.

### Phase 5 — Legacy modernization & CTO (est. 2-4 weeks, optional/exploratory)
1. **Legacy CAD:** import STEP → `fusion360_probe` + measurement sweep →
   infer parameter candidates (bbox, hole pattern detection) → reconstruct
   a parametric skeleton + documented design intent. Research-grade; start
   with prismatic parts only.
2. **Template library:** capture proven builds as named parametric templates
   (contract + parameter schema + feature recipe); CTO = template + option
   constraints → validated variant.
3. **3DP/molding DFM modes:** extend the P1 rule interface.

---

## 3. Differentiation layers — coverage after roadmap

| Layer (article) | Today | After roadmap |
|---|---|---|
| 1. Business context (requirements, suppliers, cost targets, standards) | ❌ | P2 (requirements/quote) + P4 (procurement) + P3 (approvals) |
| 2. Deterministic engineering execution | ✅ | ✅ (keep — never let the LLM freehand geometry) |
| 3. Verification vs acceptance criteria | ✅ | ✅ + P1 DFM rules, P3 re-verification |
| 4. Production documentation | ⚠️ partial | P0 PDF + P2 quote package + P4 BOM/notes |
| 5. Traceability & approval | ⚠️ platform-only | P3 revision store + CAD approval gates |

## 4. Recommended sequencing

**P0 → P1 → P2.** That sequence alone delivers the article's MVP wedge
("turns custom manufacturing requests into verified, quote-ready designs")
and the demo headline. P3 follows because it is the second product and 30%
pre-built. P4-P6 are customer-driven.

## 5. Risks / open questions

- **Fusion dependency:** everything requires the desktop Fusion bridge online
  (host.docker.internal:9876). Quoting (P2) should degrade gracefully to
  estimate-only when no bridge is connected.
- **Mass/cost accuracy:** probe-based mass is exact for the model, but process
  time estimates in P2 are heuristic — present ranges, never point quotes.
- **Sheet metal:** flat-pattern APIs and k-factor handling are the riskiest
  P1/P2 item — consider shipping CNC mode first if Fusion's flat pattern API
  fights back.
- **Domain rules:** DFM thresholds need real values (tool tables, stock
  sizes). Seed from public machining references; make them per-tenant
  configurable from day one (that is also the "company standards" layer).
