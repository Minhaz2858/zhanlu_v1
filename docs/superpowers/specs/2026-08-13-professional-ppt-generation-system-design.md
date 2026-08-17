# Professional PPT Generation System — Design Spec

**Date:** 2026-08-13
**Approach:** A — Incremental Enhancement (recommended)
**Status:** Design approved, ready for implementation plan

---

## 1. Problem Statement

The current PPT generation pipeline produces decks that look "not professional." Root causes identified through codebase analysis:

1. **Fonts hardcoded to Calibri** (`_theme.py:155-156`) — the theme JSON `fonts.heading`/`fonts.body` fields are parsed but never applied to slide text runs. Every deck across every tenant renders in the same generic Office font.
2. **Chart series ignore theme palette** (`pptx_export.py:345-363`) — `chart_palette` is defined in `DeckTheme` but never passed to `add_series`. Native pptx charts always render default blue.
3. **Preview ≠ download** (`pptx_slide_html.py:45-55`) — the HTML preview re-derives its own palette via `_chart_ramp()`, so the chat preview doesn't match the downloaded file.
4. **Audit gate is empty** (`audits/audit_deck.py` = 0 bytes) — the P0 semantic quality loop in `service.py:544-574` runs but the audit script produces no output, so no quality check ever fires.
5. **Repair module is empty** (`repairs/repair_deck.py` = 0 bytes) — even if the audit caught issues, there's no repair logic.
6. **No content structuring** — the agent emits a raw `ReportCardPayload` directly from data rows. There's no intermediate "deck plan" step that decides slide order, layout per slide, or narrative flow.
7. **No smart routing** — all PPT requests go through the structured python-pptx renderer. The `html2pptx` sandbox skill (which has rich CSS-based design capabilities) is only used as a fallback.
8. **No LLM copy polish** — slide titles and bullets are whatever the agent puts in the payload. There's no post-generation pass to tighten headlines or improve readability.

## 2. User Decisions

| Decision | Choice |
|---|---|
| Renderer path | **Both, with smart routing** — structured renderer for data reports, sandbox skill for design-heavy requests |
| Data ingestion | **Database/knowledge base (existing)** — no new data source types; focus on improving what comes through `ask_data_agent` |
| Copywriting | **Template + LLM polish** — deterministic templates generate structure, LLM polishes headlines/insights |
| Scope | **Full system** — all phases (ingestion refinement, structuring, design, copywriting, charts, QA, feedback loop) in one plan |

## 3. Architecture

The system extends the existing PPT pipeline with four new layers, all behind feature flags (default OFF):

```
User: "Make a PPT about Q3 sales"
     │
     ▼
1. DATA INGESTION (existing, enhanced)
   ask_data_agent → execute_query → raw rows
   Enhancement: data profiling (detect column types, suggest chart types)
     │
     ▼
2. CONTENT STRUCTURING (NEW — deck_planner.py)
   raw rows + user intent → LLM call → DeckPlan {
     slides: [SlidePlan{layout, title, bullets, chart_spec, kpi_spec, notes}]
     theme_recommendation: "zhanlu-blue" | "midnight-galaxy" | ...
     deck_type: "data_report" | "investor_deck" | "marketing" | "executive_brief"
   }
     │
     ▼
3. SMART ROUTER (NEW — deck_router.py)
   deck_type == "data_report"      → structured pptx_export.py
   deck_type in {investor,marketing} → sandbox html2pptx skill
   deck_type == "executive_brief"  → structured (compact mode)
     │
     ▼
4a. STRUCTURED RENDERER (ENHANCED pptx_export.py)
   - Fonts: theme.font_heading/font_body applied to every text run
   - Chart palette: theme.chart_palette → series colors
   - Master layout: brand footer band + section eyebrow + bleed margins
   - Rich KPI tiles: value + delta pill + mini-sparkline
   - Cover art: on by default for non-data decks
   - Layout engine: per-slide layout selection (not fixed catalog)
     │
     ▼
4b. SANDBOX html2pptx (ENHANCED skill invocation)
   - Theme tokens (as_hex_dict) passed as config to the sandbox
   - LLM writes one HTML slide per SlidePlan with real CSS
   - html2pptx.js converts → .pptx
   - Design language: 18 palette options, geometric patterns, typography treatments
     │
     ▼
5. QUALITY ASSURANCE (NEW — audit_deck.py + repair_deck.py)
   audit_deck.py checks:
   - Text overflow/clipping (shape height vs estimated text height)
   - Color contrast (WCAG AA: text vs background, >= 4.5:1)
   - Chart palette applied (series colors match theme.chart_palette)
   - Footer present on all non-cover slides
   - Minimum font sizes (>= 10pt body, >= 24pt title)
   - Slide count in [3, 30]
   - No empty slides (every slide has >= 1 text shape with content)
   repair_deck.py:
   - Auto-shrink font on overflow (reduce by 2pt, re-check)
   - Auto-apply chart palette if series use default blue
   - Auto-add footer if missing
     │
     ▼
6. LLM POLISH (NEW — copy_polish.py)
   Post-render: LLM receives slide text (via pptx_slide_html.py render)
   + raw data → suggests:
     - Headline rewrites (tighter, more impactful)
     - Bullet tightening (reduce to <= 12 words per bullet)
     - Speaker notes generation (1-2 sentences per slide)
   → applied as a revised ReportCardPayload → re-render
     │
     ▼
7. STORAGE + PREVIEW (existing)
   ArtifactBlob + thumbnails + chat inline preview
```

