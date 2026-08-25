# Modern PPT Renderer (HTML Design Path) — Design Spec

**Date:** 2026-08-25
**Parent design:** `docs/superpowers/specs/2026-08-13-professional-ppt-generation-system-design.md` (6-phase plan; this spec is Sub-project A.1, the renderer core)
**Status:** Design approved, ready for implementation plan
**Scope of this spec:** The HTML-based design-rich render path, dual-mode (image-fill + editable), smart routing, theme system, frontend-slides integration. Other sub-projects (B Content Engine, C Data Intelligence, D Imagery, E QA) are tracked separately.

---

## 1. Problem Statement

The current PPT generation pipeline produces decks that look "not professional" — Calibri everywhere, default-blue charts, limited layout vocabulary, no real typography, no theme system that actually applies. The existing `pptx_export.py` (`backend/app/services/artifacts/exporters/pptx_export.py`) and `layout_engine.py` are solid for **data-accurate** reports but cannot match the visual richness of Kimi / Claude / Gamma / Beautiful.ai decks, which all use HTML-based rendering with full CSS / typography / theming control.

We have **eight high-quality reference PPT skill implementations** already installed in `backend/skills/ppt_skills/` but **none are wired in**:
- `frontend-slides-main` — 12 curated style presets + 34 bold design templates (Claude Code skill format)
- `AgentBuff-Presentation-Skills-main` — same structure as frontend-slides, alternative template set
- `guizang-ppt-skill-main`, `knowledge-cat-ppt-skill-main`, `Powerpoint-fancy-design-main`, `slide-creator-main`, `Slide-maestro_agents-skill-main`, `slide-skill-master` — additional design libraries

`frontend-slides` is the best starting point: it's coding-agent-native, has a strong anti-AI-slop philosophy, ships `viewport-base.css` for fixed 16:9 stage, and uses 12 distinct curated style presets plus a 34-template bold pack.

The 2026-08-13 spec described this work as Phase 4 of a 6-phase plan. This spec delivers it as a standalone sub-project with concrete components, file paths, and test plan.

---

## 2. User Decisions (this brainstorm)

| Decision | Choice |
|---|---|
| Quality dimensions in scope | **All four**: visual richness, content/narrative, data intelligence, AI imagery. (Sub-projects A-E.) |
| Renderer approach | **Two paths, smart routing** — structured python-pptx for data-heavy, HTML design for storytelling |
| Reference skill to integrate first | **frontend-slides-main** (12 presets + 34 bold templates) |
| Renderer pipelines (v1) | **BOTH image-fill AND editable-text**, LLM picks per deck |
| Existing 2026-08-13 design | Continue to be the umbrella spec; this doc is the concrete v1 implementation of Sub-project A |

---

## 3. Architecture

The new pipeline is a **two-path renderer** wired into the existing artifact service:

```
User: "Make a sales recap deck — make it look good"
     │
     ▼
1. create_artifact (existing tool handler)
     │
     ▼
2. export_service.render_artifact (existing entry point)
     │
     ▼
3. deck_planner.build_plan(payload, ctx) → DeckPlan
   (existing module — already classifies deck_type, recommends theme)
     │
     ▼
4. deck_router.route(plan, ctx) → "structured" | "html_design"
   (existing module — keywords, deck_type, slide count drive decision)
     │
     ├─ "structured" ────────────► layout_engine + pptx_export (existing)
     │                              (data-heavy path — keep as-is)
     │
     └─ "html_design" ─────────► render_html_deck(plan, ctx)  [NEW]
                                    │
                                    ├─ select_theme(plan, ctx) → ThemePreset
                                    │
                                    ├─ generate_html_slides(plan, theme, ctx)
                                    │     │
                                    │     └─ frontend-slides SKILL.md Phase 3 logic,
                                    │        automated (no interactive preview-pick step)
                                    │
                                    ├─ render_pptx(html_files, theme, mode)
                                    │     │
                                    │     ├─ mode=image_fill   → Playwright screenshot + image fills  [v1.0]
                                    │     └─ mode=editable_text → html2pptx.js / pptxgenjs-based extraction  [v1.1]
                                    │
                                    └─ pptx_bytes
     │
     ▼
5. ArtifactBlob storage → chat preview (existing pptx_slide_html, with fallback for image-only decks)
```

