# Comparative Analysis: PPTX / DOCX Generation — Claude, MiniMax, Kimi, and Zhanlu

**Date:** 2026-07-21
**Scope:** PPTX and DOCX generation capability across four agent platforms, evaluated on formatting accuracy, layout preservation, template handling, and content structuring, followed by a prioritized improvement roadmap for Zhanlu.
**Method:** Capability ratings follow the `competitive-analysis` skill's Strong / Adequate / Weak / Absent scale. Every Zhanlu claim is grounded in source files read this session (paths cited inline). Competitor findings are carried over from the prior first-party-source research pass (Anthropic Skills API beta; MiniMax open-source `skills` repo, locally cloned at `backend/skills/minimax-*`; Kimi K3 platform docs).

---

## 1. Executive summary

Zhanlu's **skill bodies** — `backend/skills/pptx/SKILL.md` (34.9 KB) and `backend/skills/docx/SKILL.md` (29.7 KB) — already exceed the native skill guidance shipped by Claude, MiniMax, and Kimi. They contain a 12-point pre-emit self-audit (PPTX) and 10-point self-audit (DOCX), a WCAG contrast rubric, a 6×6 content-density rule with word-count script, a 5-type document rubric, accessibility quick pass, and 8-entry anti-pattern galleries. None of the three competitors' skills ship anything this opinionated.

**The gap is not in the skill bodies. It is in the runtime.** Zhanlu has two generation pipelines, and neither enforces the skill bodies' discipline:

1. **Marker pipeline** (`app/services/artifact_markers.py` → `app/routers/agents.py`): the LLM writes a file to `outputs/` and emits a `◤PPTX◤`/`◤MD_DOCX◤`/`◤HTML_DOCX◤` marker. The backend parses the marker and calls `_create_artifact_tool()`. `find_markers()` **silently skips** malformed JSON, unknown kinds, and mismatched tags — and never validates the produced file.
2. **Exporter pipeline** (`app/services/artifacts/exporters/service.py` → `pptx_export.py` / `docx_export.py`): `ExportService` reconstructs a `ReportCardPayload` and renders it. The PPTX renderer emits a **fixed 6-slide sequence** (title → summary → KPIs → chart → insights → data) with a **hardcoded single color palette**. The DOCX renderer emits a **fixed 5-section doc** (title → overview → KPI table → chart table → insight bullets) with hardcoded Calibri 11pt and one table style — **no headers/footers, page numbers, TOC, tracked changes, comments, or images**.

The result: Zhanlu's *advisory* design guidance is best-in-class, but its *enforced* output quality is below Claude's because nothing in the runtime blocks a broken deck/doc from being persisted and downloaded. Claude has the same advisory-only gap; MiniMax has it worse; Kimi has no native capability at all. **Closing the runtime-enforcement gap is the single highest-leverage investment**, and it would move Zhanlu from "best skill body, average output" to "best output, full stop."

**Bottom line by platform:**

| Platform | Skill guidance | Runtime enforcement | Native file output | Verdict |
|---|---|---|---|---|
| **Claude** | Adequate (opinionated, design-aware) | Weak (advisory only) | Strong (XSD-clean, native charts) | Benchmark to beat on output |
| **MiniMax** | Adequate (open, OOXML-aggressive) | Weak (advisory only) | Adequate (same libs, thinner defaults) | Credible open alternative |
| **Kimi** | Absent | Absent | Absent (no native pipeline) | Not a contender for file gen |
| **Zhanlu** | **Strong** (best-in-class) | **Absent** (no enforcement) | Adequate (rigid fixed-sequence renderers) | Best guidance, biggest enforcement gap |

---

## 2. Capability matrix

Rating scale (per `competitive-analysis` skill): **Strong** = market-leading; **Adequate** = functional, not differentiated; **Weak** = exists but limited; **Absent** = does not have it.

### 2.1 PPTX