### Feature Flags (all default OFF)

| Flag | Module | Purpose |
|---|---|---|
| `PPT_DECK_PLANNER_ENABLED` | `deck_planner.py` | Content structuring via LLM |
| `PPT_SMART_ROUTER_ENABLED` | `deck_router.py` | Route to structured vs sandbox |
| `PPT_LLM_POLISH_ENABLED` | `copy_polish.py` | Post-render copy improvement |
| `PPT_AUDIT_ENABLED` | `audit_deck.py` | Quality audit gate |

## 4. Component Details

### 4.1 Content Structuring (`deck_planner.py`)

**Location:** `backend/app/services/artifacts/deck_planner.py`

**Input:** Raw data rows (from `ask_data_agent`), user message, conversation context.

**Process:**
1. Profile the data: detect column types (numeric, categorical, temporal), row count, cardinality.
2. Call LLM with a structured prompt: "Given this data and user intent, produce a DeckPlan JSON with 5-12 slides."
3. The LLM returns a `DeckPlan` — a list of `SlidePlan` objects, each specifying:
   - `layout`: one of `cover`, `agenda`, `kpi_grid`, `chart_full`, `chart_with_bullets`, `findings_cards`, `insights_bullets`, `recommendations`, `data_table`, `methodology`, `section_divider`, `closing`
   - `title`: slide title
   - `bullets`: list of bullet texts
   - `chart_spec`: optional chart type + data selection
   - `kpi_spec`: optional KPI tiles
   - `notes`: speaker notes
   - `narrative_role`: one of `hook`, `context`, `evidence`, `insight`, `action`, `closing`
4. Map `DeckPlan` → `ReportCardPayload` (the existing contract) for the structured renderer, or pass `DeckPlan` directly to the sandbox skill for the html2pptx path.

**DeckPlan data contract:**
```python
class SlidePlan(BaseModel):
    layout: str  # cover | agenda | kpi_grid | chart_full | ...
    title: str
    subtitle: str = ""
    bullets: list[str] = []
    chart_spec: Optional[ChartSpec] = None
    kpi_specs: list[KPISpec] = []
    notes: str = ""
    narrative_role: str = "context"

class DeckPlan(BaseModel):
    title: str
    deck_type: str  # data_report | investor_deck | marketing | executive_brief
    theme_recommendation: str = "zhanlu-blue"
    slides: list[SlidePlan]
    summary: str = ""
    methodology: str = ""
```

### 4.2 Smart Router (`deck_router.py`)

**Location:** `backend/app/services/artifacts/deck_router.py`

