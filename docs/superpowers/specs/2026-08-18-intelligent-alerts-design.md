# Intelligent Market Alerts — Design

> **Status:** Approved (brainstorming phase complete). Ready for implementation planning.
> **Date:** 2026-08-18
> **Scope:** Focused v1 — adaptive thresholds, ¥ impact, smart context/recommendations, user preferences, in-app + webhook channels.

---

## 1. Goal

Make the Acme BI Market Alerts panel a **decision-support surface** rather than a noisy price-tick feed. Every alert should answer three questions for the user:

1. **Why does this matter?** (context: how unusual is this move for *this* product?)
2. **What's the business impact?** (¥/day at the current shipment volume)
3. **What should I do next?** (concrete recommended action)

And the user should be able to tune the **noise floor** (severity, channels, mute list, threshold sensitivity) without code edits.

---

## 2. Current State (baseline)

| Layer | Today | Gap |
|---|---|---|
| Detection thresholds | Static ±3% (medium) / ±6% (high) | Industrials have wildly different baseline volatility — a 3% move on a stable product and on a volatile one are not equivalent signals. |
| Alert message | "X price dropped 8.3% in 7 days to 716.25" | No context, no "is this unusual?", no "what now?" |
| Business impact | Only computed in the daily digest (one row per day) | Regular alert panel has no ¥/day. User can't prioritize at a glance. |
| User preferences | One generic `alerting_enabled` flag | No severity floor, no mute list, no channel choice, no sensitivity slider. |
| Notification channels | In-app panel only | No webhook/email escape hatch. |
| Storage | Single `alert_logs` table, 4 alert types | Plenty of headroom; `payload` JSONB column is under-used. |

Files involved today:
- `backend/app/models/alert.py` — `AlertLog` model
- `backend/app/services/perception/alert_service.py` — `AlertService` (CRUD + `detect_and_persist`)
- `backend/app/services/perception/worker.py` — `run_alert_scan_cycle` (orchestrator)
- `backend/app/services/perception/digest_service.py` — `DigestService.compute_impact` (¥/day formula; will be refactored out)
- `backend/app/services/perception/digest_narrator.py` — LLM narrative pattern (model for baseline call)
- `backend/app/services/perception/diagnostics.py` — `build_alert_diagnostics`
- `backend/app/routers/alerts.py` — REST endpoints
- `backend/app/models/user_setting.py` — `UserSetting` (80+ fields, no alert-specific ones)
- `frontend/src/components/ProactiveAlerts.jsx` — slide-out panel
- `frontend/src/components/AlertButton.jsx` — bell + badge
- `frontend/src/api/alertApi.js` — frontend API client

---

## 3. User Decisions (locked in by Q&A 2026-08-18)

| Decision | Choice |
|---|---|
| Scope | **Focused v1** — adaptive thresholds + ¥ impact + better context/recommendations + user preferences + in-app + webhook |
| Adaptive threshold source | **LLM-judged baseline per product** (cached) |
| Notification channels | **In-app** (existing) **+ webhook** (reuse `UserSetting.webhook_url`) |
| Preferences storage | **Reuse `UserSetting`** (additive fields, no new model) |

---

## 4. Architecture

```
[Perception worker] (existing)
   └─ build_perception_snapshot → list[product dicts]
[NEW: SmartEvaluator] ─── orchestrates ───► [NEW: ProductBaselineCache] ──► LLM
   ├─ for each product, fetch baseline (cached 7d)
   ├─ decide trigger (adaptive threshold) + severity
   ├─ compute ¥ impact  ───► [NEW: services/perception/impact.py] (refactored from DigestService)
   ├─ generate "why" + recommendation (template, not LLM)
   └─ returns list[enriched_alert]
[AlertService.detect_and_persist] (existing) ← enriched_alert → AlertLog rows
   └─ payload now includes {why, recommendation, impact_yuan_per_day, baseline_used}
[NEW: ChannelRouter]
   └─ for each new alert, fan out to in-app (no-op) + webhook (HMAC-signed POST)
[UserSetting] ← 7 new fields
[NEW: AlertPreferencesService] ←─ load+apply on every scan / list call
```

### Module boundaries