**Why two pipelines, not one:**

| Pipeline | Strengths | Trade-offs | Default for |
|---|---|---|---|
| **image_fill** | Best visual fidelity; matches design exactly; works with any CSS | Loses text editability; ~3-5 MB per file | "Make it look good", investor decks, marketing |
| **editable_text** | Preserves text editability; smaller files | Lower fidelity; complex CSS may be lost | "I need to edit the slides", training materials, internal docs |

**LLM chooses per deck** based on user intent keywords (`edit`, `tweak`, `update later` → editable; `pretty`, `investor`, `marketing` → image-fill). Default is image-fill (better fidelity).

---

## 4. Components

All new code lives under `backend/app/services/artifacts/`. All flags default OFF per project convention.

### 4.1 `render_html_deck(plan, ctx) -> bytes`

**Location:** `backend/app/services/artifacts/render_html_deck.py` (NEW)

**Signature:**
```python
def render_html_deck(plan: DeckPlan, ctx: ExportContext) -> bytes:
    """Render a DeckPlan as a PPTX using the HTML design path.
    Returns pptx bytes. Raises RenderError on unrecoverable failure.
    """
```

**Responsibilities:**
1. Resolve theme via `select_theme`
2. Generate HTML slides via `generate_html_slides`
3. Convert to PPTX via `render_pptx` (mode chosen by router)
4. Return bytes; `export_service` writes to ArtifactBlob

**Failure modes** (each raises a distinct exception so `export_service` can fall back to `structured`):
- `ThemeNotFoundError` — unknown `theme_recommendation`
- `HtmlGenerationError` — LLM/template error in slide render
- `PptxRenderError` — Playwright failure
- Each handled with structured-renderer fallback in `export_service.render_artifact`

### 4.2 `select_theme(plan, ctx) -> ThemePreset`

**Location:** `backend/app/services/artifacts/themes.py` (NEW)

**Returns:** A `ThemePreset` dataclass containing:
- `name` (e.g. "vintage_editorial")
- `font_display` (e.g. "Fraunces")
- `font_body` (e.g. "Work Sans")
- `color_tokens` (Dict[str, str]) — CSS variable values from `STYLE_PRESETS.md`
- `signature_elements` (List[str]) — preset-specific components (e.g. "drop caps", "halftone")
- `layout_overrides` (Dict[LayoutName, Dict]) — per-layout tweaks

**Mapping logic (initial):**

| `deck_type` | Default preset | Keyword overrides |
|---|---|---|
| `data_report` | Electric Studio | `editorial`→Vellum; `report`→Swiss Modern; `technical`→Neon Cyber |
| `investor_deck` | Bold Signal | `calm`→Paper & Ink; `modern`→Cartesian (bold) |
| `marketing` | Creative Voltage | `wellness`→Dark Botanical; `playful`→Pastel Geometry |
| `executive_brief` | Paper & Ink | `bold`→Bold Poster (bold); `magazine`→Emerald Editorial |
| `training` | Notebook Tabs | `tech`→Terminal Green |

Bold template pack (`frontend-slides-main/bold-template-pack/`) provides the alternative choices — exposed as `theme_recommendation` string in `DeckPlan` so the LLM picks from the full catalog.

### 4.3 `generate_html_slides(plan, theme, ctx) -> list[Path]`

**Location:** `backend/app/services/artifacts/html_slide_generator.py` (NEW)

**Implements frontend-slides Phase 3 generation, fully automated:**
1. For each `SlidePlan`:
   - Pick layout from per-theme mapping
   - Render one HTML file using theme's CSS variables
   - Embed content (titles, bullets, KPI values, chart data)
