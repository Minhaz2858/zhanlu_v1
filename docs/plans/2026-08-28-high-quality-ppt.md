# High-Quality PPT Pipeline — Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Make the agent reliably produce professional, high-quality PPTs: default to the HTML design renderer (image-fill PPTX), block delivery of audit-failing decks, ground decks in market data when requested, and enforce artifact production.

**Architecture:** Three phases. A — flip `deck_router` default to sandbox/HTML-design, fix the container runtime (chromium+libreoffice exist in Dockerfile but not in the 43h-old running image), and make the audit gate blocking. B — bind the existing "Market Research Data" KB to the project (`KnowledgeBase.project_id` drives `_bound_kbs`), prefer market KB on market-intent, cite sources. C — enforce ppt-design skill loading (already mapped `pptx → ppt-design` in `skill_routing/resolver.py`) and harden `file_turn_guard`.

**Tech Stack:** Python 3.11 FastAPI, Docker, python-pptx, chromium headless (image-fill), pytest. Docker commands run via `docker exec zhanlu-backend`.

---

## Phase A — Visual quality pipeline

### Task A1: Rebuild backend image with chromium + libreoffice

**Objective:** Make `image_fill_available()` return True inside the container (currently False → HTML design renderer silently falls back to plain layout engine).

**Files:** none (infra)

**Step 1:** Verify current state:
Run: `docker exec zhanlu-backend sh -c "which chromium chromium-browser google-chrome soffice libreoffice || echo MISSING"`
Expected: MISSING (image is 43h old; Dockerfile already declares chromium+libreoffice at backend/docker/backend.Dockerfile lines 31, 69).

**Step 2:** Rebuild + restart:
```bash
docker compose build backend
docker compose up -d backend
# wait for health
docker exec zhanlu-backend sh -c "cd /app && python -c \"from app.services.artifacts.html_to_pptx import image_fill_available; print('image_fill_available:', image_fill_available())\""
```
Expected: `image_fill_available: True`

**Step 3:** Smoke-render one HTML deck:
Run: `docker exec zhanlu-backend sh -c "cd /app && python -c \"from app.services.artifacts.render_html_deck import html_design_available; print(html_design_available())\""`
Expected: True. If the rebuild is heavy/offline, fallback: `docker exec zhanlu-backend sh -c "apt-get install -y --no-install-recommends chromium libreoffice"` then re-verify.

**Step 4:** Commit (Dockerfile already declares deps — commit any Dockerfile change or the verification note):
```bash
git add -A backend/docker backend/requirements* 2>/dev/null; git commit -m "chore(ppt): rebuild backend image with chromium+libreoffice (unlocks HTML design renderer)" --allow-empty
```

---

### Task A2: Flip deck_router default → sandbox

**Objective:** All pptx requests default to the HTML design (sandbox) path; structured only for explicit plain/data-dump asks.

**Files:**
- Modify: `backend/app/services/artifacts/deck_router.py`
- Modify: `backend/app/config.py` (new flag `PPT_DESIGN_BY_DEFAULT: bool = True`)
- Modify: `backend/app/services/artifacts/exporters/service.py` (respect flag)
- Test: `backend/tests/services/artifacts/test_deck_router.py` (create if absent)

**Step 1: Write failing tests**

```python
# tests/services/artifacts/test_deck_router.py
import pytest
from app.services.artifacts.deck_router import route_deck


def test_defaults_to_sandbox_for_plain_request():
    assert route_deck(None, "make a market view ppt") == "sandbox"


def test_plain_data_dump_goes_structured():
    assert route_deck(None, "just a plain data dump of the numbers") == "structured"


def test_chinese_plain_request_goes_structured():
    assert route_deck(None, "简单纯文本数据表") == "structured"


def test_design_keywords_still_sandbox():
    assert route_deck(None, "beautiful investor deck") == "sandbox"
```

**Step 2:** Run to verify failure:
Run: `docker exec zhanlu-backend sh -c "cd /app && python -m pytest tests/services/artifacts/test_deck_router.py -v 2>&1 | tail -6"`
Expected: FAIL — `test_defaults_to_sandbox_for_plain_request` returns "structured".