**Logic:**
```python
def route_deck(deck_plan: DeckPlan, ctx: ExportContext) -> str:
    """Return 'structured' or 'sandbox'."""
    if not settings.PPT_SMART_ROUTER_ENABLED:
        return "structured"

    deck_type = deck_plan.deck_type
    if deck_type in ("investor_deck", "marketing"):
        return "sandbox"
    if deck_type == "executive_brief" and len(deck_plan.slides) <= 6:
        return "structured"
    # Keyword signals from user message
    user_msg = (ctx.user_message or "").lower()
    design_keywords = ["beautiful", "stunning", "investor", "pitch", "marketing",
                       "漂亮", "精美", "投资者", "路演"]
    if any(kw in user_msg for kw in design_keywords):
        return "sandbox"
    return "structured"
```

### 4.3 Enhanced Structured Renderer (`pptx_export.py`)

**Changes to existing file:**

1. **Font threading** — every `_add_textbox` and `p.add_run()` call sets `run.font.name = theme.font_heading` (for titles) or `theme.font_body` (for body). Currently `_add_textbox` never sets `font.name`.

2. **Chart palette** — in `_add_chart_slide`, after creating the chart, iterate series and set `series.format.fill.fore_color.rgb` from `theme.chart_palette[i % len(palette)]`.

3. **Preview palette unification** — in `pptx_slide_html.py`, replace `_chart_ramp(_detect_primary_hex(prs))` with reading the actual chart series colors from the chart XML, or fall back to `_detect_primary_hex` only when the chart has no explicit colors.

4. **Layout engine** — replace the fixed `if payload.sections: _add_agenda_slide(...)` catalog with a `_build_slide(slide_plan, ...)` dispatcher that reads `slide_plan.layout` and calls the appropriate builder. The existing builders become layout implementations.

5. **Rich KPI tiles** — when `k.delta` is present, add a small colored pill shape (green for up, red for down). When `k.sparkline` data is present (new optional field on `KPISpec`), add a mini line chart.

6. **Cover art default** — set `ctx.cover_art = True` when `deck_type != "data_report"` (currently opt-in).

### 4.4 Audit Gate (`audit_deck.py`)

**Location:** `backend/app/services/artifacts/audits/audit_deck.py`

**Checks (each returns PASS/WARN/FAIL):**

| Check | Severity | Logic |
|---|---|---|
| Text overflow | FAIL | For each text shape, estimate text height (lines × font_size × 1.2) and compare to shape height. Flag if text height > shape height. |
| Color contrast | WARN | For each text shape, compute contrast ratio between text color and shape/slide background. Flag if < 4.5:1 (WCAG AA). |
| Chart palette | WARN | Check if chart series colors are all the same (default blue) instead of using `chart_palette`. |
| Footer presence | WARN | Every non-cover slide should have the "Generated by Zhanlu AI" footer. |
| Font size minimum | WARN | Body text < 10pt, title < 24pt. |
| Slide count | WARN | < 3 or > 30 slides. |
| Empty slides | FAIL | Any slide with no text content at all. |
| Font consistency | WARN | Mixed fonts within the same role (heading vs body). |

**Output format** (JSON to stdout, exit 0 = PASS/WARN, exit 1 = FAIL):
```json
{
  "status": "WARN",
  "summary": "2 warnings: chart palette not applied on slide 5, low contrast on slide 3",
  "checks": [
    {"name": "text_overflow", "status": "PASS", "details": ""},
    {"name": "color_contrast", "status": "WARN", "details": "slide 3: contrast 3.8:1 (title on warn_bg)"},
    {"name": "chart_palette", "status": "WARN", "details": "slide 5: all series same color"}
  ]
}
```

### 4.5 Repair Module (`repair_deck.py`)

**Location:** `backend/app/services/artifacts/repairs/repair_deck.py`

**Repairs:**
1. **Font overflow** — reduce font size by 2pt, re-render, re-audit. Max 3 shrink cycles.
2. **Missing chart palette** — detect default blue series, replace with `chart_palette` colors.
3. **Missing footer** — add footer textbox to slides that lack it.
4. **Low contrast** — adjust text color toward `theme.text` (darker) or `theme.muted` (lighter) to meet 4.5:1.

### 4.6 LLM Copy Polish (`copy_polish.py`)

**Location:** `backend/app/services/artifacts/copy_polish.py`