2. Charts use **Chart.js** (loaded via CDN, simpler than ECharts; smaller bundle)
3. Output: one HTML per slide + one `index.html` orchestrator using `viewport-base.css` (mandatory fixed 16:9 stage)

**Layout catalog** (per theme, mapped from `SlidePlan.layout`):
- `cover` — hero title slide
- `agenda` — section listing
- `kpi_grid` — 2-4 KPI tiles with delta pills
- `chart_with_bullets` — chart on one side, bullets on the other
- `chart_full` — full-bleed chart with caption
- `findings_cards` — 3-4 insight cards
- `insights_bullets` — clean bullet slide with eyebrow
- `recommendations` — numbered action items
- `data_table` — sortable table (image-fill: rendered HTML; editable: extracted to native table)
- `methodology` — callout box with steps
- `section_divider` — large section number
- `closing` — thank-you / next steps

**Key requirements (from frontend-slides):**
- Include `viewport-base.css` in full in every generated HTML
- Use `clamp()` only for non-slide UI; slides use fixed 1920×1080 coordinates
- No `display: none` for slide switching (use `.active`/`.visible`)
- `prefers-reduced-motion` support
- No "AI slop" — distinct fonts, committed palette, no purple-gradient-on-white, no Inter
- Add detailed `/* === SECTION === */` comments

### 4.4 `render_pptx(html_files, theme, mode) -> bytes`

**Location:** `backend/app/services/artifacts/html_to_pptx.py` (NEW)

**Two sub-paths based on `mode`:**

#### Mode A: `image_fill` (v1.0)

```python
def _render_image_fill(html_files, theme) -> bytes:
    # 1. Start a tiny local HTTP server (port 0) to serve the HTML
    # 2. Launch Playwright Chromium headless
    # 3. Open index.html, screenshot each .slide element at 1920x1080
    # 4. Save PNG per slide
    # 5. Build python-pptx: 16:9 deck, each slide = full-bleed image fill
    # 6. Add speaker notes from SlidePlan.notes (extracted from HTML)
    # 7. Return pptx bytes
```

**Implementation notes:**
- Playwright already used in our stack (per the `export-pdf.sh` pattern in frontend-slides)
- Local HTTP server: `python -m http.server` on a free port, or `aiohttp.web` async
- Page transitions: read `.slide.active` selector, force visibility, screenshot
- Cleanup: tempdir removed after conversion
- Failure mode: Playwright timeout (60s default) → `PptxRenderError`

#### Mode B: `editable_text` (v1.1)

```python
def _render_editable_text(html_files, theme) -> bytes:
    # 1. Parse each HTML slide with BeautifulSoup
    # 2. Extract: title (h1/h2), bullets (ul/li), kpi values, chart data
    # 3. Map to a per-theme layout engine in python-pptx
    #    - title → themed text run
    #    - bullets → list paragraph
    #    - kpi values → themed text boxes
    #    - chart data → native pptx chart (CategoryChartData)
    # 4. Build deck; add speaker notes
    # 5. Return pptx bytes
```

**Why this is harder:** Each theme has its own visual grammar; mapping HTML structure back to python-pptx is non-trivial. v1.1 ships with **3 default themes only** (Electric Studio, Bold Signal, Vintage Editorial) and the rest stay image-fill until editable renderers are built for them. LLM picks editable mode only if user intent keyword matches AND theme is in editable-supported set.

**Sub-decision (v1.0 only):** ship image_fill first; editable_text is a v1.1 follow-up if needed. Both pipelines are architected now, but v1.0 only enables image_fill; editable_text stays behind `HTML_DESIGN_EDITABLE_ENABLED` (default OFF) and ships in a follow-up commit.

### 4.5 Wire into `export_service`

**Location:** `backend/app/services/artifacts/exporters/service.py` (MODIFY)

