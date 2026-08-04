# EDIA Intelligence Layer Integration Plan (Phase 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port EDIA's 19-module intelligence pipeline into zhanlu's `backend/app/services/intelligence/` package, adapting raw MySQL/SQLite SQL to async SQLAlchemy, raw threading to asyncio scheduled tasks, and integrating with zhanlu's existing agent framework.

**Architecture:** EDIA's intelligence pipeline is a set of domain-knowledge modules (event taxonomy, causal elasticities, scenario probabilities, historical impact DB) wrapped by an orchestrator that coordinates news ingestion, event extraction, causal analysis, and auto-override decisions. We port these as a new `intelligence/` service package with SQLAlchemy models (following zhanlu's `TimestampedBase` pattern), async registry methods, and a new `intelligence_agent` wired into zhanlu's existing EDIA pipeline delegation system.

**Tech Stack:** Python 3.11+, SQLAlchemy 2.0 (async), Alembic, ChromaDB, asyncio, FastAPI, Pydantic

## Global Constraints

- All models inherit `TimestampedBase` from `app.models.base` (UUID PK, org_id/app_id tenant wall, soft-delete, timestamps)
- All DB access is async via `AsyncSessionLocal` from `app.database`
- LLM calls go through `call_llm_with_reliability` from `app.services.sub_agent_reliability`
- Background tasks use zhanlu's `scheduled_tasks.py` asyncio pattern (not raw `threading.Thread`)
- Agent definitions follow `AgentDefinition` pattern from `app.services.agent_definitions`
- Delegation tools follow `_run_sub_agent` pattern from `app.services.tool_handlers.edia_delegation_tools`
- EDIA source files at `/home/ysk2025/EDIA_5.1.2/backend/src/intelligence/`
- Target zhanlu path: `/home/ysk2025/zhanlu_7_30/backend/app/services/intelligence/`

## File Structure

```
backend/app/services/intelligence/
├── __init__.py                    # Package exports
├── event_types.py                 # ExtractedEvent dataclass (port as-is)
├── event_deduplicator.py          # In-memory dedup cache (port as-is)
├── event_extractor.py             # LLM-based event extraction (adapt LLM calls)
├── event_impact_db.py             # Historical event DB (port as-is, in-memory)
├── second_order_effects.py        # Second-order effect profiles (port as-is)
├── causal_narrative_engine.py     # Multi-hop causal reasoning (adapt LLM calls)
├── scenario_probability_engine.py # Evidence-based scenario generation (adapt LLM calls)
├── auto_override_decision.py      # Auto-override pipeline (adapt macro_override integration)
├── web_search_service.py          # Web search backends (port as-is)
├── rss_feed_service.py            # RSS feed fetching (port as-is)
├── news_ingestion_agent.py        # Background news scanner (adapt to async scheduled task)
├── intelligence_orchestrator.py   # Central coordinator (adapt to class-based service)
├── external_news_context_builder.py # Query-time news context (adapt imports)
├── intelligence_learning_loop.py  # Self-calibration (adapt ChromaDB refs)

backend/app/models/
└── intelligence.py                 # IntelligenceEvent + IngestionStatus SQLAlchemy models

backend/app/services/intelligence/
└── event_registry.py              # Async SQLAlchemy event registry (rewrite from EDIA's raw SQL)
```

---

### Task 1: Package scaffolding + event_types.py port

**Files:**
- Create: `backend/app/services/intelligence/__init__.py`
- Create: `backend/app/services/intelligence/event_types.py`
- Test: `backend/tests/test_intelligence/test_event_types.py`

**Interfaces:**
- Produces: `ExtractedEvent` dataclass with `event_id`, `impact_magnitude`, `to_dict()`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_intelligence/test_event_types.py
from app.services.intelligence.event_types import ExtractedEvent

def test_extracted_event_creation():
    event = ExtractedEvent(
        event_type="plant_outage",
        headline="Major plant shutdown in East China",
        affected_commodities=["mixed_c5", "isoprene"],
        direction="bullish",
        magnitude_estimate="large",
        certainty="confirmed",
        time_horizon="short",
        geographic_scope="china",
        relevance_to_c5_c9="direct",
    )
    assert event.event_id.startswith("evt_")
    assert event.impact_magnitude > 0
    assert event.to_dict()["impact_magnitude"] == event.impact_magnitude