**Process:**
1. After initial render, extract all slide text via `pptx_slide_html.py` (or directly from the pptx via python-pptx).
2. Send to LLM with prompt: "You are a presentation copy editor. For each slide, tighten the title to <= 8 words, tighten bullets to <= 12 words each, and write 1-2 sentence speaker notes. Return JSON."
3. Apply the polished text back to the `ReportCardPayload` / `DeckPlan`.
4. Re-render the deck.
5. The polish runs ONCE (not in the audit repair loop).

### 4.7 Agent Prompt Enhancement (`agent_prompts.py`)

Update the PPT-related instructions to:
1. Tell the agent to call `deck_planner` tool (new) after `ask_data_agent` when the user asks for a PPT.
2. Include deck-type classification guidance (data_report vs investor_deck vs marketing).
3. Add a "design quality" expectation: "The deck must look professional — use the theme's fonts, apply the chart palette, and ensure every slide has a clear title and concise bullets."
4. Add the smart routing hint: "If the user asks for a 'beautiful' or 'investor' deck, use `run_sandbox_skill` for richer design."

## 5. Phases & Milestones

### Phase 1: Design Quality Quick Wins (Week 1)
**Goal:** Fix the most visible quality issues in the structured renderer.

| Task | File | Deliverable |
|---|---|---|
| 1.1 Thread theme fonts to all text runs | `pptx_export.py` | Every `run.font.name` set from `theme.font_heading`/`font_body` |
| 1.2 Apply chart palette to chart series | `pptx_export.py` | Series colors from `theme.chart_palette` |
| 1.3 Unify preview palette with download | `pptx_slide_html.py` | Preview reads actual chart colors, not re-derived |
| 1.4 Update default fonts in theme JSONs | `backend/data/themes/*.json` | `fonts.heading`/`body` set to professional fonts (Inter, etc.) |

**Success metric:** Side-by-side comparison of before/after decks shows brand-colored charts and non-Calibri fonts.

### Phase 2: Audit & Repair Gate (Week 2)
**Goal:** Implement the quality loop that's currently a no-op.

| Task | File | Deliverable |
|---|---|---|
| 2.1 Write `audit_deck.py` | `audits/audit_deck.py` | 8 checks (overflow, contrast, palette, footer, fonts, count, empty, consistency) |
| 2.2 Write `repair_deck.py` | `repairs/repair_deck.py` | 4 repairs (shrink, palette, footer, contrast) |
| 2.3 Wire `PPT_AUDIT_ENABLED` flag | `service.py`, `config.py` | Flag gates the audit loop |
| 2.4 Tests | `tests/test_audit_deck.py` | Unit tests for each check + repair |

**Success metric:** Audit catches and auto-repairs overflow + missing palette on a test deck.

### Phase 3: Content Structuring (Week 3)
**Goal:** Add the deck planning layer that structures data into a narrative.

| Task | File | Deliverable |
|---|---|---|
| 3.1 `DeckPlan`/`SlidePlan` contracts | `synexia/contracts.py` | Pydantic models |
| 3.2 `deck_planner.py` | `artifacts/deck_planner.py` | LLM call → DeckPlan |
| 3.3 Data profiler | `artifacts/data_profiler.py` | Column type detection, chart suggestion |
| 3.4 Layout engine in renderer | `pptx_export.py` | `_build_slide(slide_plan)` dispatcher |
| 3.5 `PPT_DECK_PLANNER_ENABLED` flag | `config.py` | Gate the planner |
| 3.6 Tests | `tests/test_deck_planner.py` | Planner + layout engine tests |

**Success metric:** A data query "show me Q3 sales by region" produces a 6-slide deck with cover, KPIs, chart, findings, recommendations, closing — in a logical narrative order.

### Phase 4: Smart Routing + Sandbox Enhancement (Week 4)
**Goal:** Route design-heavy requests to the html2pptx sandbox skill.

| Task | File | Deliverable |
|---|---|---|
| 4.1 `deck_router.py` | `artifacts/deck_router.py` | Routing logic (deck_type + keywords) |
| 4.2 Enhanced sandbox invocation | `sandbox_tool.py` | Pass `DeckPlan` + theme tokens to sandbox |
| 4.3 Agent prompt update | `agent_prompts.py` | Deck-type classification + routing hint |
| 4.4 `PPT_SMART_ROUTER_ENABLED` flag | `config.py` | Gate the router |
| 4.5 Tests | `tests/test_deck_router.py` | Routing decision tests |