```python
def render_artifact(payload, ctx):
    if payload.kind == "pptx":
        plan = deck_planner.build_plan(payload, ctx) if PPT_DECK_PLANNER_ENABLED else None
        if plan and PPT_SMART_ROUTER_ENABLED:
            route = deck_router.route(plan, ctx)
            if route == "html_design" and HTML_DESIGN_RENDERER_ENABLED:
                try:
                    return render_html_deck(plan, ctx)
                except RenderError as e:
                    logger.warning("html_design render failed, falling back: %s", e)
                    # fall through to structured
        return _render_structured_pptx(payload, ctx)
    # ... other artifact kinds
```

### 4.6 Configuration

**Location:** `backend/app/config.py` (MODIFY)

```python
# New flags
HTML_DESIGN_RENDERER_ENABLED: bool = False
HTML_DESIGN_EDITABLE_ENABLED: bool = False
HTML_DESIGN_THEMES: list[str] = []  # empty = all 12 presets; user can restrict

# Existing flags (kept)
PPT_DECK_PLANNER_ENABLED: bool = False
PPT_SMART_ROUTER_ENABLED: bool = False
```

### 4.7 Frontend preview fallback

**Location:** `frontend/src/components/chat/MessageBubble.jsx` + `frontend/src/pages/MarketDashboard/components/WeeklyDigest/ReportDetail.jsx` (MODIFY)

When an artifact is image-fill rendered, the existing `pptx_slide_html.py` preview won't reflect the design (text-only fallback). Add a check: if artifact has `render_mode="image_fill"`, use the PNG preview from Playwright as the thumbnail; user gets a faithful preview.

---

## 5. Data Flow (one happy path)

```
1. User: "Make a sales recap deck for Q3 — make it look good"
   ↓
2. create_artifact (existing)
   ↓
3. export_service.render_artifact (existing entry, with router)
   ↓
4. deck_planner.build_plan(payload, ctx) → DeckPlan
   {
     title: "Q3 Sales Recap",
     deck_type: "data_report",
     theme_recommendation: "vintage_editorial",  # LLM picks from style catalog
     slides: [
       SlidePlan(layout="cover", title="Q3 2026 Sales Recap", subtitle="..."),
       SlidePlan(layout="agenda", ...),
       SlidePlan(layout="kpi_grid", kpi_specs=[
         {label: "Revenue", value: "$2.4M", delta: "+12%"},
         {label: "Orders", value: "1,847", delta: "+5%"},
         {label: "Margin", value: "31%", delta: "-2pp"},
       ]),
       SlidePlan(layout="chart_with_bullets", chart_spec={
         type: "bar", data: {labels: ["Q1","Q2","Q3"], values: [...]}
       }),
       SlidePlan(layout="findings_cards", cards=[...]),
       SlidePlan(layout="closing", ...)
     ]
   }
   ↓
5. deck_router.route(plan, ctx) → "html_design"
   decision: design keyword "look good" + brief length = 6 slides
              + deck_type ∈ {data_report, investor, ...}
   ↓
6. render_html_deck(plan, ctx)
   ├─ theme = select_theme(plan) → Vintage Editorial preset
   │    (Fraunces + Work Sans, cream background, warm accents)
   ├─ for each slide_plan:
   │    layout = lookup_layout("vintage_editorial", slide_plan.layout)
   │    html_i = render_slide(layout, slide_plan)
   │    # → /tmp/slide_1_cover.html, /tmp/slide_2_agenda.html, ...
   ├─ index.html = build_stage(html_files, theme)
   │    # viewport-base.css in full, theme CSS variables in :root
   ├─ mode = deck_router.pick_pptx_mode(plan, ctx)
   │    # "look good" + no "edit" keyword → mode = "image_fill"
   ├─ pptx_bytes = _render_image_fill(index.html, theme, slide_htmls)
   │    # Playwright screenshots each .slide at 1920x1080
   │    # python-pptx builds 16:9 deck with image fills
   ↓
7. store as ArtifactBlob → preview = first slide PNG (from Playwright)
   ↓
8. user downloads → opens in PowerPoint → sees exactly the design
```

---

## 6. Error Handling & Graceful Degradation