**Step 3:** Implement — in `deck_router.py`:
- Add `_STRUCTURED_KEYWORDS = ("plain", "simple text", "data dump", "纯文本", "简单", "数据表", "text only")`
- Change `route_deck` tail: default → `"sandbox"`; structured only when a structured keyword is present AND no sandbox keyword/type. Respect `settings.PPT_DESIGN_BY_DEFAULT` — when False, preserve old default (structured).
- In `config.py` add `PPT_DESIGN_BY_DEFAULT: bool = True` next to `PPT_SMART_ROUTER_ENABLED`.
- In `exporters/service.py` `_render_deck_pipeline`: when `route == "sandbox"` the existing HTML-design branch already runs; keep structured as fallback on RenderError. Verify `route_deck` receives the flag (import settings in deck_router).

**Step 4:** Run to verify pass:
Run: same pytest command
Expected: PASS (4 passed)

**Step 5:** Commit:
```bash
git add backend/app/services/artifacts/deck_router.py backend/app/config.py backend/app/services/artifacts/exporters/service.py backend/tests/services/artifacts/test_deck_router.py
git commit -m "feat(ppt): default deck routing to HTML design renderer (PPT_DESIGN_BY_DEFAULT)"
```

---

### Task A3: Blocking audit gate in render_dispatcher

**Objective:** A deck that fails the quality audit after the repair loop must NOT be delivered — surface a structured failure instead.

**Files:**
- Modify: `backend/app/services/artifacts/render_dispatcher.py` (the audit loop ~lines 270-290 and return path)
- Modify: `backend/app/config.py` (`PPT_AUDIT_BLOCKING_ENABLED: bool = True`)
- Test: `backend/tests/services/artifacts/test_render_dispatcher_blocking.py` (create)

**Step 1: Write failing tests**

```python
# tests/services/artifacts/test_render_dispatcher_blocking.py
"""Blocking audit gate: FAIL after repairs → no bytes returned."""
import pytest
from unittest.mock import patch, MagicMock
from app.services.artifacts.render_dispatcher import render_pptx_from_plan_sync, _audit_enabled

FAIL_REPORT = {
    "status": "FAIL",
    "summary": {"pass": 0, "warn": 0, "fail": 2, "total": 2},
    "rules": [{"id": "density", "title": "Density overflow", "level": "FAIL", "detail": "slide 3 too dense", "evidence": []}],
}
PASS_REPORT = {"status": "PASS", "summary": {"pass": 2, "warn": 0, "fail": 0, "total": 2}, "rules": []}


def test_fail_after_repairs_raises_or_returns_fail(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "PPT_AUDIT_ENABLED", True)
    monkeypatch.setattr(settings, "PPT_AUDIT_BLOCKING_ENABLED", True)
    # force repair to produce identical bytes (nothing fixable)
    with patch("app.services.artifacts.render_dispatcher._repair_bytes", return_value=None):
        with patch("app.services.artifacts.render_dispatcher._audit_bytes", return_value=FAIL_REPORT):
            from app.services.synexia.contracts import DeckPlan
            plan = DeckPlan(title="t", slides=[])
            data, report = render_pptx_from_plan_sync(plan, [], {})
            assert data == b""
            assert report["status"] == "FAIL"


def test_pass_delivers_bytes(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "PPT_AUDIT_ENABLED", True)
    monkeypatch.setattr(settings, "PPT_AUDIT_BLOCKING_ENABLED", True)
    with patch("app.services.artifacts.render_dispatcher._render_once", return_value=b"PPTXDATA"):
        with patch("app.services.artifacts.render_dispatcher._audit_bytes", return_value=PASS_REPORT):
            from app.services.synexia.contracts import DeckPlan
            plan = DeckPlan(title="t", slides=[])
            data, report = render_pptx_from_plan_sync(plan, [], {})
            assert data == b"PPTXDATA"
            assert report["status"] == "PASS"


def test_blocking_off_keeps_old_behavior(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "PPT_AUDIT_ENABLED", True)
    monkeypatch.setattr(settings, "PPT_AUDIT_BLOCKING_ENABLED", False)
    with patch("app.services.artifacts.render_dispatcher._render_once", return_value=b"PPTXDATA"):
        with patch("app.services.artifacts.render_dispatcher._audit_bytes", return_value=FAIL_REPORT):
            from app.services.synexia.contracts import DeckPlan
            plan = DeckPlan(title="t", slides=[])
            data, report = render_pptx_from_plan_sync(plan, [], {})
            assert data == b"PPTXDATA"  # old behavior: ship anyway
            assert report["status"] == "FAIL"
```

**Step 2:** Run to verify failure:
Run: `docker exec zhanlu-backend sh -c "cd /app && python -m pytest tests/services/artifacts/test_render_dispatcher_blocking.py -v 2>&1 | tail -6"`
Expected: FAIL — `test_fail_after_repairs_raises_or_returns_fail` returns bytes.