| Capability area | Claude | MiniMax | Kimi | Zhanlu |
|---|---|---|---|---|
| **Basic slides** (title, body, bullets) | Strong | Strong | Absent | Adequate |
| **Native Office charts** (editable, not raster) | Strong | Adequate | Absent | Strong (`pptx_export._add_chart_slide`, `XL_CHART_TYPE`) |
| **Embedded images** | Strong | Adequate | Absent | Weak (skill teaches; exporter has no image slide) |
| **Multi-layout decks** (>2 distinct layouts) | Strong | Strong | Absent | Weak (one fixed 6-slide sequence) |
| **Template / theme system** | Adequate (theme via `python-pptx`) | Adequate | Absent | Absent (hardcoded `C_PRIMARY` palette, no theme input) |
| **Speaker notes** | Strong | Adequate | Absent | Absent (exporter never sets `slide.notes_slide`) |
| **Precise EMU layout control** | Strong | Strong | Absent | Strong (`Inches()`/`Pt()` throughout `pptx_export`) |
| **Density / overflow guard** | Weak (advisory) | Weak (advisory) | Absent | Weak (skill has 6×6 rule + script; **runtime does not run it**) |
| **Contrast / color-blind safety** | Weak (advisory) | Weak (advisory) | Absent | Weak (skill has WCAG rubric; **runtime does not compute contrast**) |
| **Off-canvas / overlap detection** | Absent | Absent | Absent | Absent |
| **Structural integrity** (opens without repair) | Strong (XSD-clean by construction) | Adequate | Absent | Adequate (python-pptx is XSD-clean; no re-validation) |
| **Round-trip edit** (read + modify existing file) | Strong | Strong | Absent | Weak (exporter is create-only; no edit path) |
| **Animations / transitions** | Absent | Absent | Absent | Absent |
| **Skill-body design guidance** | Adequate | Adequate | Absent | **Strong** (12-point audit, density rule, contrast rubric, anti-pattern gallery) |

### 2.2 DOCX

| Capability area | Claude | MiniMax | Kimi | Zhanlu |
|---|---|---|---|---|
| **Headings, paragraphs, styles** | Strong | Strong | Absent | Adequate (`add_heading`, Normal style) |
| **Tables** | Strong | Strong | Absent | Adequate (`add_table`, but fixed "Light Grid Accent 1" style) |
| **Headers / footers** | Strong | Strong | Absent | Absent (exporter never touches `section.header`/`footer`) |
| **Page numbers** | Strong | Strong | Absent | Absent |
| **Table of contents** | Adequate | Adequate | Absent | Absent (skill teaches; exporter omits) |
| **Tracked changes** | Weak (fragile OOXML) | Adequate (explicit `lxml` path) | Absent | Absent |
| **Comments** | Weak | Adequate (explicit OOXML) | Absent | Absent |
| **Images** | Strong | Adequate | Absent | Absent (exporter has no `add_picture` path) |
| **Document-type awareness** (memo vs report vs proposal) | Adequate | Adequate | Absent | Weak (skill has 5-type rubric; **exporter ignores type, emits one shape**) |
| **Structural integrity** | Strong | Adequate | Absent | Adequate |
| **Round-trip edit** | Strong | Strong | Absent | Weak (create-only) |
| **Markdown → DOCX pipeline** | Adequate | Adequate | Absent | Adequate (pandoc fallback in `docx_export._render_via_pandoc`) |
| **HTML → DOCX pipeline** | Adequate | Adequate | Absent | Adequate (`render_html_to_docx`, HTML-canonical path in `ExportService`) |
| **Skill-body design guidance** | Adequate | Adequate | Absent | **Strong** (10-point audit, 5-type rubric, typography hierarchy, anti-pattern gallery) |

---

## 3. Four-axis deep dive

### 3.1 Formatting accuracy

**What it means:** fonts, sizes, weights, colors, table styles, list formatting, and spacing render exactly as intended and survive open/edit/reopen in Microsoft Office.

**Competitor bar:** Claude sets the bar — `python-pptx`/`python-docx` produce XSD-clean OOXML by construction, so files never trigger a repair prompt. MiniMax matches on the library floor. Both hardcode a reasonable default palette. Kimi is absent.

**Zhanlu current state:**
- PPTX: hardcoded palette in `pptx_export.py` lines 40–53 (`C_PRIMARY`, `C_TEXT`, `C_MUTED`, …). Font sizes fixed per slide type (44pt title, 28pt section header, 18pt body). No theme input — every deck looks identical regardless of topic.
- DOCX: hardcoded `Calibri` 11pt Normal style (`docx_export.py` line 63–64). Tables always use `"Light Grid Accent 1"` (lines 85, 117, 127, 132). No font-family / size / color theming.
- Both renderers are XSD-clean (python-pptx/python-docx guarantee this), so no repair-prompt risk — same floor as Claude.

