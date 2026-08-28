# High-Quality PPT Pipeline — Design

Date: 2026-08-28
Status: Approved (user: "ok do it please")
Author: Synexia Agent

## Problem

User asked: "make a c5 c9 market view ppt don't use my data use market data".
The delivered `real_c5c9.pptx` (9 slides, 59KB) was **very low quality**:
text bullets only, 0 images, 0 charts, density overflow (slide 3, 42 words),
generic "Product A/B" filler instead of real market data.

Question to solve: what does the agent need so it reliably produces
**professional, high-quality PPTs**?

## Root causes (evidence from code + real artifact)

1. **Runtime missing — THE smoking gun.** `backend/docker/backend.Dockerfile`
   installs `chromium` + `libreoffice` (lines 31, 69), but the RUNNING
   container (`zhanlu-backend`, image 43h old) has neither binary:
   `image_fill_available()` → False. The HTML design renderer
   (`render_html_deck` → `render_image_fill`, Firefox/Chromium headless
   screenshots → PPTX image-fill) therefore **silently falls back** to the
   structured layout engine (`ExportService._render_deck_pipeline` logs
   "browser missing — using layout engine fallback"). The high-quality
   machinery exists in code but can't run.
2. **Router default = plain.** `deck_router.route_deck` returns `"structured"`
   unless the request contains design keywords (investor/marketing/beautiful/
   pitch/精美 etc). "make a c5 c9 market view ppt" has none → structured path.
3. **Audit is non-blocking.** `render_dispatcher` runs `audit_deck`, repairs
   up to MAX_REPAIR_PASSES, but **delivers the deck even when status stays
   FAIL** — it only logs warnings. The density-FAIL slide 3 shipped.
4. **No market data bound to C5_C9.** C5_C9 project binds only
   `aipdp_data_warehouse_prod` (company ERP MySQL). The "Market Research Data"
   KB (128 items, vector_db) lives in the "Data Analysis" project
   (`be8cdaac`). The agent had exactly one datasource — the ERP warehouse —
   so "don't use my data" was impossible to honor. It burned 26
   `ask_data_agent` calls against the wrong DB.
5. **Structured renderer has no charts/images.** Even with rows, the layout
   engine emits text+numbers only (0 images on all 9 slides).

## Design — three phases, one pipeline

### Phase A — Visual quality pipeline (default → designed, blocking gate)

A1. **Fix the runtime.** Rebuild backend image so chromium + libreoffice are
    actually present (Dockerfile already declares them; rebuild + verify
    `image_fill_available()` True in the container). This unlocks the HTML
    design renderer that produces image-fill PPTX (16:9, full design
    fidelity, native charts).

A2. **Flip the router default.** `deck_router.route_deck`: default → `"sandbox"`
    (HTML design path) for ALL pptx requests; `"structured"` only when the
    user explicitly asks for a plain/data-dump deck (keywords: plain, simple
    text, data dump, 简单, 纯文本, 数据表). Add flag
    `PPT_DESIGN_BY_DEFAULT` (default True when HTML_DESIGN_RENDERER_ENABLED).

A3. **Blocking audit gate.** In `render_dispatcher`: when the deck audit
    status is FAIL after the repair loop → do NOT return the bytes; raise /
    return a structured failure so the artifact service surfaces an error
    ("deck failed quality audit: <rules>") instead of shipping a bad deck.
    One regeneration attempt (re-plan → re-render) before failing. Flag
    `PPT_AUDIT_BLOCKING_ENABLED` (default True).

A4. **Theme intent mapping.** Ensure `select_theme` (12-preset catalog +
    keyword resolution) receives the user message; "market"/"行业" →
    business/industry preset. (Verify existing wiring; minor.)

### Phase B — Market data grounding (content quality)

B1. **Bind market data to the project.** The `knowledge_bases` table has a
    `project_id` column. Bind the "Market Research Data" KB (or a
    project-scoped copy) to the C5_C9 project so `ask_data_agent` / KB
    retrieval can reach it. App isolation: do NOT leak KBs across projects —
    verify the agent's KB-scoping honors project_id.

B2. **Intent → datasource grounding.** In the deck planner / data grounding
    step (`PPT_DECK_DATA_GROUNDING_ENABLED` already True): when the user
    intent contains market/industry keywords (market, industry, market data,
    市场, 行业, 行情), prefer the project's market KB over the ERP warehouse
    as the row/retrieval source.

B3. **Citations.** Every slide footer cites the source (KB/file name +
    basis), matching the exec-deliverable-builder provenance principle.
    No invented "Product A/B" — data must come from the bound source.

### Phase C — Agent behavior enforcement (reliability)

C1. **Skill loading.** For pptx requests, the agent loads the bundled
    ppt-design skill (`backend/skills/ppt_skills/Powerpoint-fancy-design-main`
    — style selector, 10 styles, layout rules, audit rubric) as the sandbox
    driver, plus `pptx` skill for reading/editing. Verify the skill registry
    exposes it; wire a prompt hint if needed.

C2. **file_turn_guard hardening.** If a turn planned a deck but produced no
    artifact, the guard re-prompts (create_artifact) instead of letting the
    turn end with "trouble putting it together". (Extends existing
    file_turn_guard.)

## Architecture

```
user "make c5 c9 market view ppt (market data)"
  → deck_planner (profile=data_report, theme resolved, rows grounded)
  → deck_router  (default sandbox after A2)
  → sandbox: render_html_deck (HTML slides → chromium → PNG → PPTX image-fill)
      └─ falls back to structured ONLY on RenderError (not on missing browser
         after A1, because the browser exists)
  → audit_deck (BLOCKING after A3 — FAIL stops delivery)
  → artifact stored + preview
data: ask_data_agent → project KBs (ERP + Market KB after B1), market KB
      preferred on market intent (B2), sources cited (B3)
```

## Testing

- A1: container check `image_fill_available()` → True (e2e smoke).
- A2: router unit tests — default → sandbox; "plain data dump" → structured.
- A3: dispatcher test — audit FAIL after repairs → raises, no bytes returned;
  audit PASS → bytes returned. Regression: repair loop still runs.
- B1: KB binding test — market KB visible to C5_C9 agent, not other projects.
- B2: grounding test — market intent picks market KB rows.
- B3: audit rule — every slide has a source footer (extend audit_deck rules
  or add citation check).
- C1: skill registry test — ppt-design loadable.
- E2E: regenerate the C5_C9 market deck → PPTX has images/charts, real market
  data with citations, audit PASS.

## Scope / non-goals

- NOT building a new renderer — the HTML design engine exists; this makes it
  runnable, default, and gated.
- NOT editable-text PPTX (image-fill is static; v1.1 editable path is
  reserved, out of scope).
- NOT rebuilding the market KB content (128 items exist; verifying its
  C5/C9 relevance is a task, not a build).
- Frontend unchanged (artifact preview already handles PPTX).