**Step 3:** Implement — in `render_dispatcher.py` after the repair loop (current code logs FAIL and continues):
```python
if report.get("status") == "FAIL" and _blocking_enabled():
    logger.error(
        "render_dispatcher: blocking audit gate — deck FAIL after %d repair passes; refusing to deliver (%d fail rules)",
        passes, len([r for r in report.get("rules", []) if r.get("level") == "FAIL"]),
    )
    return b"", report
```
Add helper `_blocking_enabled()` mirroring `_audit_enabled()` reading `PPT_AUDIT_BLOCKING_ENABLED`. Note: `render_pptx_from_plan` (async) and `render_pptx_from_plan_sync` both funnel through this section — apply in the shared spot; if they diverge, apply in both.

**Step 4:** Run to verify pass:
Run: same pytest command
Expected: PASS (3 passed)

**Step 5:** Run regression (existing deck tests must stay green):
Run: `docker exec zhanlu-backend sh -c "cd /app && python -m pytest tests/services/artifacts/ -q --no-header 2>&1 | tail -3"`
Expected: pass (adjust any test that asserted FAIL decks get delivered — those now need `PPT_AUDIT_BLOCKING_ENABLED=False`).

**Step 6:** Commit:
```bash
git add backend/app/services/artifacts/render_dispatcher.py backend/app/config.py backend/tests/services/artifacts/test_render_dispatcher_blocking.py
git commit -m "feat(ppt): blocking audit gate — FAIL decks are not delivered (PPT_AUDIT_BLOCKING_ENABLED)"
```

---

### Task A4: Theme intent mapping (market → business/industry preset)

**Objective:** "market"/"行业"/"行情" resolve to a business/industry theme preset instead of generic.

**Files:**
- Modify: `backend/app/services/artifacts/themes.py` (`select_theme` keyword resolution)
- Test: extend `backend/tests/services/artifacts/test_themes.py` (or create)

**Step 1: Write failing test**

```python
def test_market_intent_selects_business_preset():
    from app.services.artifacts.themes import select_theme
    from app.services.synexia.contracts import DeckPlan
    plan = DeckPlan(title="C5/C9 Market View", slides=[])
    theme = select_theme(plan, "make a c5 c9 market view ppt using market data")
    assert theme.name in ("business", "industry")  # adjust to actual preset names
```

**Step 2:** Run to verify failure: `docker exec zhanlu-backend sh -c "cd /app && python -m pytest tests/services/artifacts/test_themes.py -v 2>&1 | tail -5"`
Expected: FAIL or the preset list needs checking — read `themes.py` preset names first and align the assertion.

**Step 3:** Implement — add "market"/"industry"/"市场"/"行业"/"行情" to the business/industry keyword group in `select_theme`.

**Step 4:** Run to verify pass; **Step 5:** Commit.

---

## Phase B — Market data grounding

### Task B1: Bind Market Research KB to C5_C9 project

**Objective:** The C5_C9 agent can reach market data. `_bound_kbs(db, project)` filters `KnowledgeBase.project_id == project.id`, so binding = set project_id on the market KB (or a scoped copy).

**Files:** none (data change) + verification test
- Test: `backend/tests/routers/test_project_catalog_kb_binding.py` (create)

**Step 1: Inspect current binding:**
Run: `docker exec zhanlu-backend sh -c "cd /app && python -c \"from app.database import SessionLocal; from sqlalchemy import text; db=SessionLocal(); print(db.execute(text(\\\"SELECT id,name,project_id FROM knowledge_bases\\\")).fetchall())\""`
Expected: Market Research Data (`e4032eda...`) has `project_id=be8cdaac` (Data Analysis), NOT C5_C9 (`07d8d339...`).