def test_extracted_event_impact_magnitude():
    event = ExtractedEvent(
        event_type="opec_decision",
        headline="OPEC cuts production",
        affected_commodities=["crude_oil"],
        direction="bullish",
        magnitude_estimate="extreme",
        certainty="confirmed",
        time_horizon="medium",
        geographic_scope="global",
        relevance_to_c5_c9="direct",
    )
    # extreme=0.95, confirmed=0.95, direct=0.90
    # 0.95*0.40 + 0.95*0.35 + 0.90*0.25 = 0.38 + 0.3325 + 0.225 = 0.9375
    assert event.impact_magnitude == 0.938
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && python -m pytest tests/test_intelligence/test_event_types.py -v`
Expected: FAIL with ModuleNotFoundError

- [ ] **Step 3: Create package and port event_types.py**

Copy `/home/ysk2025/EDIA_5.1.2/backend/src/intelligence/event_types.py` to `backend/app/services/intelligence/event_types.py`. Change the import path comment only — the module is pure Python with no external dependencies.

- [ ] **Step 4: Create `__init__.py`**

```python
# backend/app/services/intelligence/__init__.py
"""EDIA Intelligence Layer — market event detection, causal reasoning, and scenario generation.

Ported from EDIA 5.1.2 with adaptations for zhanlu's async SQLAlchemy architecture.
"""
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && python -m pytest tests/test_intelligence/test_event_types.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/intelligence/__init__.py backend/app/services/intelligence/event_types.py backend/tests/test_intelligence/
git commit -m "feat(intelligence): port ExtractedEvent dataclass from EDIA"
```

---

### Task 2: SQLAlchemy models + Alembic migration

**Files:**
- Create: `backend/app/models/intelligence.py`
- Create: `backend/alembic/versions/XXX_add_intelligence_tables.py`
- Test: `backend/tests/test_intelligence/test_models.py`

**Interfaces:**
- Consumes: `TimestampedBase` from `app.models.base`
- Produces: `IntelligenceEvent`, `IntelligenceIngestionStatus` ORM models

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_intelligence/test_models.py
import pytest
from app.models.intelligence import IntelligenceEvent, IntelligenceIngestionStatus

def test_intelligence_event_model_fields():
    event = IntelligenceEvent(
        event_id="evt_test123",
        event_type="plant_outage",
        headline="Test event",
        affected_commodities=["mixed_c5"],
        direction="bullish",
        magnitude_estimate="large",
        certainty="confirmed",
        impact_magnitude=0.85,
        review_status="approved",
    )
    assert event.event_id == "evt_test123"
    assert event.org_id == "default-org"  # from TimestampedBase default

def test_ingestion_status_model():
    status = IntelligenceIngestionStatus(
        agent_name="news_ingestion_agent",
        running=False,
        cycle_in_progress=False,
    )
    assert status.agent_name == "news_ingestion_agent"
    assert status.running is False
```

- [ ] **Step 2: Run test to verify it fails**

- [ ] **Step 3: Create the SQLAlchemy models**

```python
# backend/app/models/intelligence.py
"""Intelligence domain models — market events and ingestion status.

Ported from EDIA's raw MySQL DDL, adapted to zhanlu's SQLAlchemy + TimestampedBase pattern.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import String, Text, Integer, Float, Boolean, JSON, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class IntelligenceEvent(TimestampedBase):
    """A market-moving event detected from news, RSS, or manual submission."""

    __tablename__ = "intelligence_events"

    event_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    affected_commodities: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    direction: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    magnitude_estimate: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    certainty: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    time_horizon: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    geographic_scope: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    relevance_to_c5_c9: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    key_entities: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    key_information: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    causal_chain_hint: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_credibility: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    full_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    raw_quote: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    detected_at: Mapped[str] = mapped_column(String(64), nullable=False)
    impact_magnitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    relevance_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    review_status: Mapped[str] = mapped_column(String(32), nullable=False, default="approved")
    usefulness_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    reviewed_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    review_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class IntelligenceIngestionStatus(TimestampedBase):
    """Tracks the health and freshness of background intelligence ingestion agents."""

    __tablename__ = "intelligence_ingestion_status"

    agent_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    running: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cycle_in_progress: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    scan_interval_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_started_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_completed_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_success_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_cycle_duration_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    last_articles_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_events_extracted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_events_stored: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_run_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
```