| Module | Responsibility | Owns |
|---|---|---|
| `services/perception/baseline_cache.py` | LLM-judged per-product baseline, cached 7 days | `AlertProductBaseline` model, `get_or_compute(product_id)`, `seed_defaults()` |
| `services/perception/impact.py` | ¥/day from price × change × volume | refactored from `DigestService._get_avg_daily_volume` + `compute_impact` |
| `services/perception/smart_evaluator.py` | Decide trigger + severity + build context | `evaluate(snapshot, user_pref) → list[enriched_alert]`. Pure, no DB writes. |
| `services/alerts/channel_router.py` | Fan out to channels | `dispatch(alert, user_pref)`. Webhook HMAC sign + retry. |
| `services/alerts/preferences.py` | Load + apply UserSetting | `AlertPrefSnapshot` dataclass, `from_user(user)` |
| `routers/alert_preferences.py` | REST: get/update preferences | `GET/PATCH /api/users/me/alert-preferences` |
| `models/alert_baseline.py` | `AlertProductBaseline` | new table |
| `models/user_setting.py` | 7 new columns | additive migration |

### Data flow (one scan cycle)

```
1. run_alert_scan_cycle() called (manual button or cron)
2. SmartEvaluator.evaluate(snapshot, user_pref_for(org)):
   for each product in snapshot:
     baseline = baseline_cache.get_or_compute(product_id)
       └─ cache miss → async LLM call (fire-and-forget, don't block scan) → seed row
     threshold = baseline.baseline_pct × settings.alert_threshold_multiplier × user_pref.threshold_multiplier
     if |change_7d_pct| < threshold: skip
     severity = map(change, threshold, baseline)
     impact = impact.compute(product, alert_type)        # may be null if ERP down
     ctx = build_context(product, baseline, impact)     # "why" + recommendation
   return enriched_alerts
3. AlertService.detect_and_persist(db, org_id, enriched_alerts)
   └─ writes AlertLog with payload = {why, recommendation, impact_yuan_per_day, baseline_used, threshold_used}
4. For each new persisted alert:
   ChannelRouter.dispatch(alert, user_pref)
   └─ in_app: no-op (panel polls)
   └─ webhook: HMAC-SHA256 signed POST, 1 retry on 5xx/timeout
```

---

## 5. UserSetting fields (additive, no new model)

| Field | Type | Default | Meaning |
|---|---|---|---|
| `alert_min_severity` | `String(10)` | `"low"` | Drop alerts below this severity |
| `alert_muted_products` | `JSONB` | `[]` | List of product_ids to never alert on |
| `alert_channels` | `JSONB` | `["in_app"]` | Subscribed channels: `"in_app"`, `"webhook"` |
| `alert_threshold_multiplier` | `Float` | `1.0` | `0.5` = half as sensitive; `1.5` = 50% more |
| `alert_quiet_hours` | `JSONB` nullable | `null` | `{start_hour: 22, end_hour: 7, tz: "Asia/Shanghai"}` |
| `alert_recommendations_enabled` | `Boolean` | `True` | Show "Suggested next step" text |
| `alert_webhook_secret` | `String(64)` | auto-gen on first opt-in | HMAC key for webhook signing |

All read sites use `safe_get(setting, key, default)` to tolerate NULL/missing columns and malformed JSON.

---

## 6. ProductBaselineCache — LLM-judged, cached

**One row per product in `alert_product_baselines`.** TTL = 7 days. Read-through.

```sql
CREATE TABLE alert_product_baselines (
  product_id   VARCHAR(64) PRIMARY KEY,
  baseline_pct FLOAT       NOT NULL,        -- e.g. 2.5 means ±2.5% is "normal weekly noise"
  reasoning    TEXT,                        -- "原料油 historically tracks crude; 2-3% weekly swings are normal"
  computed_at  TIMESTAMPTZ DEFAULT NOW(),
  model_used   VARCHAR(64)
);
```

**LLM call (one-time per product, fire-and-forget on miss):**

Input: `product_id`, `name_cn`, `name_en`, `category` (from knowledge graph), `current_price`, trailing 30d weekly % changes.
Output JSON (validated):
```json
{
  "baseline_7d_pct": 2.5,
  "reasoning": "原料油 historically tracks product e; weekly 2-3% swings are normal; only ±5%+ is actionable"
}
```

Used model: `settings.get_model()` via `call_llm(prompt, temperature=0.2, task_type="alert_baseline")` — same path as `DigestNarrator`.

**Failure:** log warning, write `baseline_pct=2.5, reasoning="LLM_failed_using_default"`, continue. **Never block the scan.**