**Step 2 (decision):** Prefer a **project-scoped copy** (app isolation rule — never mutate another project's KB): insert a new knowledge_bases row copying name/description/type/source_kind with `project_id=07d8d339...`. Do NOT repoint the original. If the KB content is actually a shared vector store (ChromaDB), a copy shares the underlying store but scopes access by project_id — document that.

**Step 3:** Verify with a test that `_bound_kbs` returns the market KB for C5_C9 and not for Data Analysis (inverse check — original still bound there).

**Step 4:** Commit:
```bash
git add backend/tests/routers/test_project_catalog_kb_binding.py
git commit -m "feat(kb): bind Market Research KB to C5_C9 project (scoped copy)"
```

---

### Task B2: Market intent → market KB grounding

**Objective:** When the user asks for "market data", the deck rows come from the market KB, not the ERP warehouse.

**Files:**
- Modify: `backend/app/services/synexia/capability_router.py` (data context / KB selection ~lines 455-480 where `bound_kb_ids` is consumed)
- Test: `backend/tests/services/artifacts/test_market_grounding.py` (create)

**Step 1: Write failing test**

```python
def test_market_intent_prefers_market_kb():
    from app.services.synexia.capability_router import _select_grounding_kb  # or the real fn
    market = "market-kb-id"; erp = "erp-kb-id"
    chosen = _select_grounding_kb([erp, market], "c5 c9 market view market data")
    assert chosen == market
```

**Step 2:** Run to verify failure; **Step 3:** Implement keyword-based KB preference: on market/行业/行情 intent, prefer KB whose name/type contains market/research/行业; **Step 4:** pass; **Step 5:** Commit.

---

### Task B3: Citation footer audit rule

**Objective:** Every slide carries a source footer; audit_enforces it.

**Files:**
- Modify: `backend/app/services/artifacts/audits/audit_deck.py` (new rule `source_citation`)
- Modify: layout engine or HTML renderer to emit source footer from ctx
- Test: extend audit tests

**Step 1:** Read `audit_deck.py` rule structure (rule_id, title, level, detail) and the HTML slide template's footer slot. **Step 2:** Write failing test — a deck rendered without source context fails rule `source_citation`. **Step 3:** Implement — footer populated from `ExportContext.source_label` (planner sets it from the grounding KB name); audit rule checks each slide has non-empty source text (allow whitelist for pure-title slides). **Step 4:** Pass. **Step 5:** Commit.

---

## Phase C — Agent behavior enforcement

### Task C1: Verify ppt-design skill loading

**Objective:** pptx requests route to the ppt-design skill (already mapped in `skill_routing/resolver.py:34` — verify + test).

**Files:**
- Test: `backend/tests/services/skill_routing/test_resolver_pptx.py` (create)

**Step 1:** Write test asserting `DEFAULT_SKILL_MAP["pptx"] == "ppt-design"` and resolver returns it for a pptx format intent. **Step 2:** Fail (if absent) → **Step 3:** verify wiring (should already pass — if so, convert to a regression test and skip implementation) → **Step 4:** pass → **Step 5:** Commit.

### Task C2: file_turn_guard hardening

**Objective:** A turn that planned a deck but produced no artifact re-prompts instead of giving up.

**Files:**
- Modify: `backend/app/services/file_turn_guard.py`
- Test: extend its tests

**Step 1:** Read the guard; **Step 2:** write failing test — plan with pptx intent, no artifact → guard flags; **Step 3:** implement re-prompt (cap 1/turn, matching existing nudge patterns); **Step 4:** pass; **Step 5:** Commit.

---

## Task D: E2E verification — regenerate the C5_C9 market deck

**Objective:** Prove the pipeline produces a professional deck with real market data.

**Step 1:** Run a real agent turn (or the API driver from agent_qa) with "make a c5 c9 market view ppt don't use my data use market data". **Step 2:** Verify: PPTX has images/charts (bytes >> 60KB, slides have image fills), audit PASS, source footers present, market KB rows used (no "Product A/B"). **Step 3:** Visual QA via pptx skill thumbnail → inspect for overlap/overflow. **Step 4:** Fix any defects → re-render. **Step 5:** Commit any fixes.

---

## Verification checklist

- [ ] `image_fill_available()` True in container (A1)
- [ ] Plain "market view ppt" → sandbox route (A2)
- [ ] Explicit "plain data dump" → structured (A2)
- [ ] Audit-FAIL deck → no bytes delivered (A3); blocking off → old behavior (A3)
- [ ] Market intent → business theme (A4)
- [ ] Market KB bound to C5_C9, not leaked to others (B1)
- [ ] Market intent → market KB rows (B2)
- [ ] Source footers on every slide + audit rule (B3)
- [ ] pptx → ppt-design skill (C1)
- [ ] file_turn_guard re-prompts on missing artifact (C2)
- [ ] E2E deck: images, real data, citations, audit PASS (D)

## Notes

- A1 rebuild is the highest-risk task (image rebuild). If the registry mirror is slow, install chromium+libreoffice directly in the running container as a stopgap, then commit a Dockerfile note.
- B1 respects app isolation: scoped copy, never repoint another project's KB.
- Phase C is mostly verification — resolver already maps pptx → ppt-design (2026-08-23 fix); don't rebuild what exists.