| Failure point | Recovery |
|---|---|
| LLM planner times out (10s) | `deck_planner` returns a minimal 3-slide plan (cover + table of contents + closing) |
| LLM planner returns invalid JSON | `json_repair` retries 2x; then minimal plan |
| `select_theme` can't map `theme_recommendation` | Log warning, default to deck_type's default preset |
| HTML generation fails on one slide | Mark slide `text_fallback`, render as a structured-renderer slide, splice into the same PPTX |
| HTML generation fails on all slides | Raise `HtmlGenerationError`; fall back to structured renderer |
| Playwright not installed | `PptxRenderError` → fall back to structured renderer (better than failing) |
| Playwright times out (>60s) | Same as above |
| Image fill mode → user wants to edit | Show tooltip in preview: "This deck is rendered as images. To edit, regenerate with editable mode." |

All fallbacks are deterministic — same input + same failure mode → same output. The audit/repair module (Phase 2 of the parent design) catches overflow, contrast, palette issues on the rendered output.

---

## 7. Testing Strategy

| Test file | Covers | Time budget |
|---|---|---|
| `tests/artifacts/test_select_theme.py` | Theme mapping matrix (deck_type × keyword → preset); 50+ test cases | <1s |
| `tests/artifacts/test_html_slide_generator.py` | Per layout type renders valid HTML; no overflow (`scrollHeight ≤ 1080`); theme CSS variables present; no Inter font; no purple gradient on white | <5s |
| `tests/artifacts/test_html_to_pptx_image_fill.py` | End-to-end with 3-slide fixture; Playwright screenshots; pptx validates with `python-pptx`; visual snapshot against committed PNGs | ~30s |
| `tests/artifacts/test_render_html_deck_e2e.py` | Full chain — plan in, pptx out, open in LibreOffice headless (`soffice --headless --convert-to png`), visual diff | ~60s |
| `tests/artifacts/test_router_prefers_html.py` | Design-heavy prompts route to `html_design`; data-heavy prompts stay `structured`; no regressions in existing router tests | <1s |
| `tests/artifacts/test_graceful_degradation.py` | Kill Playwright mid-flight → structured fallback; kill LLM planner → minimal plan; corrupt slide HTML → other slides render | <5s |
| `tests/artifacts/test_theme_presets_catalog.py` | All 12 presets load; each renders at least one valid slide; no missing CSS variables | <10s |

**Visual regression:** For each of the 12 presets, commit a 3-slide fixture deck + PNG snapshots. Any theme CSS change triggers snapshot diff alert in CI.

**Snapshot test command:**
```bash
cd backend && venv/bin/python -m pytest tests/artifacts/test_html_to_pptx_image_fill.py -p no:jaxtyping -p no:cacheprovider
# UPDATING snapshots: --snapshot-update (only when intentional)
```

**E2E test (run in container):**
```bash
docker exec zhanlu-backend /usr/local/bin/python -m pytest \
  tests/artifacts/test_render_html_deck_e2e.py \
  -p no:jaxtyping -p no:cacheprovider
```

---

## 8. Phased Delivery

### Phase A.1: Foundation (this spec's core, ~3 weeks)

| Task | File | Deliverable |
|---|---|---|
| 1.1 Wire `render_html_deck` entry into `export_service` | `exporters/service.py` | New `if HTML_DESIGN_RENDERER_ENABLED:` branch |
| 1.2 Implement `select_theme` | `artifacts/themes.py` | 12-theme mapping table, unit tests |
| 1.3 Implement `generate_html_slides` | `artifacts/html_slide_generator.py` | 12 layout types × 12 themes = 144 layouts, all parameterized |
| 1.4 Implement `_render_image_fill` | `artifacts/html_to_pptx.py` | Playwright screenshot + python-pptx image fills |
| 1.5 Add 3 config flags | `app/config.py` | `HTML_DESIGN_RENDERER_ENABLED`, `HTML_DESIGN_EDITABLE_ENABLED`, `HTML_DESIGN_THEMES` |
| 1.6 Frontend preview fallback | `MessageBubble.jsx`, `ReportDetail.jsx` | Use PNG thumbnail for image_fill artifacts |
| 1.7 Theme system tests | `tests/artifacts/test_select_theme.py` | 50+ matrix cases |
| 1.8 Generator tests | `tests/artifacts/test_html_slide_generator.py` | Per-layout validity, no overflow, no AI slop |
| 1.9 Image-fill E2E test | `tests/artifacts/test_html_to_pptx_image_fill.py` | 3-slide fixture → visual snapshot |
| 1.10 Gracious-degradation tests | `tests/artifacts/test_graceful_degradation.py` | All fallback paths covered |

