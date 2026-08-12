# CEO Morning Digest — Design Spec

> **Status**: Approved (2026-08-12)  
> **Author**: AI + User  
> **Scope**: Extend existing proactive alert system with CEO-perspective daily digest

---

## 1. Problem Statement

The current proactive alert system (`alert_service.py`) is **trader-grade**: it detects `price_spike`/`price_drop` (±3% in 7d, high ≥ ±6%) and `regional_arbitrage` (spread > freight), ranked by **percentage change**. A CEO needs alerts denominated in **margin impact (¥/day)**, ranked by ¥ at stake, not by volatility.

Additionally, the current system has a **critical latent bug** in `worker.py` that means it likely persists zero alerts in production (see §5).

## 2. Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Extend vs. greenfield | **Extend current system** | Incremental, reuses AlertLog model, Alert API, ProactiveAlerts UI |
| Ranking axis | **¥ impact/day** | CEO cares about margin, not % |
| Narrative approach | **A + B combined** | Deterministic spine (trustworthy numbers) + LLM narrator (polish), template as auto-fallback |
| Narrator default | **ON** (`ALERT_DIGEST_LLM_ENABLED=true`) | Better UX; template is failure fallback |
| Quiet days | **Persist nothing** | No noise |
| Delivery | **In-app alert (pinned card)** | No new chat channel needed; fits existing ProactiveAlerts |
| Dedup | **One digest per day** | `alert_type="daily_digest"` + org_id + date uniqueness |

## 3. Architecture

### 3.1 New Files

| File | Purpose |
|------|---------|
| `backend/app/services/perception/digest_service.py` | Core: collect, compute impact, rank, render template, persist |
| `backend/app/services/perception/digest_narrator.py` | LLM narrative layer, gated by `ALERT_DIGEST_LLM_ENABLED` |

### 3.2 Modified Files

| File | Change |
|------|--------|
| `backend/app/services/perception/worker.py` | **Fix critical bug** (see §5) |
| `backend/app/services/scheduled_tasks.py` | Add `_digest_loop()` (cloned from `_nightly_forecast_loop` pattern) |
| `backend/app/models/alert.py` | Add `payload` JSONB column |
| `backend/alembic/versions/053_add_alert_payload.py` | Migration for `payload` column |
| `backend/app/config.py` | Add `ALERT_DIGEST_ENABLED`, `ALERT_DIGEST_LLM_ENABLED`, `ALERT_DIGEST_HOUR_UTC`, `ALERT_DIGEST_TOP_N` |
| `frontend/src/components/ProactiveAlerts.jsx` | Render `daily_digest` as pinned "Morning Brief" card |
| `frontend/src/components/Sidebar.jsx` | Add `daily_digest` branch to `buildInvestigateQuestion` |

### 3.3 Config Flags

```python
ALERT_DIGEST_ENABLED: bool = False           # Master gate
ALERT_DIGEST_LLM_ENABLED: bool = True        # Narrator (default ON, template is fallback)
ALERT_DIGEST_HOUR_UTC: int = 0               # 08:00 CST
ALERT_DIGEST_TOP_N: int = 5                  # Top-N items in digest
```

## 4. Detailed Design

### 4.1 DigestService

```python
class DigestService:
    def __init__(self, db: Session, org_id: str): ...

    async def build_and_persist(self) -> Optional[AlertLog]:
        """Main entry: collect → compute → rank → narrate → persist."""

    async def collect(self, window_hours: int = 24) -> list[dict]:
        """Gather alerts from last 24h + current price snapshot for high-value products."""

    def compute_impact(self, alert: dict) -> dict:
        """Compute ¥ impact for a single alert.
        - price_spike/drop: |price × change_7d_pct%| ÷ 7 × avg_daily_volume_tons
        - regional_arbitrage: (spread − freight) × avg_daily_volume_tons
        Volume source: erp_product_sales_details.shipment_quantity, 30d trailing avg, FNAME match.
        """

    def rank(self, items: list[dict]) -> list[dict]:
        """Sort by abs(impact_yuan_per_day) descending, take top N."""

    def render_template(self, items: list[dict]) -> str:
        """Markdown template rendering. Used when LLM narrator fails or is disabled."""

    async def _get_avg_daily_volume(self, product_name: str) -> Optional[float]:
        """Query erp_product_sales_details for 30d trailing avg shipment_quantity."""
```

### 4.2 ¥ Impact Formulas

**Price alerts** (`price_spike` / `price_drop`):
```
impact_¥_per_day = |price × change_7d_pct%| ÷ 7 × avg_daily_volume_tons
```

**Arbitrage alerts** (`regional_arbitrage`):
```
impact_¥_per_day = (spread − freight) × avg_daily_volume_tons
```

Where:
- `price` = latest price from perception snapshot
- `change_7d_pct` = 7-day price change percentage
- `avg_daily_volume_tons` = `AVG(shipment_quantity)` from `erp_product_sales_details` over trailing 30 days, matched by `FNAME`
- `spread` = regional price difference
- `freight` = freight cost between regions