**Gap:** Zhanlu matches the competitor *floor* (no corruption) but has zero *theming flexibility*. Claude and MiniMax let the agent choose palettes/fonts per document; Zhanlu's exporter ignores any theme the skill/agent selects. The skill body (`pptx/SKILL.md` "Color Palettes", "Pre-validated combinations") teaches rich theming, but the exporter cannot receive a theme parameter.

### 3.2 Layout preservation

**What it means:** elements stay on-canvas, do not overlap, do not overflow, and round-trip edits preserve formatting on untouched elements.

**Competitor bar:** Claude is Adequate — precise EMU placement but no runtime overlap/overflow check (same blind-generation gap as everyone). MiniMax is the same. Kimi is absent.

**Zhanlu current state:**
- PPTX: precise EMU placement (`Inches()` everywhere in `pptx_export._add_*`). KPI grid auto-sizes columns/rows based on count (lines 167–174) — good. Data table truncates at 24 rows with a footnote (lines 328, 360–366) — good defensive design. **But:** no overlap detection, no off-canvas detection. The fixed slide sequence is safe-by-construction because positions are hardcoded, but the moment a richer renderer lets the agent place boxes freely, the current runtime will not catch mistakes.
- DOCX: flow-layout (python-docx), so overflow is the renderer's problem, not Zhanlu's. Table column widths are not set explicitly — Word auto-sizes, which can produce ugly tables on long headers.
- Round-trip: **both exporters are create-only.** Neither opens an existing `.pptx`/`.docx`, modifies it, and saves. `ExportService._payload_from_artifact()` synthesizes a payload from metadata, then renders fresh.

**Gap:** Zhanlu is at parity with Claude on the *floor* (no overlap because positions are fixed) but below parity on *round-trip editing* — a capability Claude and MiniMax both have. The skill body's "Editing Workflow" and "Reading Content" sections (`pptx/SKILL.md` lines 56–80; `docx/SKILL.md` "Reading Content", "Accepting Tracked Changes") describe round-trip editing, but no exporter implements it.

### 3.3 Template handling

**What it means:** the platform can apply a template / theme / layout system to produce decks and docs that look bespoke rather than stamp-identical.

**Competitor bar:** Claude and MiniMax are Adequate — `python-pptx` supports loading a `.potx` template or defining slide layouts; both skills mention it. Neither ships a first-party template gallery. Kimi is absent.

**Zhanlu current state:** **Absent.**
- `pptx_export.render()` starts from `Presentation()` (blank) and uses only `slide_layouts[6]` (blank layout) for every slide (line 66). It never loads a `.potx`, never defines reusable layouts, and never accepts a theme parameter. Every deck is the same 6 slides in the same colors.
- `docx_export.render()` starts from `Document()` (default template) and never loads a `.dotx` or defines a custom style set.
- The `backend/skills/` tree contains `ppt-template-creator/`, `presentation-generator/`, `frontend-slides/`, and `pptx-author/` — four additional skills that *suggest* template ambitions exist, but none are wired into the exporter pipeline, and three are stubs or partial (`pptx-author/SKILL.md` is 1.78 KB).

**Gap:** This is Zhanlu's largest *capability* gap versus Claude/MiniMax. A template/theme system (load `.potx`/`.dotx`, or accept a theme JSON parameter on `render()`) is the difference between "every deck looks the same" and "every deck looks bespoke."

### 3.4 Content structuring

**What it means:** the platform produces documents with correct structure for their type — a memo is not shaped like a report, a proposal has the right sections, a deck has a summary and next-steps slide.

**Competitor bar:** Claude is Adequate — the skill body gives structural guidance (slide-type conventions, document types), and the agent follows it. MiniMax is similar. Neither enforces structure at runtime. Kimi is absent.