**Success metric:** With `HTML_DESIGN_RENDERER_ENABLED=True` and `PPT_DECK_PLANNER_ENABLED=True`, "Make a sales recap deck" produces a 6-slide PPTX in a chosen theme, with each slide rendering correctly. Visual quality matches a frontend-slides manual render of the same content.

### Phase A.2: Editable text pipeline (v1.1, follow-up commit, ~1.5 weeks)

| Task | File | Deliverable |
|---|---|---|
| 2.1 Implement `_render_editable_text` | `artifacts/html_to_pptx.py` | Parse HTML → extract structured content → native pptx |
| 2.2 Editable layout engine | `artifacts/editable_layouts.py` | Per-theme mapping (3 default themes) |
| 2.3 Editable-mode tests | `tests/artifacts/test_html_to_pptx_editable.py` | Round-trip: HTML → PPTX → parse text → assert match |

**Success metric:** User regenerates the same deck in editable mode → can edit text in PowerPoint, layout preserved (best-effort).

### Phase A.3: Wider theme support (v1.2, follow-up, ~1 week)

Extend editable-text support to all 12 themes + top 5 bold templates. Each new theme needs its own layout-to-pptx mapping; cheaper than A.1 because the parsing layer is shared.

---

## 9. Key Files

| File | Status | Purpose |
|---|---|---|
| `backend/app/services/artifacts/render_html_deck.py` | NEW | Main entry point |
| `backend/app/services/artifacts/themes.py` | NEW | Theme catalog + mapping |
| `backend/app/services/artifacts/html_slide_generator.py` | NEW | HTML slide generation per theme/layout |
| `backend/app/services/artifacts/html_to_pptx.py` | NEW | image_fill + editable_text pipelines |
| `backend/app/services/artifacts/exporters/service.py` | MODIFY | Wire router → html_design path with structured fallback |
| `backend/app/services/artifacts/deck_router.py` | MODIFY | Add `pick_pptx_mode(plan, ctx)` for image_fill vs editable |
| `backend/app/config.py` | MODIFY | 3 new flags |
| `frontend/src/components/chat/MessageBubble.jsx` | MODIFY | Use PNG preview for image_fill artifacts |
| `frontend/src/pages/MarketDashboard/components/WeeklyDigest/ReportDetail.jsx` | MODIFY | Same |
| `tests/artifacts/test_select_theme.py` | NEW | Theme matrix tests |
| `tests/artifacts/test_html_slide_generator.py` | NEW | Layout validity tests |
| `tests/artifacts/test_html_to_pptx_image_fill.py` | NEW | E2E + visual snapshot |
| `tests/artifacts/test_render_html_deck_e2e.py` | NEW | Full chain E2E |
| `tests/artifacts/test_router_prefers_html.py` | NEW | Routing decisions |
| `tests/artifacts/test_graceful_degradation.py` | NEW | Fallback paths |
| `tests/artifacts/test_theme_presets_catalog.py` | NEW | All 12 presets load |
| `docs/superpowers/specs/2026-08-13-professional-ppt-generation-system-design.md` | REFERENCE | Parent 6-phase design |