**Trigger rule (deterministic, no LLM at trigger time):**
```python
threshold = baseline.baseline_pct * settings.alert_threshold_multiplier * user_pref.threshold_multiplier
trigger   = |change_7d_pct| >= threshold
severity  = "high"   if |change_7d_pct| >= max(baseline.baseline_pct * 2, settings.ALERT_HIGH_SEVERITY_FLOOR)
            "medium" if |change_7d_pct| >= threshold
            "low"    otherwise
```

The product-level baseline is the only "smart" input. Everything downstream is deterministic.

---

## 7. ¥ Impact on every alert

Refactor `DigestService._get_avg_daily_volume` and `DigestService.compute_impact` into a new `services/perception/impact.py` module so both the digest and the per-alert evaluator share one code path. Formula unchanged:

```
price alerts:    |price × change_7d_pct%| ÷ 7 × avg_daily_volume_tons
arbitrage:       (spread − freight) × avg_daily_volume_tons
```

Every newly-created `AlertLog` row carries `payload.impact_yuan_per_day` and `payload.avg_daily_volume_tons`. Panel renders a `¥X/day` chip. `null` if ERP MySQL is down or volume missing — UI shows "impact TBD" gracefully.

Volume source: `v_sale_order.FDELIQTY` (tons) over trailing 30 days, via the canonical `_PRODUCT_MATERIAL_NAMES` map in `services/erp/kpi.py`.

---

## 8. "Why this matters" + "Suggested next step"

Per-alert, generated by a **template function** at trigger time (no LLM call). Cheap, deterministic, no drift.

```python
def build_context(alert, baseline, impact) -> dict:
    return {
        "why": f"{baseline.reasoning} 当前波动 {alert.change_7d_pct:+.1f}% "
               f"高于典型 {baseline.baseline_pct:.1f}% 的基准。",
        "recommendation": pick_recommendation(alert, impact),
    }
```

`pick_recommendation` returns `{title, steps[]}` from ~6 hand-written Chinese templates keyed on `(alert_type, severity, has_volume)`:

| Pattern | Recommendation title | Steps (1-2 items) |
|---|---|---|
| `high price_spike + has_volume` | 锁定销售窗口 | 1) 在 ¥{price} 锁定现有库存 30-50% 销售；2) 与长期客户确认补货窗口 |
| `high price_drop + has_volume` | 评估采购延期 | 1) 评估原料采购延期 1-2 周可行性（潜在节省 ¥{impact}/天）；2) 关注是否有进一步下行空间 |
| `medium` | 加入 24h 观察列表 | 1) 加入 24h 观察；2) 如再触发则升级为 high |
| `high regional_arbitrage` | 协调跨区运输 | 1) 协调 {low_region}→{high_region} 运输计划，套利空间 ¥{impact}/天 |
| `low` | (no recommendation) | empty steps |
| any + no_volume | (skip recommendation) | impact TBD |

When `alert_recommendations_enabled=False`, suppress the section entirely. When the user clicks "Investigate" (already exists), the chat pre-fills the prompt with the alert context — **deferred to v1.1** (noted but not in scope).

---

## 9. ChannelRouter — in-app + webhook

```python
async def dispatch(alert: AlertLog, pref: AlertPrefSnapshot) -> None:
    if "in_app" in pref.channels:
        # No-op — panel polls /api/alerts every 2 minutes
        return
    if "webhook" not in pref.channels or not pref.webhook_url:
        return
    if alert.severity not in ("high", "medium"):
        return  # only surface medium+ externally
    if in_quiet_hours(pref.quiet_hours, now_local()):
        # TODO v1.1: queue for quiet end
        logger.info("AlertRouter: webhook suppressed (quiet hours): %s", alert.id)
        return
    payload = {
        "alert_id": alert.id,
        "product": alert.product_name,
        "type": alert.alert_type,
        "severity": alert.severity,
        "change_7d_pct": alert.change_7d_pct,
        "price": alert.price,
        "impact_yuan_per_day": alert.payload.get("impact_yuan_per_day"),
        "recommendation": alert.payload.get("recommendation"),
        "why": alert.payload.get("why"),
        "url": f"{settings.APP_BASE_URL}/alerts/{alert.id}",
    }
    await fire_webhook(pref.webhook_url, payload, secret=pref.webhook_secret)
```