- [ ] **Step 4: Generate Alembic migration**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && alembic revision --autogenerate -m "add intelligence tables"`

- [ ] **Step 5: Run test to verify it passes**

- [ ] **Step 6: Commit**

```bash
git add backend/app/models/intelligence.py backend/alembic/versions/ backend/tests/test_intelligence/test_models.py
git commit -m "feat(intelligence): add SQLAlchemy models + migration for intelligence events"
```

---

### Task 3: Event registry (async SQLAlchemy)

**Files:**
- Create: `backend/app/services/intelligence/event_registry.py`
- Test: `backend/tests/test_intelligence/test_event_registry.py`

**Interfaces:**
- Consumes: `IntelligenceEvent`, `IntelligenceIngestionStatus` from Task 2; `ExtractedEvent` from Task 1
- Produces: `IntelligenceEventRegistry` class with `upsert_event()`, `list_recent_events()`, `review_event()`, `upsert_ingestion_status()`, `get_ingestion_freshness()`

**Key adaptation:** EDIA's `event_registry.py` uses raw SQL with `self.engine.begin()`. Zhanlu uses async SQLAlchemy sessions. All methods become `async def` and use `select()`, `insert()`, `update()` ORM constructs.

- [ ] **Step 1: Write the failing test** (test upsert + list + review)

- [ ] **Step 2: Run test to verify it fails**

- [ ] **Step 3: Implement the async registry**

Port from EDIA's `event_registry.py`, converting:
- `self.engine.begin() as conn` → `async with db.begin():` or `db.add()` / `db.execute()`
- Raw SQL `text()` → SQLAlchemy `select()`, `insert()`, `update()` constructs
- Module-level singleton `intelligence_event_registry` → removed (inject db session)

- [ ] **Step 4: Run test to verify it passes**

- [ ] **Step 5: Commit**

---

### Task 4: Event deduplicator + event extractor

**Files:**
- Create: `backend/app/services/intelligence/event_deduplicator.py` (port as-is)
- Create: `backend/app/services/intelligence/event_extractor.py` (adapt LLM calls)
- Test: `backend/tests/test_intelligence/test_deduplicator.py`, `test_extractor.py`

**Key adaptation for extractor:** EDIA uses `self.llm.generate_json()`. Zhanlu uses `call_llm_with_reliability()` from `app.services.sub_agent_reliability`.

- [ ] **Step 1-5:** Write test, verify fail, implement, verify pass, commit

---

### Task 5: Event impact DB + second-order effects

**Files:**
- Create: `backend/app/services/intelligence/event_impact_db.py` (port as-is, in-memory)
- Create: `backend/app/services/intelligence/second_order_effects.py` (port as-is)
- Test: `backend/tests/test_intelligence/test_impact_db.py`, `test_second_order.py`

These are pure domain-knowledge modules with no external dependencies. Port directly.

- [ ] **Step 1-5:** Write test, verify fail, implement, verify pass, commit

---

### Task 6: Causal narrative engine

**Files:**
- Create: `backend/app/services/intelligence/causal_narrative_engine.py`
- Test: `backend/tests/test_intelligence/test_causal_engine.py`

**Key adaptation:** Replace `self.llm.generate_completion()` with `call_llm_with_reliability()`. The elasticity tables (`_NAPHTHA_ELASTICITIES`, `_APPROX_PRICES_CNY`) are static domain knowledge — port as-is.

- [ ] **Step 1-5:** Write test, verify fail, implement, verify pass, commit

---

### Task 7: Scenario probability engine + auto-override

**Files:**
- Create: `backend/app/services/intelligence/scenario_probability_engine.py`
- Create: `backend/app/services/intelligence/auto_override_decision.py`
- Test: `backend/tests/test_intelligence/test_scenario_engine.py`, `test_auto_override.py`

**Key adaptation for auto-override:** EDIA calls `from backend.src.service.macro_override_service import set_override`. In zhanlu, this becomes a stub or a hook into the pricing agent's override mechanism. For now, log the decision and store it in the DB.

- [ ] **Step 1-5:** Write test, verify fail, implement, verify pass, commit

---

### Task 8: Web search + RSS feed services

**Files:**
- Create: `backend/app/services/intelligence/web_search_service.py` (port as-is)
- Create: `backend/app/services/intelligence/rss_feed_service.py` (port as-is)
- Test: `backend/tests/test_intelligence/test_web_search.py`, `test_rss.py`

These modules use `requests` and `feedparser` — both available in zhanlu. Port directly, only changing import paths.

- [ ] **Step 1-5:** Write test, verify fail, implement, verify pass, commit

---

### Task 9: News ingestion agent (async background task)

**Files:**
- Create: `backend/app/services/intelligence/news_ingestion_agent.py`
- Modify: `backend/app/services/scheduled_tasks.py` (add news ingestion task)
- Test: `backend/tests/test_intelligence/test_news_agent.py`

**Key adaptation:** EDIA uses `threading.Thread` with `while True: sleep()`. Zhanlu uses the `_periodic_task` asyncio pattern from `scheduled_tasks.py`. The agent becomes a class with an async `run_news_cycle()` method, wired as a scheduled task.

- [ ] **Step 1-5:** Write test, verify fail, implement, verify pass, commit

---

### Task 10: Intelligence orchestrator

**Files:**
- Create: `backend/app/services/intelligence/intelligence_orchestrator.py`
- Create: `backend/app/services/intelligence/external_news_context_builder.py`
- Create: `backend/app/services/intelligence/intelligence_learning_loop.py`
- Test: `backend/tests/test_intelligence/test_orchestrator.py`

**Key adaptation:** EDIA uses module-level singletons. Zhanlu uses a class-based service that accepts a db session. The orchestrator coordinates: fetch events → run causal analysis → generate scenarios → evaluate overrides → return structured context.

- [ ] **Step 1-5:** Write test, verify fail, implement, verify pass, commit

---

### Task 11: Agent definition + delegation tool wiring

**Files:**
- Modify: `backend/app/services/agent_definitions/__init__.py` (add `intelligence_agent`)
- Modify: `backend/app/services/tool_handlers/edia_delegation_tools.py` (add `ask_intelligence`)
- Create: `backend/app/services/tool_handlers/intelligence_tools.py` (new tools: `get_intelligence_context`, `get_market_events`, `ingest_manual_event`)
- Test: `backend/tests/test_intelligence/test_agent_wiring.py`

**Key adaptation:** Follow the existing `AgentDefinition` pattern. Add `ask_intelligence` to `_DENIED_RECURSIVE` set. The `intelligence_agent` system prompt instructs it to use `get_intelligence_context` before answering questions about market events.

- [ ] **Step 1-5:** Write test, verify fail, implement, verify pass, commit

---

## Implementation Notes

### LLM Call Adaptation Pattern

EDIA pattern:
```python
result = self.llm.generate_json(system_prompt, user_prompt)
```

Zhanlu adaptation:
```python
from app.services.sub_agent_reliability import call_llm_with_reliability
result = await call_llm_with_reliability(
    messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
    response_format="json",
    db=db,
)
```

### Async DB Pattern

EDIA pattern:
```python
with self.engine.begin() as conn:
    conn.execute(text("INSERT INTO ..."), payload)