**Skill files to consume (read-only):**
- `backend/skills/ppt_skills/frontend-slides-main/SKILL.md` — generation rules
- `backend/skills/ppt_skills/frontend-slides-main/STYLE_PRESETS.md` — theme catalog
- `backend/skills/ppt_skills/frontend-slides-main/viewport-base.css` — stage CSS (include in full)
- `backend/skills/ppt_skills/frontend-slides-main/html-template.md` — HTML structure
- `backend/skills/ppt_skills/frontend-slides-main/animation-patterns.md` — animation snippets
- `backend/skills/ppt_skills/frontend-slides-main/bold-template-pack/selection-index.json` — additional 34 templates
- `backend/skills/ppt_skills/frontend-slides-main/scripts/extract-pptx.py` — reference (not used directly)

---

## 10. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| LLM planner adds latency (2-5s for plan, 5-15s for HTML gen) | `HTML_DESIGN_RENDERER_ENABLED` is opt-in; structured path stays fast for users who don't need design |
| Playwright cold start adds 5-10s | Keep Chromium pre-installed in Docker; cache first-run artifacts |
| Playwright not in Docker image | Graceful fallback to structured renderer (logged warning) |
| html2pptx fidelity gaps (CSS rounded corners, gradients, etc.) | image_fill mode bypasses this; editable_text limited to simple layouts |
| Font availability in Docker | Embed font files in `themes/fonts/`; system fallbacks to web-safe |
| Image-fill PPTX is non-editable | Acceptable trade-off in v1; editable_text v1.1 covers this for 3 themes |
| Theme sprawl (12 + 34 = 46 themes) | v1 ships 12 presets; bold pack enabled in v1.2; user can disable via `HTML_DESIGN_THEMES` config |
| Visual regression when CSS changes | Snapshot tests + visual diff in CI |
| Backward compatibility | All flags default OFF; structured path unchanged when flags are off |

---

## 11. Self-Review

- [x] **Placeholder scan:** No TBDs. Every module has a clear signature and file path. v1.1 / v1.2 follow-ups are explicit, not hidden TODOs.
- [x] **Internal consistency:** Architecture (Section 3) matches components (Section 4) matches data flow (Section 5). Router is the single source of truth for routing decisions.
- [x] **Scope check:** Single implementation plan (A.1 = 3 weeks). A.2 / A.3 are explicit follow-ups, not scope creep. Sub-projects B/C/D/E tracked separately.
- [x] **Ambiguity check:** "Both pipelines" resolved as: v1.0 ships image_fill; v1.1 ships editable_text behind separate flag. "LLM chooses per deck" is keyword-based with documented mapping.
- [x] **No external dependencies:** All needed tools (Playwright, python-pptx) are already in our stack.
- [x] **Backward compatible:** All new flags default OFF; structured path unchanged.
- [x] **Frontend impact bounded:** Only thumbnail preview change; no UX flow changes.

---

## 12. Notes for Future Work

- Sub-project B (Content Engine): polish `deck_planner.py` and `copy_polish.py` prompts to make the LLM produce tighter headlines and richer narratives. Independent of A but the output flows into A's `SlidePlan` inputs.
- Sub-project C (Data Intelligence): enhance `data_profiler.py` to suggest chart types per column + add inline source attribution. Independent of A.
- Sub-project D (Visual Identity & Imagery): add AI image generation (cover art, hero graphics) as a new sub-renderer that runs after HTML slide generation but before screenshot. ~3-4 weeks.
- Sub-project E (QA Loop): add visual quality scoring (LLM-judge test) + user rating capture + quality feedback into deck_planner prompts. ~1-2 weeks.
- The 7 other reference skills in `backend/skills/ppt_skills/` (AgentBuff, guizang, knowledge-cat, etc.) become inputs to A.3 or independent integrations.
- The existing `slide-creator-main/tests/test_pptx_export_smoke.py` is a useful template for our `test_html_to_pptx_image_fill.py` smoke test.

---

**Spec written and committed to** `docs/superpowers/specs/2026-08-25-modern-ppt-renderer-design.md`. **Please review it and let me know if you want to make any changes before we start writing out the implementation plan.**