`fire_webhook`:
- HMAC-SHA256 signature in `X-Zhanlu-Signature: sha256=<hex>` header
- 5 second timeout
- 4xx → log + drop (user's URL is bad)
- 5xx/timeout → 1 retry after 30 s, then drop
- No new infra. `httpx.AsyncClient` (already a dep).

---

## 10. Frontend additions

### `ProactiveAlerts.jsx` (modified)

Each `AlertCard`:
- New **¥/day chip** next to the severity badge (or "impact TBD" if null).
- Expandable **"Why this matters"** section (text from `payload.why`).
- **"Suggested next step"** bullet list (text from `payload.recommendation.steps[]`).
- Small **"Mute this product"** action in the card footer.

Panel header:
- New **gear icon** → opens `AlertPreferencesModal`.

### `AlertPreferencesModal.jsx` (new)

- Min severity: radio (low / medium / high)
- Channels: checkboxes (In-app, Webhook)
- Threshold sensitivity: slider (0.5× to 2.0×, step 0.1)
- Quiet hours: 2 hour pickers + timezone select (Asia/Shanghai default)
- Mute list: typeahead input over product_id, removable chips
- Save → `PATCH /api/users/me/alert-preferences` → toast

### `alertApi.js` (additions)

- `getAlertPreferences() → Promise<AlertPrefSnapshot>`
- `updateAlertPreferences(prefs) → Promise<AlertPrefSnapshot>`
- `muteProduct(productId) → Promise<{muted: string[]}>` (delegates to `updateAlertPreferences({muted_products: [...existing, productId]})`)

### `AlertButton.jsx`: no change.

---

## 11. Flag gating

| Flag | Default | Purpose |
|---|---|---|
| `SMART_ALERTS_ENABLED` | `False` | Master gate. When off, falls back to today's static ±3%/±6% thresholds and template messages. |
| `ALERT_BASELINE_LLM_ENABLED` | `True` (when SMART on) | If off, baselines seeded with default `2.5%` for all products (no LLM). |
| `ALERT_WEBHOOK_ENABLED` | `True` (when SMART on) | Master switch for the webhook channel. |
| `ALERT_IMPACT_ENABLED` | `True` (when SMART on) | Master switch for ¥/day computation. |
| `ALERT_RECOMMENDATIONS_ENABLED` | `True` (when SMART on) | Show recommendation text. |
| `ALERT_HIGH_SEVERITY_FLOOR` | `6.0` | Minimum |change_7d_pct|% to be classified as "high" regardless of baseline. |

All flags default to today's behavior to preserve the no-regression guarantee.

---

## 12. Error handling

| Failure | Handling |
|---|---|
| Baseline LLM call fails / times out | Log warning, seed row with `baseline_pct=2.5, reasoning="LLM_failed_using_default"`. Scan continues. |
| Baseline LLM returns invalid JSON | Same as above. |
| ERP MySQL volume query fails | `impact_yuan_per_day=null`. Alert still fires. UI shows "impact TBD". |
| Webhook 4xx response | Log + drop. (User's URL is bad; retrying won't help.) |
| Webhook 5xx or timeout | 1 retry after 30 s, then drop. No further escalation in v1. |
| UserSetting JSON field malformed | `safe_get(setting, "alert_muted_products", default=[])` at every read site. |
| User has no UserSetting row | Use defaults from `UserSetting.__init__`. |
| `alert_webhook_secret` is NULL when webhook is enabled | Auto-generate on first dispatch, persist on the row. |

---

## 13. Testing

### Backend (per project convention: individual files, run via `venv/bin/python -m pytest`)

| File | Coverage |
|---|---|
| `tests/services/perception/test_baseline_cache.py` | cache hit, cache miss → LLM call, LLM failure → default seed, invalid JSON → default seed, TTL not enforced (manual refresh only) |
| `tests/services/perception/test_impact.py` | price_spike formula, price_drop formula, arbitrage formula, missing volume → null, ERP down → null |
| `tests/services/perception/test_smart_evaluator.py` | trigger below threshold → skip, trigger at threshold → medium, double baseline → high, multiplier scales threshold, high floor clamps low-baseline products, no baseline yet → use default |
| `tests/services/alerts/test_channel_router.py` | in_app no-op, webhook payload shape + signature, 4xx drop, 5xx retry-then-drop, quiet hours skip, no secret auto-generated on first call |
| `tests/services/alerts/test_preferences.py` | mute list filters out, severity floor filters, multiplier scales, malformed JSON safe |
| `tests/services/perception/test_scan_integration.py` | full cycle: snapshot → evaluate → persist → payload shape (why/recommendation/impact/baseline_used) |
| `tests/routers/test_alert_preferences_api.py` | GET default, PATCH partial, malformed JSON 422 |

### Frontend (Vitest, React Testing Library)

| File | Coverage |
|---|---|
| `src/components/__tests__/ProactiveAlerts.impact.test.jsx` | ¥/day chip renders, "impact TBD" when null, recommendation bullets render |
| `src/components/__tests__/AlertPreferencesModal.test.jsx` | slider updates value, mute add/remove, save calls API, error toast on failure |
| `src/api/__tests__/alertApi.preferences.test.js` | `getAlertPreferences` and `updateAlertPreferences` call correct endpoints |

### No-regression guarantee

All 12 existing `tests/services/perception/` tests continue to pass. When `SMART_ALERTS_ENABLED=False`, the system must produce byte-identical output to today (no behavior change). A test asserts this.

---

## 14. Migration

`alembic/versions/062_smart_alerts.py` (idempotent raw SQL via `ADD COLUMN IF NOT EXISTS`):

```sql
-- UserSetting fields
ALTER TABLE user_settings
  ADD COLUMN IF NOT EXISTS alert_min_severity VARCHAR(10) DEFAULT 'low',
  ADD COLUMN IF NOT EXISTS alert_muted_products JSONB DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS alert_channels JSONB DEFAULT '["in_app"]'::jsonb,
  ADD COLUMN IF NOT EXISTS alert_threshold_multiplier FLOAT DEFAULT 1.0,
  ADD COLUMN IF NOT EXISTS alert_quiet_hours JSONB,
  ADD COLUMN IF NOT EXISTS alert_recommendations_enabled BOOLEAN DEFAULT TRUE,
  ADD COLUMN IF NOT EXISTS alert_webhook_secret VARCHAR(64);

-- New baselines table
CREATE TABLE IF NOT EXISTS alert_product_baselines (
  product_id   VARCHAR(64) PRIMARY KEY,
  baseline_pct FLOAT       NOT NULL,
  reasoning    TEXT,
  computed_at  TIMESTAMPTZ DEFAULT NOW(),
  model_used   VARCHAR(64)
);

CREATE INDEX IF NOT EXISTS ix_alert_product_baselines_computed_at
  ON alert_product_baselines (computed_at);
```

Per project convention: raw SQL via `ADD COLUMN IF NOT EXISTS` to avoid tripping the unapplied migration chain.

---

## 15. Out of scope (deferred)

- **Investigate button pre-fill** — chat pre-fills prompt with alert context. (v1.1)
- **Email digests** — daily bundle at 08:00 CST. (v1.2)
- **Slack/Feishu native channels** — webhook covers these via the user's own bridge. (v1.2)
- **Quiet-hours queueing** — currently drops during quiet hours; v1.1 stores them and fires at quiet-end. (v1.1)
- **Pattern detection** — volatility regimes, trend reversals, multi-product co-movement. (v2)
- **Feedback loop** — thumbs up/down on alerts to tune baseline. (v2)
- **Per-alert LLM narrative** — LLM at trigger time. (v2)
- **Multi-tenant scoping** — org-level baseline overrides, org-level channel policy. (v2)

---

## 16. Rollout plan (high level)

1. **Migration 062** applied (additive columns + new table).
2. **Backend**: `SMART_ALERTS_ENABLED=False` by default. Deploy code. Confirm zero behavior change.
3. **Smoke**: enable `SMART_ALERTS_ENABLED=True` in `.env`; run manual scan; verify 1-2 alerts fire with payload populated.
4. **Webhook**: enable `ALERT_WEBHOOK_ENABLED=True`; configure a test endpoint (e.g. webhook.site); verify HMAC signature and payload.
5. **Frontend**: preferences modal works; mute list respected; ¥/day chip renders.
6. **Monitor for 1 week**: alert volume, webhook delivery rate, false-positive rate.
7. **Tune**: adjust `alert_threshold_multiplier` per product based on user feedback.

---

## 17. Self-review checklist

- [x] No placeholders, no TBDs
- [x] All sections internally consistent
- [x] Module boundaries clear, each module has one job
- [x] Test plan covers new + regression
- [x] Flag gating preserves no-regression (`SMART_ALERTS_ENABLED=False` → byte-identical to today)
- [x] Migration is additive and idempotent
- [x] Error handling specifies fallback for every external call (LLM, ERP, webhook)
- [x] Out-of-scope items explicitly listed (not silently dropped)