**Success metric:** "Make a beautiful investor deck" routes to sandbox; "Show me Q3 sales as PPT" routes to structured.

### Phase 5: LLM Copy Polish (Week 5)
**Goal:** Post-render copy improvement pass.

| Task | File | Deliverable |
|---|---|---|
| 5.1 `copy_polish.py` | `artifacts/copy_polish.py` | LLM polish → revised payload → re-render |
| 5.2 Speaker notes generation | `copy_polish.py` | 1-2 sentence notes per slide |
| 5.3 `PPT_LLM_POLISH_ENABLED` flag | `config.py` | Gate the polish |
| 5.4 Tests | `tests/test_copy_polish.py` | Polish prompt + application tests |

**Success metric:** Polished deck has tighter headlines (<= 8 words) and bullets (<= 12 words) vs unpolished.

### Phase 6: Feedback Loop + Integration (Week 6)
**Goal:** Wire everything together end-to-end + user feedback mechanism.

| Task | File | Deliverable |
|---|---|---|
| 6.1 End-to-end integration | `service.py`, `agent_prompts.py` | Planner → router → render → audit → polish → store |
| 6.2 User feedback capture | `artifacts/feedback.py` | "Rate this deck" → stores rating on Artifact |
| 6.3 Rating-based improvement | `deck_planner.py` | Use past ratings to improve future plans |
| 6.4 E2E tests | `tests/test_ppt_e2e.py` | Full pipeline test |
| 6.5 Documentation | `docs/ppt-generation.md` | Architecture + usage guide |

**Success metric:** User can generate a deck, rate it, and see improvement on the next generation.

## 6. Success Metrics (Overall)

| Metric | Baseline | Target |
|---|---|---|
| Chart palette applied | 0% (always default blue) | 100% |
| Font variety | 1 (Calibri only) | 11+ (one per theme) |
| Audit pass rate | N/A (audit is no-op) | >= 90% PASS after repair |
| Text overflow incidents | Unknown (no audit) | 0 after repair |
| Preview/download match | No (different palettes) | Yes |
| Slide narrative structure | Fixed catalog | Data-driven via DeckPlan |
| Copy quality (headline length) | Variable | <= 8 words (after polish) |
| User deck rating | N/A | >= 4/5 average |

## 7. Key Files to Modify

| File | Changes |
|---|---|
| `backend/app/services/artifacts/exporters/pptx_export.py` | Font threading, chart palette, layout engine, rich KPIs |
| `backend/app/services/artifacts/exporters/_theme.py` | Ensure fonts flow through all paths |
| `backend/app/services/artifacts/pptx_slide_html.py` | Unify preview palette with download |
| `backend/app/services/artifacts/audits/audit_deck.py` | Write all 8 audit checks |
| `backend/app/services/artifacts/repairs/repair_deck.py` | Write 4 repair functions |
| `backend/app/services/synexia/contracts.py` | Add `DeckPlan`, `SlidePlan` |
| `backend/app/services/artifacts/deck_planner.py` | NEW — content structuring |
| `backend/app/services/artifacts/deck_router.py` | NEW — smart routing |
| `backend/app/services/artifacts/copy_polish.py` | NEW — LLM copy polish |
| `backend/app/services/artifacts/data_profiler.py` | NEW — data profiling |
| `backend/app/services/artifacts/exporters/service.py` | Wire planner/router/polish into pipeline |
| `backend/app/services/agent_prompts.py` | PPT quality expectations + deck-type guidance |
| `backend/app/config.py` | 4 new feature flags |
| `backend/data/themes/*.json` | Update default fonts |

## 8. Risk Mitigation

| Risk | Mitigation |
|---|---|
| LLM planner adds latency (2-5s) | Flag-gated; default OFF; planner has 10s timeout |
| Sandbox path is slow (10-15s) | Only for design-heavy decks; structured path stays fast |
| Audit false positives | Repairs are conservative (shrink by 2pt max); WARN doesn't block |
| Font availability in Docker | Use web-safe fonts (Inter, Arial) as fallbacks; bundle font files |
| Backward compatibility | All flags default OFF; existing `create_artifact` path unchanged when flags are off |
