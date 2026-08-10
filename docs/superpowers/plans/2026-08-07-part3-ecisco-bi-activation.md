# Part 3 — Ecisco BI Module Activation — 2026-08-07

> **Status:** Active (2026-08-07). Six modules dormant. One big plan.
>
> **Approach:** Sequential flag activation (A). Each phase = one flag, one docker restart, one smoke test. Dependency chain is non-negotiable.

---

## Context

The 5-EDIA-module migration (plan `2026-08-03-ecisco-bi-migration.md`) shipped all six modules under `app/services/{knowledge_graph, pricing, decision, perception, weekly_report, erp}` — but **every feature flag is OFF**. Code is complete; data stubs are the blocker.

**Three integration bugs discovered during scoping (2026-08-07):**

1. `perception/service.py:143` writes `from app.services.market_dashboard.service` but actual file is `market_dashboard_service.py` (no subdirectory) — silently swallowed by try/except.
2. `weekly_report/data.py:65` has the same broken import — silently swallowed.
3. `intelligence/event_registry.py` `IntelligenceEventRegistry.list_events_as_of()` exists but `perception._query_recent_intelligence_events()` is a `return []` stub.

**Plus:** Part 2 plan `2026-08-06-part2-gap-analysis_639e9291` ships 6 new modules but writes only 2 of 10 required regression tests — the rest are still missing.

**Goal:** Close all three integration bugs, finish Part 2 regression tests, then activate all six Ecisco BI flags in dependency order with end-to-end validation. End state = Ecisco BI a full EDIA-equivalent (L1 ↔ L6 capability parity).

---

## Scope

### In scope

- **Phase 0** — Part 2 regression tests (8 missing files)
- **Phase 1** — 5A Knowledge Graph (`KNOWLEDGE_GRAPH_ENABLED`)
- **Phase 2** — 5C Perception (`PERCEPTION_ENABLED`) — wires 2 stubs
- **Phase 3** — 5B Pricing + Decision (`PRICING_ENABLED`, `DECISION_ENABLED`)
- **Phase 4** — 5D Intelligence (`INTEL_ENHANCED_ENABLED`)
- **Phase 5** — 5E Weekly Report (`WEEKLY_REPORT_ENABLED`) — fix import bug
- **Phase 6** — 5F ERP (`ERP_KPI_ENABLED`, `ERP_WRITEBACK_ENABLED`) — wire persistence

### Out of scope

- Stream A forecasting activation (separate plan `2026-08-06-forecasting-activation-and-decision-loop.md`)
- Decision loop frontend (Stream B of same plan)
- Tier 3 data ceiling (Stream C)
- New SQLAlchemy models (reuse existing schemas)

---

## Module State Matrix (Pre-Phase)

| Module | Flag (default) | Code | Data Stub | Activation Blocker |
|---|---|---|---|---|
| 5A Knowledge Graph | `KNOWLEDGE_GRAPH_ENABLED=False` | Complete | None (static C5/C9 graph) | Flag only |
| 5B Pricing + Decision | `PRICING_ENABLED=False` | Complete | Falls back to mock when MySQL absent | Depends on 5C output |
| 5C Perception | `PERCEPTION_ENABLED=False` | Complete | 3 stubs: `_get_price_history`, `_query_recent_intelligence_events`, broken import | Wire 2 stubs + 1 import fix |
| 5D Intelligence | `INTEL_ENHANCED_ENABLED=False` | Complete (17 files) | None explicit | Depends on 5C |
| 5E Weekly Report | `WEEKLY_REPORT_ENABLED=False` | Complete | `data.py:65` import bug; `charts.py` is stub (no matplotlib) | Fix import bug, optional matplotlib |
| 5F ERP | `ERP_KPI_ENABLED=False`, `ERP_WRITEBACK_ENABLED=False` | Complete | `writeback.py` in-memory only; `get_inventory_trend()` stub | Wire PG persistence + KB bind |

---

## Dependency Chain

```
Phase 0 → Phase 1 → Phase 2 → Phase 3 (depends on 2)
                           ↘ Phase 4 (depends on 2)
                → Phase 5 (independent of 5A-F chain, only depends on 0)
                → Phase 6 (depends on PG persistence config)
```