**Zhanlu current state:**
- **Skill body is Strong.** `docx/SKILL.md` ships a 5-type rubric (memo / report / proposal / meeting minutes / …) with a full structural template for each (lines 59–230), plus type-mismatch warnings (line 230). `pptx/SKILL.md` ships "Slide-type Conventions" (line 681) and a "Professional Output Principles" closing section (line 724).
- **Exporter is Weak.** `pptx_export.render()` emits exactly one structure regardless of input: title → summary → KPIs → chart → insights → data (lines 68–78). `docx_export.render()` emits exactly one structure: title → overview → KPI table → chart table → insight bullets (lines 66–148). The `ReportCardPayload` data shape forces this — it has no `document_type` field, so the exporter cannot branch.

**Gap:** The skill body teaches 5 document types; the exporter implements 1. The payload contract (`ReportCardPayload`) is the bottleneck — adding a `document_type`/`deck_type` field and branching the renderer would close this without touching the skill body.

---

## 4. Where Zhanlu already leads

These capabilities are **already best-in-class** and should NOT be re-solved in the roadmap. Citing verified `SKILL.md` sections:

| Capability | Zhanlu section | Claude / MiniMax equivalent |
|---|---|---|
| **Pre-emit self-audit (12 pts, PPTX)** | `pptx/SKILL.md` §"Pre-Emit Self-Audit (12 points)" (line 505) — Content/Visual/Polish | None — Anthropic skill has no point-counted audit |
| **Pre-emit self-audit (10 pts, DOCX)** | `docx/SKILL.md` §"Pre-Emit Self-Audit (10 points)" (line 333) — Content/Layout/Quality | None |
| **WCAG contrast scoring** | `pptx/SKILL.md` §"Color & Contrast" (line 243) — 60-30-10 rule, WCAG scoring, pre-validated combos, "never ship" combos | None — Claude skill mentions contrast but provides no scoring rubric |
| **Content density rule** | `pptx/SKILL.md` §"Content Density" (line 178) — 6×6 rule, white-space rules, anti-patterns, word-count script | None |
| **5-type document rubric** | `docx/SKILL.md` §"Choosing a Document Type" (line 55) — memo/report/proposal/minutes templates + type-mismatch warnings | None — Claude skill has generic guidance |
| **Anti-pattern galleries** | `pptx/SKILL.md` §"Anti-Patterns Gallery" (line 599, 8 entries); `docx/SKILL.md` §"Anti-Patterns Gallery" (line 359, 8 entries) | Thinner or absent |
| **Accessibility quick pass** | `pptx/SKILL.md` §"Accessibility Quick Pass" (line 410) — 5-min pass, color-blind-safe palette tags, alt-text patterns, programmatic alt-text | None |
| **Typography hierarchy** | `pptx/SKILL.md` §"Typography Hierarchy" (line 333) — 4-level system, paragraph rhythm; `docx/SKILL.md` §"Typography & Page Layout" (line 244) | Generic |
| **Pipeline chooser** | `docx/SKILL.md` §"Creating New Documents" (line 413) — HTML→DOCX (styled) vs MD→DOCX (simple) decision tree | Implicit |

**Implication for the roadmap:** every dollar spent *re-writing* these sections is wasted. The investment thesis is to make the runtime *enforce* what these sections already teach.

---

## 5. Gap analysis mapped to verified code

Every gap below cites a file read this session. Gaps are split into **skill-body gaps** (cheap, edit a `.md`) and **runtime/tooling gaps** (structural, edit Python).

### 5.1 Runtime / tooling gaps (the real work)