If volume query fails (ERP unavailable, no matching FNAME), `impact_¥_per_day` → `null` and the item falls back to severity-based ranking.

### 4.3 DigestNarrator

```python
class DigestNarrator:
    async def narrate(self, items: list[dict], date: str) -> Optional[str]:
        """LLM-generated morning brief narrative.
        - Receives pre-computed ranked items with ¥ impact
        - Instructed to NEVER invent figures
        - Returns None on any failure → template fallback
        Gated by ALERT_DIGEST_LLM_ENABLED.
        """
```

### 4.4 Persistence

Digest is stored as a single `AlertLog` row:

| Column | Value |
|--------|-------|
| `alert_type` | `"daily_digest"` |
| `severity` | `"high"` (pinned) |
| `source` | `"digest_service"` |
| `title` | `"Morning Brief — {date}"` |
| `message` | Narrated text (or template) |
| `payload` | JSONB: `{items: [...], date: "2026-08-12", narrator: "llm"|"template"}` |
| `org_id` | tenant scope |

**One-digest-per-day dedup**: before persisting, check if an `AlertLog` with `alert_type="daily_digest"` and same date already exists for this org.

### 4.5 Scheduler

```python
async def _digest_loop():
    """Sleep-until-target-time pattern, cloned from _nightly_forecast_loop.
    - Runs at ALERT_DIGEST_HOUR_UTC (default 0 = 08:00 CST)
    - Advisory lock to prevent concurrent runs
    - Error isolation (catch + log, don't crash loop)
    """
```

### 4.6 Quiet Days

If no alerts in the 24h window, **persist nothing**. No empty digest.

### 4.7 Frontend

**ProactiveAlerts.jsx**:
- Detect `alert_type === "daily_digest"` → render as pinned "Morning Brief" card
- Card shows: title, date, narrated summary (or template), top-5 items with ¥ impact
- Expandable details section

**Sidebar.jsx** — `buildInvestigateQuestion`:
- New `daily_digest` branch: generates a question like "展开今日晨报详情" or "Dig into the morning brief"

## 5. Prerequisite: Fix Worker Bug

The current `worker.py` has a critical bug that means the alert system likely persists **zero alerts** in production. Three issues:

### Bug 1: No db session
```python
# Current (broken):
snapshot = await asyncio.to_thread(build_perception_snapshot)  # no db!

# Fix:
from app.database import SessionLocal
db = SessionLocal()
try:
    snapshot = await asyncio.to_thread(build_perception_snapshot, db=db)
finally:
    db.close()
```

### Bug 2: Dict vs list
```python
# Current (broken):
snapshot = build_perception_snapshot()  # returns dict
AlertService.detect_and_persist(db, org_id, snapshot)  # expects list[dict]

# Fix: pass explicitly mapped list
product_list = [
    {
        "product_id": v["id"],
        "product_name": v.get("name_cn") or v.get("name_en"),
        "current_price": v["latest_price"],
        "change_7d_pct": v.get("change_7d_pct"),
        "region": v.get("region"),
        "freight_cost": v.get("freight_cost"),
    }
    for v in snapshot["products"].values()
    if v.get("latest_price") is not None
]
AlertService.detect_and_persist(db, org_id, product_list)
```

### Bug 3: Key mismatch
- `build_perception_snapshot` returns keys: `id`, `latest_price`, `name_cn`, `name_en`
- `detect_and_persist` expects keys: `product_id`, `current_price`, `product_name`
- Fix: explicit mapping as shown above

### Bug 4: Missing constant
`MARKET_ALERT_SCAN_INTERVAL` is referenced in `scheduled_tasks.py` but never defined. Will cause `NameError` at startup.

## 6. Migration Plan

| Step | Migration | Description |
|------|-----------|-------------|
| 053 | `053_add_alert_payload.py` | Add `payload` JSONB column to `alert_logs` |

```python
def upgrade():
    op.add_column("alert_logs", sa.Column("payload", sa.JSON(), nullable=True))

def downgrade():
    op.drop_column("alert_logs", "payload")
```

## 7. Testing Strategy

1. **Unit tests**: `DigestService.compute_impact`, `rank`, `render_template` with mock data
2. **Integration tests**: `build_and_persist` end-to-end with test DB
3. **Narrator tests**: `DigestNarrator.narrate` with mock LLM, verify fallback on failure
4. **Worker bug fix**: verify `run_alert_scan_cycle` now correctly persists alerts
5. **Frontend**: verify `daily_digest` card renders correctly, investigate question works

## 8. Out of Scope

- Push notification / email delivery
- Chat-based delivery (user can ask "what's in today's digest?")
- Historical digest archive UI
- Per-user digest customization
- Real-time ¥ impact on individual alerts (future: add impact column to all AlertLog rows)