Note: Phases 3, 4, 5, 6 can run **independently** after Phase 2 passes. We sequence them anyway for safer rollout + validation isolation.

---

## Phase 0 — Part 2 Regression Tests (8 files)

**Goal:** Close the loop on Part 2 gap analysis. No business logic changes — just tests for already-shipped modules.

### Tests to write (`backend/tests/`)

| # | File | Target module | Min tests |
|---|---|---|---|
| 1 | `test_provider_health.py` | `app/services/llm/provider_health.py` | 3: health score decay, recovery bump, threshold emit |
| 2 | `test_model_router.py` | `app/services/llm/model_router.py` | 4: cheap-model default, escalation on health drop, all-providers-down fallback, budget cap |
| 3 | `test_llm_cache.py` | `app/services/llm/cache.py` | 3: hit, miss, TTL expiry |
| 4 | `test_citation_grounding.py` | `app/services/rag/citation_grounding.py` | 4: all-supported citation, partial-support warning, no-support reject, mixed-source scoring |
| 5 | `test_rate_limiter.py` | `app/services/concurrency/rate_limiter.py` | 3: under-limit allow, over-limit reject with backoff, refill window |
| 6 | `test_task_queue.py` | `app/services/concurrency/task_queue.py` | 3: enqueue/dequeue, persistence on restart, retry with backoff |
| 7 | `test_retrieval_hybrid.py` | `app/services/rag/retrieval_hybrid.py` | 4: BM25-only, vector-only, RRF fusion, empty-query graceful |
| 8 | `test_intent_planner.py` | `app/services/nlu/intent_planner.py` | 4: classify confidence, plan-success, plan-conflict, low-confidence fallback |

**Conventions** (match existing tests in `backend/tests/`):
- SQLite in-memory via `Base.metadata.create_all`
- Use `SessionLocal` factory, monkeypatch in `conftest.py` fixtures
- Mark slow tests with `@pytest.mark.slow` and skip in `-m 'not slow'`
- No parallel workers (RAM limit per `MEMORY.md`)

### Validation

```bash
cd backend
PYTHONPATH=. venv/bin/pytest tests/test_provider_health.py tests/test_model_router.py tests/test_llm_cache.py tests/test_citation_grounding.py tests/test_rate_limiter.py tests/test_task_queue.py tests/test_retrieval_hybrid.py tests/test_intent_planner.py -v
# Expected: ≥24 tests pass, 0 errors, 0 collection failures
```

### Rollback

N/A — pure test additions, no prod code touched.

---

## Phase 1 — Knowledge Graph (5A)

**Goal:** First flag on. No business logic changes — just turn the key.

### Files touched

- `backend/.env` (or `docker-compose.yml` env block)

### Steps

1. Read current value:
   ```bash
   grep KNOWLEDGE_GRAPH_ENABLED backend/.env docker-compose.yml
   ```
2. Set `KNOWLEDGE_GRAPH_ENABLED=True` in `.env` (commit-safe: keep default `False` in `config.py`)
3. Restart: `docker compose up -d --force-recreate backend`
4. Health check:
   ```bash
   curl -s http://localhost:5152/api/health | jq '.modules.knowledge_graph'
   # Expected: {"enabled": true, "status": "ok", "nodes": 47, "edges": 124}
   ```
5. Smoke test:
   ```bash
   TOKEN=$(curl -s -X POST http://localhost:5152/api/apps/default-app/auth/login \
     -H 'Content-Type: application/json' \
     -d '{"email":"admin@zhanlu.dev","password":"admin123"}' | jq -r .access_token)
   curl -s -H "Authorization: Bearer $TOKEN" \
     "http://localhost:5152/api/apps/default-app/knowledge/graph/query?product=C5+sensor" | jq '.entities | length'
   # Expected: ≥ 5 entities
   ```

### Validation matrix