| # | Gap | Verified location | Severity |
|---|---|---|---|
| R1 | **No runtime enforcement of the self-audit.** The 12/10-point audits are advisory text; nothing in `ExportService` or `_create_artifact_tool` runs them. | `exporters/service.py` `_render_and_store()` (line 212) renders and stores with no validation step; `artifact_service.py` `create_artifact()` (line 38) validates only `artifact_type` | Critical |
| R2 | **No render-to-image verification.** Skill says "convert to images and inspect" (`pptx/SKILL.md` line 653); no `soffice`/LibreOffice conversion runs at export time. | `exporters/pptx_export.py` `render()` (line 59) returns bytes with no image conversion; `ExportService` never calls a preview renderer | High |
| R3 | **No contrast/overlap computation.** The WCAG rubric and off-canvas checks exist only as instructions to the LLM. | No `contrast`/`overlap`/`off-canvas` function exists in `exporters/` | High |
| R4 | **No post-render XSD / structural re-validation.** Files are trusted as python-pptx/python-docx produced them. | `service.py` line 247 `data, mime, ext = render(...)` → immediately stored, no validate step | Medium |
| R5 | **PPTX exporter is a fixed 6-slide sequence with hardcoded palette.** No template, no theme parameter, no multi-layout. | `pptx_export.py` lines 40–53 (palette), 66 (blank layout only), 68–78 (fixed slide calls) | Critical |
| R6 | **DOCX exporter omits headers/footers, page numbers, TOC, tracked changes, comments, images.** | `docx_export.py` lines 51–152 — none of these APIs are called | High |
| R7 | **Exporter is create-only — no round-trip edit.** Skill teaches round-trip; no exporter opens an existing file. | `service.py` `_payload_from_artifact()` (line 399) always synthesizes fresh; no "open existing .docx, modify, save" path | Medium |
| R8 | **`ReportCardPayload` has no `document_type`/`deck_type` field.** Exporter cannot branch by document type despite the skill's 5-type rubric. | `synexia/contracts.py` `ReportCardPayload` (referenced throughout `service.py` and exporters) | High |
| R9 | **Marker pipeline silently skips bad markers with no feedback to the model.** | `artifact_markers.py` `find_markers()` lines 60–92 — `logger.debug` only, returns `[]` on malformed | Medium |
| R10 | **`mark_version_built()` accepts a `validation_report` but nothing computes it.** | `artifact_service.py` line 258 — `validation_report` is caller-supplied, never auto-populated | Medium |
| R11 | **Eager-render is best-effort and swallows all errors.** A broken render silently produces no file; user sees a download that 404s. | `service.py` `eager_render_default()` lines 160–164 — `except Exception` returns `None` | Low |

### 5.2 Skill-body gaps (cheap fixes)