```

Zhanlu adaptation:
```python
async with db.begin():
    event = IntelligenceEvent(**payload)
    db.add(event)
    await db.flush()
```

### Background Task Pattern

EDIA pattern:
```python
thread = threading.Thread(target=self._run_loop, daemon=True)
thread.start()
```

Zhanlu adaptation:
```python
# In scheduled_tasks.py:
NEWS_INGESTION_INTERVAL = 60 * 60  # 1 hour

async def _run_news_ingestion_cycle():
    async with AsyncSessionLocal() as db:
        agent = NewsIngestionAgent(db)
        await agent.run_news_cycle(trigger="scheduled")

# Add to start_scheduled_tasks():
loop.create_task(_periodic_task("news_ingestion", NEWS_INGESTION_INTERVAL, _run_news_ingestion_cycle))
```

---

## Phase 2 & 3 (Following Plans)

After Phase 1 is complete:

**Phase 2: Hybrid RAG** — Port EDIA's `hybrid_retrieval.py` (dense + sparse + RRF fusion) into zhanlu's `document_ingestion/` package. Upgrade `store.query()` to use hybrid retrieval. Add intelligence events to a dedicated ChromaDB collection.

**Phase 3: Forecasting Domain Logic** — Port causal elasticities and seasonal rules into a new `forecasting/domain_adjustments.py`. Add intelligence adjustment hook (calls P1's orchestrator). Add LSTM model. Add nightly self-improvement background task.