| Check | Expected | Failure response |
|---|---|---|
| `/api/health` reports kg.enabled=true | yes | revert flag, log to `flag-validation-log.md` |
| Graph query returns C5 entities | ≥ 5 | check `build_c5_c9_graph()` not raising at import |
| `ask_perception` tool call works in agent chat | Yes (still no enrichment — that's Phase 2) | n/a this phase |

### Rollback

```bash
sed -i 's/KNOWLEDGE_GRAPH_ENABLED=True/KNOWLEDGE_GRAPH_ENABLED=False/' backend/.env
docker compose up -d --force-recreate backend
# Verify health shows kg.enabled=false
```

---

## Phase 2 — Perception (5C, wires 2 stubs + fixes 1 import)

**Goal:** Make perception consume real prices + real intelligence events instead of returning empty stubs.

### Files to modify

- `backend/app/services/perception/service.py` — three fixes
- Possibly `backend/app/services/market_dashboard_service.py` — add helper if missing

### Change 1: Fix broken import (`service.py:143`)

```python
# BEFORE (broken — silent failure)
from app.services.market_dashboard.service import MarketDashboardService

# AFTER
from app.services.market_dashboard_service import MarketDashboardService
```

Same fix in `weekly_report/data.py:65`.

### Change 2: Wire `_get_price_history()`

**Current:** `service.py:174-177`
```python
def _get_price_history(self, product_id: str) -> list[dict]:
    """Stub — returns empty."""
    return []
```

**New:**
```python
def _get_price_history(self, product_id: str, days: int = 90) -> list[dict]:
    """Get N-day price history for anomaly detection."""
    if not self.db:
        return []
    try:
        from app.services.market_dashboard_service import MarketDashboardService
        svc = MarketDashboardService(self.db, org_id=self.org_id)
        return svc.get_price_history_for_sku(product_id, days=days) or []
    except Exception as exc:
        logger.debug("Could not get price history for %s: %s", product_id, exc)
        return []
```

**Note:** First verify `MarketDashboardService.get_price_history_for_sku(sku, days)` exists. If not, add a small adapter using the existing price-table reads (already in `market_dashboard_service.py`).

### Change 3: Wire `_query_recent_intelligence_events()`

**Current:** `service.py:224-226`
```python
def _query_recent_intelligence_events(self, hours: int = 4) -> list[dict]:
    """Stub — return empty until intelligence layer is fully wired."""
    return []
```

**New:**
```python
def _query_recent_intelligence_events(self, hours: int = 4) -> list[dict]:
    """Pull recent intel events from IntelligenceEventRegistry."""
    try:
        from app.services.intelligence.event_registry import IntelligenceEventRegistry
        from datetime import datetime, timezone, timedelta
        registry = IntelligenceEventRegistry(org_id=self.org_id)
        as_of = datetime.now(timezone.utc)
        events = registry.list_events_as_of(as_of=as_of, lookback_hours=hours)
        return [e.to_dict() for e in (events or []) if hasattr(e, "to_dict")]
    except Exception as exc:
        logger.debug("Could not query intel events: %s", exc)
        return []
```

**Note:** First verify `IntelligenceEventRegistry.list_events_as_of()` accepts `lookback_hours` kwarg. If not, use the closest method and adapt.

### Steps

1. Verify both target methods exist via `grep`:
   ```bash
   grep -n "get_price_history_for_sku\|list_events_as_of" backend/app/services/
   ```
2. Apply the three fixes above
3. Run unit test (write a small inline test):
   ```bash
   PYTHONPATH=backend venv/bin/python -c "
   from app.services.perception.service import PerceptionService
   svc = PerceptionService.__new__(PerceptionService)
   hist = svc._get_price_history('C5')
   intel = svc._query_recent_intelligence_events(hours=24)
   print('price_history rows:', len(hist), 'intel events:', len(intel))
   "
   # Expected: price_history > 0 AND intel_events > 0 (with seed data)
   ```
4. Set `PERCEPTION_ENABLED=True`
5. Restart backend
6. End-to-end test via agent:
   ```bash
   TOKEN=$(...)
   curl -s -H "Authorization: Bearer $TOKEN" -X POST \
     http://localhost:5152/api/apps/default-app/agents/general_agent/chat \
     -d '{"message":"价格感知一下当前的C5传感器", "stream":false}' | jq '.tool_calls[0].output.price_history | length'
   # Expected: ≥ 30 days of prices
   ```

### Validation matrix

| Check | Expected |
|---|---|
| `service.py` import resolves | `python -c "import app.services.perception.service"` returns 0 |
| `_get_price_history("C5")` returns non-empty with seed data | `≥ 30` rows |
| `_query_recent_intelligence_events(hours=24)` returns ≥ 1 event | yes |
| `ask_perception` tool in agent chat | returns real prices + intel events |

### Rollback

```bash
git checkout backend/app/services/perception/service.py
sed -i 's/PERCEPTION_ENABLED=True/PERCEPTION_ENABLED=False/' backend/.env
docker compose up -d --force-recreate backend
```

---

## Phase 3 — Pricing + Decision (5B)

**Goal:** Enable the 6-stage decision pipeline (`perception → resolve_product → diagnosis → forecast → pricing → decision → action`).

### Flags

- `PRICING_ENABLED=True`
- `DECISION_ENABLED=True`

### Pre-flight

Verify `pricing_service` consumes perception output:
```bash
grep -rn "PerceptionService\|perception_service\|build_snapshot" \
  backend/app/services/pricing backend/app/services/decision
```
If direct calls exist → good. If not, check if perception snapshot is passed in via the orchestration layer (`edia_delegation_tools.py`).

### Steps

1. Set both flags
2. Restart
3. Test via `ask_pricing` and `ask_decision` agent tools
4. Verify decision log table populates

### Validation matrix

| Check | Expected |
|---|---|
| `ask_pricing(product="C5")` returns computed price | Yes, with confidence score |
| `ask_decision(product="C5")` returns decide/hold/abort | Yes, with rationale string |
| Decision audit row written | 1 new row in `decision_audit` table |

### Rollback

```bash
sed -i 's/PRICING_ENABLED=True/PRICING_ENABLED=False/' backend/.env
sed -i 's/DECISION_ENABLED=True/DECISION_ENABLED=False/' backend/.env
docker compose up -d --force-recreate backend
```

---

## Phase 4 — Intelligence Enhancements (5D)

**Goal:** Enable learning loop + narrative generator + product intelligence.

### Flag

- `INTEL_ENHANCED_ENABLED=True`

### Pre-flight

Verify all 17 files in `app/services/intelligence/` import without errors:
```bash
PYTHONPATH=backend venv/bin/python -c "
import app.services.intelligence
import app.services.intelligence.learning_loop
import app.services.intelligence.narrative_generator
import app.services.intelligence.product_intelligence
import app.services.intelligence.event_registry
print('All intelligence modules import OK')
"
```

### Steps

1. Set flag
2. Restart
3. Test `ask_intelligence` tool — verify enriched event presentation
4. Verify learning_loop captures feedback (call once, hit DB)

### Validation matrix

| Check | Expected |
|---|---|
| All intelligence modules import | 0 errors |
| `ask_intelligence(topic="C5 supply")` returns enriched events | Yes, with rationale |
| `learning_loop.record_feedback()` writes row | 1 row in `intelligence_feedback` |

### Rollback

```bash
sed -i 's/INTEL_ENHANCED_ENABLED=True/INTEL_ENHANCED_ENABLED=False/' backend/.env
docker compose up -d --force-recreate backend
```

---

## Phase 5 — Weekly Report (5E)

**Goal:** Generate real weekly reports with real numbers.

### Files to modify

- `backend/app/services/weekly_report/data.py` — fix import (same path bug as Phase 2)
- `backend/app/services/weekly_report/charts.py` — optional, replace stub with matplotlib output (or keep stub, document as `WEEKLY_REPORT_INLINE_CHARTS=False`)

### Flag

- `WEEKLY_REPORT_ENABLED=True`
- (Optional) `WEEKLY_REPORT_INLINE_CHARTS=False`

### Change 1: Fix import (`data.py:65`)

```python
# BEFORE
from app.services.market_dashboard.service import MarketDashboardService

# AFTER
from app.services.market_dashboard_service import MarketDashboardService
```

**Bonus:** Remove the silent `except Exception: pass` and log a `logger.warning("Market data unavailable for week %s", ...)` — silent failure masks real outages.

### Change 2: Optional — Render charts

If matplotlib is available in `backend/venv`:
```bash
PYTHONPATH=backend venv/bin/python -c "import matplotlib; print(matplotlib.__version__)"
# If prints version → enable inline charts (set WEEKLY_REPORT_INLINE_CHARTS=True)
# Else → keep stub, document behavior
```

### Steps

1. Apply import fix
2. Optionally regenerate charts.py
3. Set `WEEKLY_REPORT_ENABLED=True`
4. Restart
5. Test:
   ```bash
   TOKEN=$(...)
   curl -s -H "Authorization: Bearer $TOKEN" -X POST \
     http://localhost:5152/api/apps/default-app/agents/general_agent/chat \
     -d '{"message":"生成本周C5传感器的周报", "stream":false}' | jq '.tool_calls[0].output | {week, products: (.products | length), has_charts}'
   ```

### Validation matrix

| Check | Expected |
|---|---|
| `data.py` import resolves | 0 errors |
| `collect_all_numbers("C5", db)` returns non-empty | current_price, change_7d_pct, forecast_band all populated |
| `ask_weekly_report(week=...)` returns report | Yes, with markdown body + per-product numbers |
| Chart rendering | Either inline (matplotlib) OR explicit stub with placeholder + log |

### Rollback

```bash
git checkout backend/app/services/weekly_report/data.py
sed -i 's/WEEKLY_REPORT_ENABLED=True/WEEKLY_REPORT_ENABLED=False/' backend/.env
docker compose up -d --force-recreate backend
```

---

## Phase 6 — ERP (5F)

**Goal:** Persist ERP writebacks; bind MySQL knowledge base.

### Flags

- `ERP_KPI_ENABLED=True`
- `ERP_WRITEBACK_ENABLED=True`

### Files to modify

- `backend/app/services/erp/writeback.py` — wire to PostgreSQL via SQLAlchemy
- `backend/app/services/erp/kpi.py` — verify `connector_factory` usage + bind correct KB
- Possibly add a new model `EciscoWritebackLog` or reuse existing audit table

### Change 1: Persist writeback (`writeback.py`)

**Current:** in-memory `_pending: list[dict] = []`
**New:** SQLAlchemy insert into `erp_writeback_log` table (create new model or reuse existing audit table).

```python
from app.models.base import SessionLocal
from app.models.erp import ErpWritebackLog  # TODO: confirm model name

def record_writeback(action: str, payload: dict) -> str:
    with SessionLocal() as session:
        log = ErpWritebackLog(
            action=action,
            payload=payload,
            status="pending",
            created_at=datetime.now(timezone.utc),
        )
        session.add(log)
        session.commit()
        session.refresh(log)
        return str(log.id)
```

### Change 2: Wire `get_inventory_trend()`

Replace stub with:
```python
def get_inventory_trend(sku: str, days: int = 30) -> list[dict]:
    # Use connector_factory with EDIA_MYSQL_* env
    conn = get_connector()  # connector_factory
    return conn.query(f"""
        SELECT snapshot_date, on_hand_qty
        FROM edia_erp_inventory_snapshot
        WHERE sku = %s AND snapshot_date >= %s
        ORDER BY snapshot_date
    """, (sku, days_ago(days)))
```

### Pre-flight

Verify env vars exist for EDIA MySQL binding:
```bash
grep -E "EDIA_MYSQL_(HOST|PORT|USER|PASSWORD|DB)" backend/.env
```
If missing → ask user for the connection string **before** activating flag.

### Steps

1. Confirm env vars (or set placeholders)
2. Apply writeback persistence
3. Apply inventory trend query
4. Set `ERP_KPI_ENABLED=True`
5. Set `ERP_WRITEBACK_ENABLED=True`
6. Restart
7. Test `ask_erp_kpi` and `ask_erp_writeback`

### Validation matrix

| Check | Expected |
|---|---|
| `record_writeback` inserts row | 1 new row in `erp_writeback_log` |
| `get_inventory_trend("C5", 30)` returns data | ≥ 4 weekly snapshots (assuming weekly grain) |
| `ask_erp_kpi(metric="inventory_turnover")` | Returns numeric KPI |
| `ask_erp_writeback(action="adjust_price", product="C5")` | Returns writeback_id + status="pending" or "applied" |

### Rollback

```bash
git checkout backend/app/services/erp/
sed -i 's/ERP_KPI_ENABLED=True/ERP_KPI_ENABLED=False/' backend/.env
sed -i 's/ERP_WRITEBACK_ENABLED=True/ERP_WRITEBACK_ENABLED=False/' backend/.env
docker compose up -d --force-recreate backend
```

---

## Risk Register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Market dashboard missing price history method | Medium | Phase 2 blocked | Phase 2 pre-flight verifies; fallback to direct DB read |
| Intelligence event registry API mismatch | Low | Phase 2 blocked | Pre-flight grep; fallback to `list_events()` adapter |
| Memory ceiling during Part 2 tests | Low | Phases 0–1 delayed | Run per-file, no `-n` parallel (per MEMORY.md) |
| EDIA MySQL env vars not configured | Medium | Phase 6 blocked | Pre-flight check; ask user before activating flag |
| Flag rollback leaves zombie scheduled jobs | Low | Mid | Each phase pre-flight: `crontab -l` (in container) check |
| Hidden Phase 2+3 dependency (e.g., pricing needs forecast data) | Medium | Phase 3 partial | Pre-flight grep before each phase; expected MAPE validation |
| Charts.py stub produces ugly PDF in production | Low | Cosmetic | Optional matplotlib upgrade; explicit stub OK |

---

## Validation Strategy

### Per-phase gate

Each phase ends with a smoke test script:

```bash
backend/tests/smoke_phase_N.sh   # added per phase
```

These scripts:
1. Verify flag flip took effect (curl `/api/health`)
2. Hit the new capability via `ask_*` agent tool or REST endpoint
3. Assert on response shape
4. Exit 0 on pass, non-zero on fail

### End-of-plan gate

After Phase 6 succeeds, run an integrated scenario:

```bash
# Single-shot end-to-end across all six modules:
TOKEN=...
curl -s -H "Authorization: Bearer $TOKEN" -X POST \
  http://localhost:5152/api/apps/default-app/agents/general_agent/chat \
  -d '{"message":"对C5传感器跑完整决策：感知→知识图谱→情报→预测→定价→决策→ERP写回", "stream":false}' \
  | jq '.tool_calls[] | {tool: .name, status: .status, summary: (.output.summary // .output)}'
```

Expected: every tool in the chain returns a populated output (not the empty stubs).

---

## Exit Criteria

- [ ] Phase 0: ≥ 24 of 24 new Part 2 regression tests pass
- [ ] Phase 1: `KNOWLEDGE_GRAPH_ENABLED=True`, C5 query returns ≥ 5 entities
- [ ] Phase 2: `PERCEPTION_ENABLED=True`, `_get_price_history` returns ≥ 30 days, intel events ≥ 1
- [ ] Phase 3: `ask_pricing` + `ask_decision` work; audit row written
- [ ] Phase 4: `ask_intelligence` returns enriched events; feedback loop writes
- [ ] Phase 5: `WEEKLY_REPORT_ENABLED=True`, import bug fixed, week report renders numbers
- [ ] Phase 6: ERP writeback persists to PG; inventory trend returns real data
- [ ] End-to-end scenario: all six tool calls return populated outputs

## Related

- Plan: `docs/superpowers/plans/2026-08-03-ecisco-bi-migration.md` (code port)
- Plan: `docs/superpowers/plans/2026-08-06-forecasting-activation-and-decision-loop.md` (Stream A/B/C, parallel work)
- Plan: `.codebuddy/plans/2026-08-06-part2-gap-analysis_639e9291.md` (Phase 0 source)
- Memory: `.codebuddy/memory/MEMORY.md` (dev server, ECISCO_BI flag table, EDIA architectural references)