| # | Gap | Verified location | Severity |
|---|---|---|---|
| S1 | **No "emit marker" contract example showing the exact JSON keys the backend expects.** Marker payload keys (`md_path`, `html_path`, `slides_path`, `filename`) are defined in `artifact_markers.py` but the skill does not show a complete worked marker. | `pptx/SKILL.md` line 703 "Emitting the PPTX marker" is thin; `docx/SKILL.md` has no marker section | Medium |
| S2 | **No guidance mapping the 5 document types to a `document_type` payload field** (because R8 means the field doesn't exist yet). | `docx/SKILL.md` §"Choosing a Document Type" (line 55) | Low (blocked by R8) |
| S3 | **5 overlapping PPTX skills + 2 overlapping DOCX skills create routing ambiguity.** `pptx/`, `pptx-author/` (1.78 KB stub), `presentation-generator/` (6.23 KB), `frontend-slides/` (6.41 KB), `ppt-template-creator/` (8.75 KB); `docx/` vs `md-to-docx/` (2.71 KB). Plus a legacy `skills2/` tree with pre-overhaul versions. | `backend/skills/` directory listing | Medium |

---

## 6. Prioritized improvement roadmap

Prioritization uses the `product-management-workflows` framing: P0 = blocks the core promise ("professional-grade file"), P1 = differentiates, P2 = polish/consolidation. Effort estimates are rough (S = ≤1 day, M = 1–3 days, L = 1+ week).

### P0 — Runtime enforcement (closes the "advisory-only" gap shared with Claude)

| ID | Task | Files | Effort | Success criteria |
|---|---|---|---|---|
| **P0.1** | **Programmatic self-audit runner.** Implement `exporters/qa/audit.py` that takes rendered `.pptx`/`.docx` bytes and runs the skill's 12/10-point checks programmatically: word-count-per-slide (density), off-canvas shape detection, textbox overlap detection, placeholder-text scan. Returns a `ValidationReport`. Wire into `ExportService._render_and_store()` before `store_blob()`. | new `exporters/qa/audit.py`; `service.py` `_render_and_store()` (line 212) | M | A deck with an overlapping textbox is rejected (or auto-fixed) before storage; `validation_report` on the version is auto-populated (fixes R1, R3, R10) |
| **P0.2** | **Contrast computation.** Implement `exporters/qa/contrast.py` computing WCAG ratio for every text/background pair in the rendered file; fail the audit if any body text is below AA. | new `exporters/qa/contrast.py`; called by P0.1 | S | A deck with `C_TEXT`-on-`C_BG` passes; a deck with low-contrast body fails with the specific slide + ratio |
| **P0.3** | **Render-to-image verification hook.** After render, convert to PNG via `soffice --headless --convert-to png` (LibreOffice is already a project dependency for previews), run overlap/overflow detection on the image, attach the preview image as a `blob_type="preview"` blob. | `service.py` `_render_and_store()`; new `exporters/qa/render_image.py` | M | A preview PNG is attached to every exported artifact; off-canvas elements are flagged (fixes R2) |

**Why P0:** This is the one gap where Zhanlu can leapfrog Claude. Claude's self-audit is advisory; Zhanlu's would be enforced. This is the differentiator that makes "best skill body" actually translate into "best output."

### P1 — Capability parity with Claude/MiniMax

| ID | Task | Files | Effort | Success criteria |
|---|---|---|---|---|
| **P1.1** | **Theme/template parameter on renderers.** Add an optional `theme: ThemeSpec` (palette + fonts + table style) parameter to `pptx_export.render()` and `docx_export.render()`. Default to current palette (backward-compatible). Thread through `ExportService` and `ReportCardPayload` (or `ExportContext`). | `pptx_export.py` lines 40–53, 59; `docx_export.py` lines 51–152; `service.py` `_render_and_store()` | M | Requesting a "dark" vs "corporate" theme produces visibly different decks without code changes (fixes R5, part of §3.1) |
| **P1.2** | **`document_type` / `deck_type` on payload + renderer branching.** Add field to `ReportCardPayload`; branch `pptx_export` (e.g., report deck vs pitch deck vs data deck) and `docx_export` (memo vs report vs proposal vs minutes — reusing the skill's 5-type templates). | `synexia/contracts.py`; `pptx_export.render()` line 59; `docx_export._render_via_python_docx()` line 51 | L | A "memo" payload produces a memo-shaped doc; a "pitch deck" payload produces a different slide set than a "report deck" (fixes R8, §3.4) |
| **P1.3** | **DOCX headers/footers/page numbers/TOC/images.** Add `section.header`, `section.footer`, page-number field, TOC field, and an `add_picture` path to `docx_export`. Gate behind `document_type` (reports/proposals get TOC + page numbers; memos don't). | `docx_export.py` `_render_via_python_docx()` | M | A report DOCX has a header, footer with page numbers, and a TOC; an image payload field renders inline (fixes R6) |
| **P1.4** | **XSD / structural re-validation post-render.** Validate the rendered OOXML against the bundled XSDs (already present: `pptx/scripts/*.xsd`, `docx/scripts/*.xsd`). On failure, fail the version rather than storing a corrupt file. | `service.py` after `render()` call; reuse existing XSD files | S | A deliberately-corrupt render is rejected (fixes R4) |

### P2 — Polish, consolidation, and round-trip

| ID | Task | Files | Effort | Success criteria |
|---|---|---|---|---|
| **P2.1** | **Skill consolidation.** Merge `pptx-author/`, `presentation-generator/`, `frontend-slides/`, `ppt-template-creator/` into `pptx/` (or delete the stubs). Merge `md-to-docx/` into `docx/`. Delete the legacy `skills2/` tree. | `backend/skills/` | S | One canonical PPTX skill, one canonical DOCX skill; no routing ambiguity (fixes S3) |
| **P2.2** | **Round-trip edit path.** Add an `edit_artifact` exporter mode that opens the existing `.pptx`/`.docx` blob, applies a diff (text replacements, restyles), and saves — preserving untouched formatting. | new `exporters/edit.py`; `service.py` new method | L | Editing one slide's title does not disturb other slides' formatting (fixes R7, §3.2) |
| **P2.3** | **Marker contract documentation in skills.** Add a complete worked marker example (`◤PPTX◤{"slides_path": "outputs/deck.json", "filename": "Q3-Review.pptx"}◤END_PPTX◤`) to both skill bodies, listing the exact payload keys the backend expects. | `pptx/SKILL.md` line 703; `docx/SKILL.md` (add section) | S | A model reading the skill emits a marker the backend accepts on the first try (fixes S1) |
| **P2.4** | **Marker failure feedback.** When `find_markers()` skips a malformed marker, surface a structured warning back to the model (via the trace/activity-steps channel) so it can retry. | `artifact_markers.py`; `agents.py` lines 2086–2132, 4015–4057 | M | A malformed marker produces a visible "marker rejected: reason" trace rather than silent skip (fixes R9) |
| **P2.5** | **Speaker notes on PPTX.** Set `slide.notes_slide.notes_text_frame.text` per slide in `pptx_export`. | `pptx_export.py` each `_add_*_slide` | S | Exported decks have speaker notes visible in PowerPoint's notes pane |

### Non-goals (explicit)

These are a **shared library ceiling** across Claude, MiniMax, Kimi, *and* Zhanlu. All four sit on `python-pptx`/`python-docx`. Pursuing them is not a differentiator and is out of scope:

- **Slide animations and transitions** — `python-pptx` has no API for them. (Absent on all four platforms.)
- **SmartArt** — out of scope for the underlying libraries. (Absent on all four.)
- **Pivot tables (Excel)** — not applicable to PPTX/DOCX, but flagged as a shared ceiling for the xlsx skill.
- **Beating Kimi on native file generation** — Kimi has no native pipeline; there is nothing to beat. Kimi's only real advantage is 1M-token context for *content drafting*, which is orthogonal to file production and should be matched at the agent/context layer, not the exporter layer.

---

## 7. Architecture (current vs. proposed)

### Current

```
LLM ──► writes file to outputs/ ──► emits ◤PPTX◤/◤MD_DOCX◤ marker
                                            │
              find_markers() (silent skip on malformed)
                                            │
                              _create_artifact_tool() ──► store blob
                              (no validation)

ExportService.get_or_render()
   └─ _payload_from_artifact() ── ReportCardPayload (fixed shape)
   └─ render(format, payload) ── pptx_export / docx_export (fixed sequence)
   └─ store_blob()  ← NO audit, NO image check, NO XSD validate
```

### Proposed (P0 + P1)

```
LLM ──► writes file to outputs/ ──► emits marker
                                            │
                              find_markers() + structured warning (P2.4)
                                            │
                              _create_artifact_tool() ──► store blob

ExportService.get_or_render()
   └─ _payload_from_artifact() ── ReportCardPayload (+ document_type, + theme)  [P1.1, P1.2]
   └─ render(format, payload, theme) ── templated renderer                      [P1.1, P1.2, P1.3]
   └─ qa.audit(rendered_bytes) ──► ValidationReport                             [P0.1, P0.2]
   │     ├─ density / overlap / off-canvas / placeholder checks
   │     ├─ WCAG contrast computation
   │     └─ XSD structural re-validation                                        [P1.4]
   └─ if report.failed: mark_version_failed() (NOT stored as preview_ready)
   └─ qa.render_image(rendered_bytes) ──► preview PNG blob                      [P0.3]
   └─ store_blob(original) + store_blob(preview)
   └─ mark_version_built(validation_report=report)                              [fixes R10]
```

---

## 8. Summary scorecard

| Axis | Claude | MiniMax | Kimi | Zhanlu (now) | Zhanlu (after P0) | Zhanlu (after P0+P1) |
|---|---|---|---|---|---|---|
| Formatting accuracy | Strong | Adequate | Absent | Adequate | Adequate | **Strong** (theming) |
| Layout preservation | Adequate | Adequate | Absent | Adequate | **Strong** (enforced) | **Strong** (+ round-trip) |
| Template handling | Adequate | Adequate | Absent | Absent | Absent | **Strong** |
| Content structuring | Adequate | Adequate | Absent | Weak | Weak | **Strong** (type branching) |
| Skill-body guidance | Adequate | Adequate | Absent | **Strong** | **Strong** | **Strong** |
| Runtime enforcement | Weak | Weak | Absent | Absent | **Strong** | **Strong** |

**After P0 alone, Zhanlu leads on runtime enforcement** — the one dimension where every competitor is weak. After P0+P1, Zhanlu leads or ties on every axis.

---

## Appendix A — Verified code references

| File | Read confirms |
|---|---|
| `backend/app/services/artifact_markers.py` | `MARKER_PATTERN` regex (line 33); `SUPPORTED_KINDS = {MD_DOCX, HTML_DOCX, PPTX}` (line 38); `find_markers()` silent-skips malformed JSON / unknown kinds (lines 60–92) |
| `backend/app/services/artifacts/artifact_service.py` | `create_artifact()` validates `artifact_type` only (lines 38–52); lifecycle `draft → building → preview_ready → validated` (line 8); `mark_version_built()` accepts caller-supplied `validation_report`, never auto-computed (lines 258–276) |
| `backend/app/services/artifacts/exporters/pptx_export.py` | Hardcoded palette lines 40–53; `Presentation()` blank + `slide_layouts[6]` line 66; fixed 6-slide sequence lines 68–78; native charts via `XL_CHART_TYPE` lines 256–267; data table truncates at 24 rows lines 328/360; **no theme param, no notes, no images, no overlap check** |
| `backend/app/services/artifacts/exporters/docx_export.py` | `python-docx` primary + pandoc fallback (lines 51, 155); hardcoded Calibri 11pt (line 63); fixed "Light Grid Accent 1" table style (lines 85/117/127/132); BytesIO in-memory + tempfile cleanup in `finally` (lines 150/174); **no headers/footers/page numbers/TOC/tracked changes/comments/images** |
| `backend/app/services/artifacts/exporters/service.py` | `ExportService.get_or_render()` (line 92); HTML-canonical branch (lines 229–233); `_render_and_store()` renders → stores with **no audit step** (lines 212–272); `eager_render_default()` swallows all errors (lines 160–164); `_payload_from_artifact()` synthesizes fresh payload, no round-trip (lines 399–431) |
| `backend/app/routers/agents.py` | Marker handling at lines 2086–2132 (non-SSE) and 4015–4057 (v3 SSE); both wrap in `try/except` and log warning on failure; both call `_create_artifact_tool()` then `strip_markers()` |
| `backend/app/services/artifacts/exporters/_common.py` | `ExportContext` dataclass (line 27); `ReportCardPayload` imported from `synexia.contracts` (line 23) |
| `backend/skills/pptx/SKILL.md` | 34.93 KB; 65 H1–H3 headers verified; 12-point self-audit (line 505); WCAG contrast (line 243); density 6×6 (line 178); anti-pattern gallery (line 599); accessibility quick pass (line 410) |
| `backend/skills/docx/SKILL.md` | 29.68 KB; 87+ H1–H3 headers verified; 5-type rubric (line 55); 10-point self-audit (line 333); typography & page layout (line 244); anti-pattern gallery (line 359); HTML→DOCX vs MD→DOCX chooser (line 413) |
| `backend/skills/` directory | 5 PPTX-related skills (`pptx/`, `pptx-author/`, `presentation-generator/`, `frontend-slides/`, `ppt-template-creator/`); 2 DOCX-related (`docx/`, `md-to-docx/`); legacy `skills2/` tree with pre-overhaul versions |

## Appendix B — Competitor mechanism summary (from prior research pass)

- **Claude:** Anthropic-managed Skills (xlsx/pptx/docx/pdf) via Skills API beta; sandboxed `python-pptx`/`python-docx`/`openpyxl`/`reportlab`; returns `file_id` via Files API. XSD-clean by construction. Beta (3 `anthropic-beta` headers). No animations/SmartArt. Self-audit advisory only.
- **MiniMax:** Open-source `MiniMax-AI/skills` repo (`minimax-docx`/`minimax-xlsx`/`minimax-pdf`/`minimax-pptx`); same Python stack + `pypdf`/`PyMuPDF`; more aggressive on raw OOXML (tracked changes/comments via `lxml`); task-routing table in `minimax-xlsx`; design-token PDF skill. Local clones at `backend/skills/minimax-xlsx/` and `backend/skills/minimax-pdf/`.
- **Kimi:** K3 API exposes Tool Calling + dynamic tool loading; official tools list = web search only (flagged "not recommended near-term"). No first-party PPTX/DOCX skill. No sandboxed code-execution-with-file-download. Consumer "Kimi Slides" produces web-style HTML decks, not real `.pptx`. Verdict: **Absent** for native Office file generation.
