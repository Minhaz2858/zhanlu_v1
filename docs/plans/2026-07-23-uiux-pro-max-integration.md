# UI/UX Pro Max Integration — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Wire the dormant `ui-ux-pro-max` skill into the Zhanlu global agent so dashboards (and other UI artifacts) get professional design intelligence — color palettes, typography, chart types, UX guidelines — before HTML generation.

**Architecture:** 5-layer wrapper. (1) `manifest.yaml` makes the skill discoverable by `ManifestIndex`. (2) `SKILL.md` exposes methodology via `load_skill_body`. (3) Two new tool handlers (`uiux_search`, `uiux_design_system`) wrap the upstream Python CLI (`src/ui-ux-pro-max/scripts/search.py`). (4) Registered in `tool_registry.py`. (5) `build-dashboard` skill prompt + resolver hook guide the planner to call ui-ux-pro-max first.

**Tech Stack:** Python 3.11, FastAPI, subprocess (CLI invocation), existing `ToolRegistry`, existing `ManifestIndex`, BM25 + regex search engine (upstream).

---

## Task 1: Create `manifest.yaml` for ui-ux-pro-max

**Files:**
- Create: `backend/skills/ui-ux-pro-max/manifest.yaml`

**Step 1: Write the manifest**

```yaml
name: ui-ux-pro-max
version: 2.11.0
description: >
  AI-powered design intelligence with 192 color palettes, 84 UI styles, 74 font
  pairings, 25 chart types, and 98 UX guidelines across 22 tech stacks. Use
  this skill BEFORE building any visual artifact (dashboards, HTML pages, slide
  decks) so the output uses a vetted design system rather than ad-hoc CSS.
  Backed by a BM25 + regex search engine across 12 domains (style, color,
  chart, landing, product, ux, typography, google-fonts, icons, gsap, react,
  web). Companion to build-dashboard — call this first, then build.
author: NextLevelBuilder
license: MIT
category: ui-ux-pro-max
source: bundled
requires_sandbox: false
user_invocable: false
runtime: python
tags:
  - design
  - ui
  - ux
  - dashboard
  - styling
  - color-palette
  - typography
  - chart-types
artifact_types:
  - html
inputs:
  - name: query
    type: string
  - name: domain
    type: string
    optional: true
  - name: stack
    type: string
    optional: true
  - name: max_results
    type: integer
    optional: true
outputs:
  - name: results
    type: array
  - name: design_system
    type: object
    optional: true
```

**Step 2: Verify discovery**

Run:
```bash
cd /root/zhanlu/backend && python -c "
from app.services.skills_loader.manifest_index import get_manifest_index
idx = get_manifest_index()
idx.ensure_loaded()
m = idx.get('ui-ux-pro-max')
print('FOUND:' if m else 'MISSING:', m.name if m else None)
"
```
Expected: `FOUND: ui-ux-pro-max`

---

## Task 2: Create `SKILL.md` wrapper

**Files:**
- Create: `backend/skills/ui-ux-pro-max/SKILL.md`

**Step 1: Write the SKILL.md**

A 120-line methodology file that documents:
- When to call (before any visual artifact)
- The 12 domains + their purposes
- The `--design-system` mode
- The 22 supported stacks (html-tailwind is default)
- The 3 design dials (variance, motion, density)
- The output format (markdown token-optimized)
- Token-saving tips (use `--max-results 3`)

---

## Task 3: Build tool handlers

**Files:**
- Create: `backend/app/services/tool_handlers/ui_ux_pro_max_tool.py`
- Test: `backend/tests/test_ui_ux_pro_max_tool.py`

Two tools:
1. `uiux_search(query, domain?, stack?, max_results?, page?)` — calls `python3 src/ui-ux-pro-max/scripts/search.py "<query>" [--domain X] [--stack X] [-n N]`
2. `uiux_design_system(query, project?, variance?, motion?, density?)` — calls with `--design-system` flag

Both wrap `subprocess.run()` with:
- 30s timeout
- Capture stdout as the markdown result
- On error return `{"success": False, "error": str, "fallback": ""}` — never raise

---

## Task 4: Register tools

**Files:**
- Modify: `backend/app/services/tool_handlers/__init__.py` (or wherever handlers auto-import)

Add import + register calls in the appropriate `__init__.py` so the registry picks them up at startup. Look at how `load_skill_body_tool.py` is wired.

---

## Task 5: Hook into dashboard flow

**Files:**
- Modify: `backend/app/services/synexia/agent_prompts.py` (or `default_skills.py`) — add ui-ux-pro-max as a "companion skill" mentioned in build-dashboard's prompt context

When user says "dashboard", planner now sees both `build-dashboard` and `ui-ux-pro-max` in catalog, and the build-dashboard prompt tells it: "Before generating HTML, call `uiux_design_system(query="<topic>", stack="html-tailwind")` for color palette + typography, then `uiux_search(query="<chart keyword>", domain="chart")` for chart recommendations."

---

## Task 6: Tests

Three test files. All using `unittest.mock.patch` to mock `subprocess.run`:

1. `test_ui_ux_pro_max_tool.py`:
   - `test_search_calls_cli_with_correct_args`
   - `test_search_handles_cli_failure`
   - `test_design_system_calls_with_flag`
   - `test_timeout_returns_fallback`

2. `test_ui_ux_pro_max_manifest.py`:
   - `test_manifest_index_discovers_uiux`
   - `test_skills_registry_loads_skill_md_body`

3. `test_dashboard_uiux_hook.py`:
   - `test_dashboard_routes_to_build_dashboard`
   - `test_build_dashboard_prompt_mentions_uiux_companion` (string match)

---

## Task 7: Run full suite

```bash
cd /root/zhanlu/backend && python -m pytest tests/test_ui_ux_pro_max_tool.py tests/test_ui_ux_pro_max_manifest.py tests/test_dashboard_uiux_hook.py -v
cd /root/zhanlu/backend && python -m pytest --tb=short -q 2>&1 | tail -20
```

Expected: All new tests pass. Existing tests unchanged (158 prior still green).

---

## Task 8: Verification report

Write to `docs/plans/2026-07-23-uiux-pro-max-integration-verification.md` summarizing:
- Files created/modified
- Test results
- Before/after of dashboard quality expectation
- Deployment note (git commit + pull + restart)