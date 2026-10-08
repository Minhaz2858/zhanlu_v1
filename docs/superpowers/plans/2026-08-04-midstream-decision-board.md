# Midstream Decision Board Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an AI Decision Board to `/market-dashboard/midstream` — a matrix showing all 4 products at once (name + code, price, 7d forecast, P(rise), plain-language AI action for next week) with expandable rows revealing a mini forecast chart + full AI narrative.

**Architecture:** New backend `decision_board_service.py` composes the existing `get_market_snapshot()` (current prices, Acme MySQL) with `get_forecast_base_batch_v2()` (forecast + Phase D/E overlays, Postgres) into per-product bundles with template-generated Chinese narratives (pure functions, no LLM). Frontend adds a self-fetching `DecisionBoard.jsx` between `ObservedDataStrip` and `ForecastTrendChart`.

**Tech Stack:** FastAPI + Pydantic (backend), React + TanStack Query + recharts + Tailwind (frontend), pytest + vitest.

**Spec:** `docs/superpowers/specs/2026-08-04-midstream-decision-board-design.md`

## Global Constraints

- Backend tests run individually (SSH RAM limited): `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/<file> -v`
- Frontend tests: `cd /home/ysk2025/zhanlu_7_30/frontend && node_modules/.bin/vitest run <file>`
- Backend has NO `--reload`: `docker restart zhanlu-backend` then sleep ~20s before curl checks. Backend port **5002**.
- Commit only the files listed per task (`git add <specific files>`) — user has unrelated uncommitted changes on `main`.
- No emojis in UI; lucide icons only. Dark-mode classes required (`dark:` variants).
- Chinese market convention: 涨=red, 跌=green for price-change numbers.
- `expected_change_pct` and `p_rise` are **fractions** (0.029 = 2.9%); multiply by 100 for display.

---

### Task 1: Decision-board schemas + narrative template functions

**Files:**
- Modify: `backend/app/schemas/market_dashboard.py` (append after `SkuForecastMeta`, ~line 158)
- Create: `backend/app/services/decision_board_service.py`
- Test: `backend/tests/test_service_decision_board.py`

**Interfaces:**
- Produces (used by Tasks 2-5): `DecisionBoardProduct`, `DecisionBoardResponse`, `SkuBrief` schemas; `action_label(action)`, `build_one_liner(...)`, `build_narrative(...)`, `build_briefing(products, day)`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_service_decision_board.py`:

```python
"""decision_board_service — pure narrative builders (no I/O)."""
from app.services.decision_board_service import (
    action_label, build_briefing, build_narrative, build_one_liner,
)
from app.schemas.market_dashboard import DecisionBoardProduct


def test_action_label_mapping():
    assert action_label("buy") == "建议备货"
    assert action_label("sell") == "建议出货"
    assert action_label("hold") == "按需跟进"
    assert action_label("watch") == "建议观望"
    assert action_label(None) == "建议观望"
    assert action_label("BUY") == "建议备货"


def test_one_liner_full_data():
    s = build_one_liner(
        action="buy", p_rise=0.68, expected_change_pct=0.029,
        intel_event_count=3, intel_bias="up",
    )
    assert "+2.9%" in s
    assert "68%" in s
    assert "建议备货" in s
    assert "3 条" in s


def test_one_liner_missing_data():
    assert build_one_liner(
        action="watch", p_rise=None, expected_change_pct=None,
    ) == "数据不足,建议观望"


def test_narrative_full_data():
    s = build_narrative(
        name_zh="电子元件", day=7, current_price=12100.0, forecast_base=12450.0,
        expected_change_pct=0.029, p_rise=0.68, action="buy", confidence="medium",
        directional_accuracy=0.72, intel_event_count=3, intel_bias="up",
        volatility_regime="NORMAL",
    )
    assert "电子元件" in s
    assert "12,100" in s
    assert "12,450" in s
    assert "+2.9%" in s
    assert "68%" in s
    assert "72%" in s          # directional accuracy
    assert "提前备货" in s
    assert "置信度:中" in s


def test_narrative_missing_forecast():
    s = build_narrative(
        name_zh="原料A", day=7, current_price=None, forecast_base=None,
        expected_change_pct=None, p_rise=None, action="watch", confidence="low",
        directional_accuracy=None, intel_event_count=0, intel_bias="neutral",
        volatility_regime=None,
    )
    assert "数据不足" in s
    assert "观望" in s


def test_briefing_with_strong_signal():
    products = [
        DecisionBoardProduct(
            id="product_d", name_zh="电子元件", name_en="Product_D", code="product_d",
            action="buy", confidence="medium", p_rise=0.68,
        ),
        DecisionBoardProduct(
            id="product_b", name_zh="材料B", name_en="PRODUCT_B", code="product_b",
            action="watch", confidence="low",
        ),
    ]
    s = build_briefing(products, 7)
    assert "电子元件" in s
    assert "看涨" in s
    assert "材料B" in s   # mentioned in the tail


def test_briefing_all_quiet():
    products = [
        DecisionBoardProduct(
            id="product_d", name_zh="电子元件", name_en="Product_D", code="product_d",
            action="watch", confidence="low",
        ),
    ]
    s = build_briefing(products, 7)
    assert "平稳" in s
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_service_decision_board.py -v`
Expected: FAIL with `ModuleNotFoundError: app.services.decision_board_service`

- [ ] **Step 3: Add schemas**

Append to `backend/app/schemas/market_dashboard.py`:

```python
class SkuBrief(BaseModel):
    """Compact SKU row for the decision board expanded card."""
    material_code: str
    name: str
    forecast_base: Optional[float] = None


class DecisionBoardProduct(BaseModel):
    """One product row on the decision board (plain-language bundle)."""
    id: str
    name_zh: str
    name_en: str
    code: str
    current_price: Optional[float] = None
    price_date: Optional[str] = None
    forecast_base: Optional[float] = None
    expected_change_pct: Optional[float] = None  # fraction, 0.029 = +2.9%
    p_rise: Optional[float] = None               # fraction [0,1]
    action: str = "watch"                        # buy|hold|sell|watch
    action_label_zh: str = "建议观望"
    confidence: str = "low"                      # high|medium|low
    trust_tier: Optional[str] = None
    trust_label_zh: Optional[str] = None
    trust_color: Optional[str] = None            # green|yellow|orange|red
    directional_accuracy: Optional[float] = None  # fraction [0,1]
    intel_event_count: int = 0
    intel_bias: str = "neutral"                  # up|down|neutral
    volatility_regime: Optional[str] = None      # NORMAL|MODERATE|HIGH
    one_liner_zh: str = ""
    narrative_zh: str = ""
    skus: list[SkuBrief] = []


class DecisionBoardResponse(BaseModel):
    """GET /market-dashboard/decision-board payload."""
    group: str
    day: int
    generated_at: str
    briefing_zh: str
    products: list[DecisionBoardProduct] = []
```

- [ ] **Step 4: Implement the narrative builders**

Create `backend/app/services/decision_board_service.py`:

```python
"""Decision board service — plain-language weekly decision summaries.

Composes market_dashboard_service.get_market_snapshot() (current prices)
with get_forecast_base_batch_v2() (forecast + Phase D/E overlays) into one
per-product bundle with Chinese plain-language narratives for the
/market-dashboard/:section Decision Board UI.

Narrative builders are pure functions (no I/O) for unit testing.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from app.models.market_dashboard import PRODUCT_GROUP_MAP, PRODUCT_LABELS
from app.schemas.market_dashboard import (
    DecisionBoardProduct, DecisionBoardResponse, SkuBrief,
)
from app.services import market_dashboard_service as mds

ACTION_LABELS_ZH = {
    "buy": "建议备货",
    "sell": "建议出货",
    "hold": "按需跟进",
    "watch": "建议观望",
}

TRUST_LABELS_ZH = {
    "high": "高可信",
    "medium": "中等可信",
    "directional": "方向性参考",
    "low": "低可信",
}

_CONFIDENCE_ZH = {"high": "高", "medium": "中", "low": "低"}
_VOLATILITY_ZH = {"NORMAL": "正常", "MODERATE": "偏高", "HIGH": "剧烈"}
_INTEL_BIAS_ZH = {"up": "偏多", "down": "偏空", "neutral": "中性"}

_ACTION_ADVICE_ZH = {
    "buy": "建议提前备货,锁定当前价格",
    "sell": "建议抓紧出货,兑现当前高价",
    "hold": "建议按需跟进,不追涨不囤货",
    "watch": "建议保持观望,等待更明确的信号",
}


def action_label(action: Optional[str]) -> str:
    return ACTION_LABELS_ZH.get((action or "watch").lower(), "建议观望")


def _fmt_price(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:,.0f}"


def _fmt_pct(v: Optional[float]) -> str:
    """Fraction (0.029) → signed percent (+2.9%)."""
    return "—" if v is None else f"{v * 100:+.1f}%"


def _fmt_prob(v: Optional[float]) -> str:
    return "—" if v is None else f"{v * 100:.0f}%"


def build_one_liner(
    *,
    action: str,
    p_rise: Optional[float],
    expected_change_pct: Optional[float],
    intel_event_count: int = 0,
    intel_bias: str = "neutral",
) -> str:
    if p_rise is None and expected_change_pct is None:
        return "数据不足,建议观望"
    parts = []
    if expected_change_pct is not None:
        parts.append(f"预计{_fmt_pct(expected_change_pct)}")
    if p_rise is not None:
        parts.append(f"上涨概率 {_fmt_prob(p_rise)}")
    line = ",".join(parts) + f",{action_label(action)}"
    if intel_event_count > 0 and intel_bias in ("up", "down"):
        line += f"(情报{_INTEL_BIAS_ZH[intel_bias]} {intel_event_count} 条)"
    return line


def build_narrative(
    *,
    name_zh: str,
    day: int,
    current_price: Optional[float],
    forecast_base: Optional[float],
    expected_change_pct: Optional[float],
    p_rise: Optional[float],
    action: str,
    confidence: str,
    directional_accuracy: Optional[float],
    intel_event_count: int,
    intel_bias: str,
    volatility_regime: Optional[str],
) -> str:
    if forecast_base is None or current_price is None:
        return f"{name_zh}当前预测数据不足,{_ACTION_ADVICE_ZH['watch']}。"
    move = "涨至" if (expected_change_pct or 0) >= 0 else "跌至"
    s1 = (
        f"{name_zh}现价 {_fmt_price(current_price)} 元/吨,"
        f"预计未来 {day} 天{move} {_fmt_price(forecast_base)} 元/吨"
        f"({_fmt_pct(expected_change_pct)})"
    )
    if p_rise is not None:
        s1 += f",上涨概率 {_fmt_prob(p_rise)}"
    s1 += "。"
    evidence = []
    if directional_accuracy is not None:
        evidence.append(f"方向预测历史准确率 {_fmt_prob(directional_accuracy)}")
    if intel_event_count > 0:
        evidence.append(
            f"市场情报{_INTEL_BIAS_ZH.get(intel_bias, '中性')}"
            f"({intel_event_count} 条在册事件)"
        )
    if volatility_regime:
        evidence.append(
            f"波动状态{_VOLATILITY_ZH.get(volatility_regime, volatility_regime)}"
        )
    s2 = "、".join(evidence) + "。" if evidence else ""
    s3 = (
        f"{_ACTION_ADVICE_ZH.get(action, _ACTION_ADVICE_ZH['watch'])}"
        f"(置信度:{_CONFIDENCE_ZH.get(confidence, '低')})。"
    )
    return s1 + s2 + s3


def build_briefing(products: list[DecisionBoardProduct], day: int) -> str:
    strong = [
        p for p in products
        if p.action in ("buy", "sell") and p.confidence != "low"
    ]
    if not strong:
        return (
            f"未来 {day} 天市场整体平稳,{len(products)} 个产品均无强买卖信号,"
            "建议按需跟进、控制库存风险。"
        )
    segs = []
    for p in strong:
        direction = "看涨" if p.action == "buy" else "看跌"
        prob = f"(上涨概率 {_fmt_prob(p.p_rise)})" if p.p_rise is not None else ""
        segs.append(f"{p.name_zh}{direction}{prob},{_ACTION_ADVICE_ZH[p.action]}")
    others = [p for p in products if p not in strong]
    tail = (
        "其余 " + "、".join(p.name_zh for p in others) + " 建议观望或按需跟进。"
        if others else ""
    )
    return f"未来 {day} 天重点关注:" + ";".join(segs) + "。" + tail
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_service_decision_board.py -v`
Expected: 6 PASS

- [ ] **Step 6: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30 && git add backend/app/schemas/market_dashboard.py backend/app/services/decision_board_service.py backend/tests/test_service_decision_board.py && git commit -m "feat(decision-board): schemas + zh narrative template builders"
```

---

### Task 2: `build_decision_board` composition

**Files:**
- Modify: `backend/app/services/decision_board_service.py` (append)
- Test: `backend/tests/test_service_decision_board.py` (append)

**Interfaces:**
- Consumes: `mds.get_market_snapshot(group) -> list[MarketSnapshot]`; `mds.get_forecast_base_batch_v2(product_ids, day, db) -> ForecastBaseBatchResponse` (fields: `base_by_product`, `overlay_by_product`, `sku_forecasts_by_product`).
- Produces: `build_decision_board(group: str, day: int = 7, db=None) -> DecisionBoardResponse` (used by Task 3 router).

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_service_decision_board.py`:

```python
from app.schemas.market_dashboard import (
    DecisionMeta, DirectionalEdgeMeta, ForecastBaseBatchResponse,
    ForecastOverlayMeta, IntelligenceOverlayMeta, MarketSnapshot,
    PolicyOverlayMeta, ProbabilityOverlayMeta, SkuForecastMeta, TrustTierMeta,
)
from app.services import decision_board_service as dbs


def _snapshot(pid, price):
    return MarketSnapshot(
        id=pid, name=pid, group="midstream", price=price, date="2026-08-03",
    )


def _overlay(pid, action="buy", conf="medium"):
    return ForecastOverlayMeta(
        product_id=pid,
        intelligence=IntelligenceOverlayMeta(active_event_count=3, bias_direction="up"),
        policy=PolicyOverlayMeta(volatility_regime="NORMAL"),
        trust_tier=TrustTierMeta(
            tier="high", badge_color="green",
            badge_label_zh="高可信", badge_label_en="High",
        ),
        probability=[ProbabilityOverlayMeta(horizon=7, p_rise=0.68, expected_change_pct=0.029)],
        directional=[DirectionalEdgeMeta(horizon=7, accuracy=0.72, n_test=40, p_value=0.001, status="edge")],
        decision=[DecisionMeta(horizon=7, action=action, confidence=conf, rationale="r")],
    )


def test_build_decision_board_composes(monkeypatch):
    monkeypatch.setattr(
        dbs.mds, "get_market_snapshot",
        lambda group: [_snapshot(pid, 12100.0)
                       for pid in ("product_d", "product_f", "product_b", "feedstock_a")],
    )
    monkeypatch.setattr(
        dbs.mds, "get_forecast_base_batch_v2",
        lambda product_ids, day=7, db=None: ForecastBaseBatchResponse(
            day=day,
            base_by_product={pid: 12450.0 for pid in product_ids},
            overlay_by_product={pid: _overlay(pid) for pid in product_ids},
            sku_forecasts_by_product={
                "feedstock_a": [SkuForecastMeta(
                    material_code="C5R-001", name="原料A SKU", report_order=1,
                    is_primary=True, forecast_base=8100.0,
                )],
            },
        ),
    )
    resp = dbs.build_decision_board("midstream", day=7)
    assert resp.group == "midstream"
    assert len(resp.products) == 4
    iso = next(p for p in resp.products if p.id == "product_d")
    assert iso.name_zh == "电子元件"
    assert iso.code == "product_d"
    assert iso.current_price == 12100.0
    assert iso.forecast_base == 12450.0
    assert iso.p_rise == 0.68
    assert iso.action == "buy"
    assert iso.action_label_zh == "建议备货"
    assert iso.trust_label_zh == "高可信"
    assert iso.directional_accuracy == 0.72
    assert "12,100" in iso.narrative_zh
    raf = next(p for p in resp.products if p.id == "feedstock_a")
    assert raf.skus[0].material_code == "C5R-001"
    assert "电子元件" in resp.briefing_zh


def test_build_decision_board_missing_overlay(monkeypatch):
    monkeypatch.setattr(
        dbs.mds, "get_market_snapshot",
        lambda group: [_snapshot("product_d", None)],
    )
    monkeypatch.setattr(
        dbs.mds, "get_forecast_base_batch_v2",
        lambda product_ids, day=7, db=None: ForecastBaseBatchResponse(
            day=day,
            base_by_product={"product_d": None},
            overlay_by_product={"product_d": None},
            sku_forecasts_by_product={},
        ),
    )
    # limit group to a single product via PRODUCT_GROUP_MAP patch
    monkeypatch.setattr(dbs, "PRODUCT_GROUP_MAP", {"midstream": ["product_d"]})
    resp = dbs.build_decision_board("midstream", day=7)
    p = resp.products[0]
    assert p.action == "watch"
    assert p.action_label_zh == "建议观望"
    assert p.one_liner_zh == "数据不足,建议观望"
    assert "数据不足" in p.narrative_zh
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_service_decision_board.py -v`
Expected: FAIL with `AttributeError: ... build_decision_board`

- [ ] **Step 3: Implement `build_decision_board`**

Append to `backend/app/services/decision_board_service.py`:

```python
def _pick(items, day):
    """Return the overlay entry whose horizon == day, else None."""
    for it in items or []:
        if getattr(it, "horizon", None) == day:
            return it
    return None


def build_decision_board(group: str, day: int = 7, db=None) -> DecisionBoardResponse:
    product_ids = PRODUCT_GROUP_MAP.get(group, [])
    # get_market_snapshot enforces the Acme fail-closed guard (503).
    snapshots = {s.id: s for s in mds.get_market_snapshot(group)}
    batch = mds.get_forecast_base_batch_v2(product_ids, day=day, db=db)

    products: list[DecisionBoardProduct] = []
    for pid in product_ids:
        snap = snapshots.get(pid)
        labels = PRODUCT_LABELS.get(pid, {})
        overlay = (batch.overlay_by_product or {}).get(pid)
        prob = _pick(overlay.probability if overlay else None, day)
        dec = _pick(overlay.decision if overlay else None, day)
        dire = _pick(overlay.directional if overlay else None, day)
        intel = overlay.intelligence if overlay else None
        policy = overlay.policy if overlay else None
        trust = overlay.trust_tier if overlay else None

        action = dec.action if dec else "watch"
        confidence = dec.confidence if dec else "low"
        p_rise = prob.p_rise if prob else None
        exp_chg = prob.expected_change_pct if prob else None
        intel_count = intel.active_event_count if intel else 0
        intel_bias = intel.bias_direction if intel else "neutral"
        vol = policy.volatility_regime if policy else None
        name_zh = labels.get("label_zh", pid)
        current = snap.price if snap else None
        base = (batch.base_by_product or {}).get(pid)

        trust_label = None
        if trust is not None:
            trust_label = trust.badge_label_zh or TRUST_LABELS_ZH.get(trust.tier)

        skus = [
            SkuBrief(material_code=s.material_code, name=s.name,
                     forecast_base=s.forecast_base)
            for s in (batch.sku_forecasts_by_product or {}).get(pid, [])
        ]

        products.append(DecisionBoardProduct(
            id=pid,
            name_zh=name_zh,
            name_en=labels.get("label_en", pid),
            code=pid,
            current_price=current,
            price_date=snap.date if snap else None,
            forecast_base=base,
            expected_change_pct=exp_chg,
            p_rise=p_rise,
            action=action,
            action_label_zh=action_label(action),
            confidence=confidence,
            trust_tier=trust.tier if trust else None,
            trust_label_zh=trust_label,
            trust_color=trust.badge_color if trust else None,
            directional_accuracy=dire.accuracy if dire else None,
            intel_event_count=intel_count,
            intel_bias=intel_bias,
            volatility_regime=vol,
            one_liner_zh=build_one_liner(
                action=action, p_rise=p_rise, expected_change_pct=exp_chg,
                intel_event_count=intel_count, intel_bias=intel_bias,
            ),
            narrative_zh=build_narrative(
                name_zh=name_zh, day=day, current_price=current,
                forecast_base=base, expected_change_pct=exp_chg, p_rise=p_rise,
                action=action, confidence=confidence,
                directional_accuracy=dire.accuracy if dire else None,
                intel_event_count=intel_count, intel_bias=intel_bias,
                volatility_regime=vol,
            ),
            skus=skus,
        ))

    return DecisionBoardResponse(
        group=group,
        day=day,
        generated_at=datetime.now(timezone.utc).isoformat(),
        briefing_zh=build_briefing(products, day),
        products=products,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_service_decision_board.py -v`
Expected: 8 PASS

- [ ] **Step 5: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30 && git add backend/app/services/decision_board_service.py backend/tests/test_service_decision_board.py && git commit -m "feat(decision-board): build_decision_board composing snapshot + forecast overlays"
```

---

### Task 3: Router endpoint `GET /market-dashboard/decision-board`

**Files:**
- Modify: `backend/app/routers/market_dashboard.py` (imports line 16-23 + new route after `/forecast-base-batch`, ~line 100)
- Test: `backend/tests/test_router_decision_board.py`

**Interfaces:**
- Consumes: `dbs.build_decision_board(group, day)` from Task 2; `DecisionBoardResponse` from Task 1.
- Produces: `GET /api/market-dashboard/decision-board?group=midstream&day=7` (used by Task 4 frontend client).

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_router_decision_board.py`:

```python
"""decision-board endpoint — auth overridden, tests endpoint logic."""
import pytest
from fastapi.testclient import TestClient

from app.deps import get_current_user_required


@pytest.fixture
def client():
    from main import app
    fake_user = type("FakeUser", (), {
        "id": "test-user", "org_id": "default-org", "is_anonymous": False,
    })()
    app.dependency_overrides[get_current_user_required] = lambda: fake_user
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_decision_board_503_when_unconfigured(monkeypatch, client):
    monkeypatch.setattr(
        "app.services.market_dashboard_service.settings.Acme_MYSQL_URL", ""
    )
    r = client.get("/api/market-dashboard/decision-board?group=midstream&day=7")
    assert r.status_code == 503
    assert r.json()["detail"] == "edia_unavailable"


def test_decision_board_returns_payload(monkeypatch, client):
    from app.schemas.market_dashboard import (
        DecisionBoardProduct, DecisionBoardResponse,
    )
    fake = DecisionBoardResponse(
        group="midstream", day=7, generated_at="2026-08-04T00:00:00+00:00",
        briefing_zh="未来 7 天市场整体平稳。",
        products=[DecisionBoardProduct(
            id="product_d", name_zh="电子元件", name_en="Product_D",
            code="product_d",
        )],
    )
    monkeypatch.setattr(
        "app.services.decision_board_service.build_decision_board",
        lambda group, day: fake,
    )
    r = client.get("/api/market-dashboard/decision-board?group=midstream")
    assert r.status_code == 200
    data = r.json()
    assert data["products"][0]["name_zh"] == "电子元件"
    assert data["briefing_zh"]


def test_decision_board_rejects_bad_group(client):
    r = client.get("/api/market-dashboard/decision-board?group=bogus")
    assert r.status_code == 422
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_router_decision_board.py -v`
Expected: FAIL with 404 (route not registered)

- [ ] **Step 3: Implement the route**

In `backend/app/routers/market_dashboard.py`:

1. Add to the schema import block (line 16-23): `DecisionBoardResponse,` (append to the existing parenthesized import list).
2. Add after the `svc` import (line 24):

```python
from app.services import decision_board_service as dbs
```

3. Add the route after the `/forecast-base-batch` handler (~line 100):

```python
@router.get("/decision-board", response_model=DecisionBoardResponse)
def get_decision_board(
    group: str = Query(..., pattern="^(upstream|midstream|downstream)$"),
    day: int = Query(7, ge=1, le=30),
    user=Depends(get_current_user_required),
) -> DecisionBoardResponse:
    """Plain-language weekly decision board for all products in a group."""
    return dbs.build_decision_board(group=group, day=day)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ysk2025/zhanlu_7_30/backend && venv/bin/python -m pytest tests/test_router_decision_board.py -v`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30 && git add backend/app/routers/market_dashboard.py backend/tests/test_router_decision_board.py && git commit -m "feat(decision-board): GET /market-dashboard/decision-board endpoint"
```

---

### Task 4: Frontend API client + DecisionBoard matrix

**Files:**
- Modify: `frontend/src/api/marketDashboard.js` (add one method after `forecastBaseBatch`, line 51)
- Create: `frontend/src/pages/MarketDashboard/components/DecisionBoard.jsx`
- Test: `frontend/src/pages/MarketDashboard/components/__tests__/DecisionBoard.test.jsx`

**Interfaces:**
- Consumes: `GET /api/market-dashboard/decision-board` (Task 3).
- Produces: `<DecisionBoard group selectedId onSelectProduct />` (wired in Task 6). `onSelectProduct(id)` fires on row click.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/pages/MarketDashboard/components/__tests__/DecisionBoard.test.jsx`:

```jsx
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

const FIXTURE = {
  group: 'midstream', day: 7, generated_at: '2026-08-04T00:00:00+00:00',
  briefing_zh: '未来 7 天重点关注:电子元件看涨(上涨概率 68%),建议提前备货。',
  products: [
    {
      id: 'product_d', name_zh: '电子元件', name_en: 'Product_D', code: 'product_d',
      current_price: 12100, price_date: '2026-08-03', forecast_base: 12450,
      expected_change_pct: 0.029, p_rise: 0.68, action: 'buy',
      action_label_zh: '建议备货', confidence: 'medium',
      trust_tier: 'high', trust_label_zh: '高可信', trust_color: 'green',
      directional_accuracy: 0.72, intel_event_count: 3, intel_bias: 'up',
      volatility_regime: 'NORMAL',
      one_liner_zh: '预计+2.9%,上涨概率 68%,建议备货',
      narrative_zh: '电子元件现价 12,100 元/吨,预计未来 7 天涨至 12,450 元/吨。',
      skus: [],
    },
    {
      id: 'product_b', name_zh: '材料B', name_en: 'PRODUCT_B', code: 'product_b',
      current_price: 9800, price_date: '2026-08-03', forecast_base: null,
      expected_change_pct: null, p_rise: null, action: 'watch',
      action_label_zh: '建议观望', confidence: 'low',
      trust_tier: 'low', trust_label_zh: '低可信', trust_color: 'red',
      directional_accuracy: null, intel_event_count: 0, intel_bias: 'neutral',
      volatility_regime: null,
      one_liner_zh: '数据不足,建议观望',
      narrative_zh: '材料B当前预测数据不足,建议保持观望。',
      skus: [],
    },
  ],
};

vi.mock('@/api/marketDashboard', () => ({
  marketDashboardApi: {
    decisionBoard: vi.fn().mockResolvedValue(FIXTURE),
    forecastChart: vi.fn().mockResolvedValue({ product_id: 'product_d', points: [] }),
  },
}));

import DecisionBoard from '../DecisionBoard';

function renderBoard(props = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <DecisionBoard group="midstream" selectedId={null} onSelectProduct={() => {}} {...props} />
    </QueryClientProvider>,
  );
}

describe('DecisionBoard', () => {
  it('renders briefing and product rows with name + code', async () => {
    renderBoard();
    expect(await screen.findByText(/重点关注/)).toBeInTheDocument();
    expect(await screen.findByText('电子元件')).toBeInTheDocument();
    expect(screen.getByText('product_d')).toBeInTheDocument();
    expect(screen.getByText('材料B')).toBeInTheDocument();
    expect(screen.getByText('建议备货')).toBeInTheDocument();
    expect(screen.getByText('建议观望')).toBeInTheDocument();
    expect(screen.getByText('68%')).toBeInTheDocument();
  });

  it('row click fires onSelectProduct', async () => {
    const onSelect = vi.fn();
    renderBoard({ onSelectProduct: onSelect });
    const row = await screen.findByText('电子元件');
    fireEvent.click(row.closest('tr'));
    expect(onSelect).toHaveBeenCalledWith('product_d');
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/frontend && node_modules/.bin/vitest run src/pages/MarketDashboard/components/__tests__/DecisionBoard.test.jsx`
Expected: FAIL with `Cannot find module '../DecisionBoard'`

- [ ] **Step 3: Add API client method**

In `frontend/src/api/marketDashboard.js`, after `forecastBaseBatch` (line 51):

```js
  decisionBoard: (group, day = 7) =>
    getJson('/api/market-dashboard/decision-board', { group, day }),
```

- [ ] **Step 4: Implement DecisionBoard.jsx**

Create `frontend/src/pages/MarketDashboard/components/DecisionBoard.jsx`:

```jsx
// DecisionBoard — A+B hybrid: decision matrix (all products at once) with
// expandable rows revealing a mini forecast chart + full AI narrative.
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Sparkles, ChevronDown } from 'lucide-react';
import { marketDashboardApi } from '@/api/marketDashboard';

const ACTION_STYLES = {
  buy: 'bg-green-100 text-green-700 border-green-200 dark:bg-green-900/40 dark:text-green-300 dark:border-green-800',
  sell: 'bg-red-100 text-red-700 border-red-200 dark:bg-red-900/40 dark:text-red-300 dark:border-red-800',
  hold: 'bg-blue-100 text-blue-700 border-blue-200 dark:bg-blue-900/40 dark:text-blue-300 dark:border-blue-800',
  watch: 'bg-amber-100 text-amber-700 border-amber-200 dark:bg-amber-900/40 dark:text-amber-300 dark:border-amber-800',
};

const TRUST_DOTS = {
  green: 'bg-green-500',
  yellow: 'bg-yellow-500',
  orange: 'bg-orange-500',
  red: 'bg-red-500',
};

function fmtPrice(v) {
  if (v == null || Number.isNaN(Number(v))) return '—';
  return Number(v).toLocaleString('en-US', { maximumFractionDigits: 0 });
}

function fmtPctFraction(v) {
  // 0.029 → "+2.9%"
  if (v == null) return '—';
  const pct = v * 100;
  return `${pct >= 0 ? '+' : ''}${pct.toFixed(1)}%`;
}

function fmtProb(v) {
  if (v == null) return '—';
  return `${Math.round(v * 100)}%`;
}

export default function DecisionBoard({ group, selectedId, onSelectProduct }) {
  const [expandedId, setExpandedId] = useState(null);

  const { data, isLoading, isError } = useQuery({
    queryKey: ['market-dashboard', 'decision-board', group],
    queryFn: () => marketDashboardApi.decisionBoard(group, 7),
    staleTime: 60_000,
  });

  if (isLoading) {
    return (
      <div className="mx-6 mt-4 rounded-lg border border-slate-200 bg-white px-4 py-3 text-sm text-slate-500 dark:border-slate-700 dark:bg-slate-900">
        AI 决策看板加载中…
      </div>
    );
  }
  // Board is additive — never break the page on error.
  if (isError || !data || !Array.isArray(data.products) || !data.products.length) {
    return null;
  }

  return (
    <div className="mx-6 mt-4 overflow-hidden rounded-lg border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900">
      <div className="border-b border-slate-100 px-4 py-3 dark:border-slate-800">
        <div className="flex items-center gap-2">
          <Sparkles className="h-4 w-4 text-violet-500" />
          <h2 className="text-sm font-semibold text-slate-800 dark:text-slate-100">
            下周决策看板 · AI Decision Board
          </h2>
          <span className="text-xs text-slate-400">未来 {data.day} 天</span>
        </div>
        <p className="mt-1 text-sm leading-relaxed text-slate-600 dark:text-slate-300">
          {data.briefing_zh}
        </p>
      </div>

      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-slate-100 text-left text-xs text-slate-400 dark:border-slate-800">
            <th className="px-4 py-2 font-medium">物料</th>
            <th className="px-2 py-2 font-medium text-right">现价</th>
            <th className="px-2 py-2 font-medium text-right">7日预测</th>
            <th className="px-2 py-2 font-medium text-right">预期涨跌</th>
            <th className="px-2 py-2 font-medium text-right">上涨概率</th>
            <th className="px-2 py-2 font-medium">AI建议</th>
            <th className="px-2 py-2 font-medium">可靠度</th>
            <th className="px-2 py-2 font-medium">AI 简评</th>
            <th className="w-8 px-2 py-2" />
          </tr>
        </thead>
        <tbody>
          {data.products.map((p) => {
            const isSelected = p.id === selectedId;
            const isExpanded = p.id === expandedId;
            return (
              <DecisionRow
                key={p.id}
                p={p}
                day={data.day}
                isSelected={isSelected}
                isExpanded={isExpanded}
                onSelect={() => onSelectProduct?.(p.id)}
                onToggle={() => setExpandedId(isExpanded ? null : p.id)}
              />
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function DecisionRow({ p, day, isSelected, isExpanded, onSelect, onToggle }) {
  const chgCls =
    p.expected_change_pct == null
      ? 'text-slate-400'
      : p.expected_change_pct >= 0
        ? 'text-red-600 dark:text-red-400'   // 涨=red (CN convention)
        : 'text-green-600 dark:text-green-400';
  return (
    <>
      <tr
        onClick={onSelect}
        className={`cursor-pointer border-b border-slate-50 transition-colors hover:bg-slate-50 dark:border-slate-800/60 dark:hover:bg-slate-800/40 ${
          isSelected ? 'bg-slate-50 dark:bg-slate-800/50' : ''
        }`}
      >
        <td className="px-4 py-2.5">
          <div className="font-medium text-slate-800 dark:text-slate-100">{p.name_zh}</div>
          <span className="rounded bg-slate-100 px-1 font-mono text-[10px] text-slate-500 dark:bg-slate-800 dark:text-slate-400">
            {p.code}
          </span>
        </td>
        <td className="px-2 py-2.5 text-right tabular-nums">{fmtPrice(p.current_price)}</td>
        <td className="px-2 py-2.5 text-right tabular-nums">{fmtPrice(p.forecast_base)}</td>
        <td className={`px-2 py-2.5 text-right tabular-nums ${chgCls}`}>
          {fmtPctFraction(p.expected_change_pct)}
        </td>
        <td className="px-2 py-2.5 text-right tabular-nums">{fmtProb(p.p_rise)}</td>
        <td className="px-2 py-2.5">
          <span className={`inline-block rounded-full border px-2 py-0.5 text-xs font-medium ${ACTION_STYLES[p.action] || ACTION_STYLES.watch}`}>
            {p.action_label_zh}
          </span>
        </td>
        <td className="px-2 py-2.5">
          <span className="inline-flex items-center gap-1.5 text-xs text-slate-500 dark:text-slate-400">
            <span className={`h-2 w-2 rounded-full ${TRUST_DOTS[p.trust_color] || 'bg-slate-300'}`} />
            {p.trust_label_zh || '—'}
          </span>
        </td>
        <td className="max-w-[220px] truncate px-2 py-2.5 text-xs text-slate-500 dark:text-slate-400">
          {p.one_liner_zh}
        </td>
        <td className="px-2 py-2.5">
          <button
            type="button"
            aria-label={`展开 ${p.name_zh}`}
            onClick={(e) => { e.stopPropagation(); onToggle(); }}
            className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-600 dark:hover:bg-slate-800"
          >
            <ChevronDown className={`h-4 w-4 transition-transform ${isExpanded ? 'rotate-180' : ''}`} />
          </button>
        </td>
      </tr>
      {isExpanded && (
        <tr className="border-b border-slate-100 dark:border-slate-800">
          <td colSpan={9} className="bg-slate-50/60 px-4 py-3 dark:bg-slate-800/30">
            <ExpandedCard p={p} day={day} />
          </td>
        </tr>
      )}
    </>
  );
}
```

(`ExpandedCard` is added in Task 5 — for this task, temporarily render `<div />` placeholder... NO — placeholders are plan failures. Instead: this task's `DecisionRow` omits the expanded row entirely; Task 5 adds it. Adjust Step 4: remove the `{isExpanded && ...}` block and the `ExpandedCard` reference for Task 4; Task 5 re-adds them.)

**Revised Step 4 note for implementer:** In `DecisionRow`, omit the `{isExpanded && (<tr>…<ExpandedCard />…</tr>)}` block and the `day` prop usage — Task 5 adds both. The `expandedId` state and chevron toggle stay (chevron just rotates in this task).

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /home/ysk2025/zhanlu_7_30/frontend && node_modules/.bin/vitest run src/pages/MarketDashboard/components/__tests__/DecisionBoard.test.jsx`
Expected: 2 PASS

- [ ] **Step 6: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30 && git add frontend/src/api/marketDashboard.js frontend/src/pages/MarketDashboard/components/DecisionBoard.jsx frontend/src/pages/MarketDashboard/components/__tests__/DecisionBoard.test.jsx && git commit -m "feat(decision-board): matrix UI with briefing, badges, zh narratives"
```

---

### Task 5: Expandable row card (mini chart + narrative + SKU table)

**Files:**
- Modify: `frontend/src/pages/MarketDashboard/components/DecisionBoard.jsx`
- Test: `frontend/src/pages/MarketDashboard/components/__tests__/DecisionBoard.test.jsx` (append)

**Interfaces:**
- Consumes: `marketDashboardApi.forecastChart(productId, 30)` → `{ points: [{date, actual, base, bull, bear, zone}] }`; `normalizeForecastPoints(rawPoints)` from `../lib/forecastChartMath` → `{ points, hasForecast }`.

- [ ] **Step 1: Write the failing test**

Append to the describe block in `DecisionBoard.test.jsx`:

```jsx
  it('chevron expands the card showing full narrative', async () => {
    renderBoard();
    const btn = await screen.findByLabelText('展开 电子元件');
    fireEvent.click(btn);
    expect(
      await screen.findByText(/电子元件现价 12,100 元\/吨/),
    ).toBeInTheDocument();
    expect(screen.getByText(/方向准确率/)).toBeInTheDocument();
    // collapse again
    fireEvent.click(btn);
    expect(screen.queryByText(/电子元件现价 12,100 元\/吨/)).not.toBeInTheDocument();
  });
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ysk2025/zhanlu_7_30/frontend && node_modules/.bin/vitest run src/pages/MarketDashboard/components/__tests__/DecisionBoard.test.jsx`
Expected: FAIL — narrative text not found (expanded card not implemented)

- [ ] **Step 3: Implement ExpandedCard**

In `DecisionBoard.jsx`:

1. Add imports at top:

```jsx
import {
  ResponsiveContainer, ComposedChart, Area, Line, XAxis, YAxis, Tooltip,
} from 'recharts';
import { normalizeForecastPoints } from '../lib/forecastChartMath';
```

2. In `DecisionRow`, add the expanded row right after the main `</tr>`:

```jsx
      {isExpanded && (
        <tr className="border-b border-slate-100 dark:border-slate-800">
          <td colSpan={9} className="bg-slate-50/60 px-4 py-3 dark:bg-slate-800/30">
            <ExpandedCard p={p} day={day} />
          </td>
        </tr>
      )}
```

(And pass `day={day}` into `DecisionRow` from the parent map — `data.day`.)

3. Append at the end of the file:

```jsx
const VOL_ZH = { NORMAL: '正常', MODERATE: '偏高', HIGH: '剧烈' };
const CONF_ZH = { high: '高', medium: '中', low: '低' };

function ExpandedCard({ p, day }) {
  const { data } = useQuery({
    queryKey: ['market-dashboard', 'forecast-chart', p.id, 30],
    queryFn: () => marketDashboardApi.forecastChart(p.id, 30),
    staleTime: 60_000,
  });
  const { points } = normalizeForecastPoints(data?.points);

  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
      <div className="h-40 rounded border border-slate-100 bg-white p-2 dark:border-slate-700 dark:bg-slate-900">
        {points.length ? (
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={points} margin={{ top: 4, right: 8, bottom: 0, left: 8 }}>
              <XAxis dataKey="date" tick={{ fontSize: 10 }} tickFormatter={(d) => String(d).slice(5)} />
              <YAxis tick={{ fontSize: 10 }} domain={['auto', 'auto']} width={50} />
              <Tooltip formatter={(v) => (v == null ? '—' : Number(v).toLocaleString())} />
              <Area type="monotone" dataKey="actual" stroke="#2563eb" fill="#bfdbfe" strokeWidth={1.5} name="实际" />
              <Line type="monotone" dataKey="base" stroke="#f59e0b" strokeDasharray="4 2" dot={false} name={`${day}日预测`} />
            </ComposedChart>
          </ResponsiveContainer>
        ) : (
          <div className="flex h-full items-center justify-center text-xs text-slate-400">
            走势图数据加载中…
          </div>
        )}
      </div>

      <div>
        <p className="text-sm leading-relaxed text-slate-700 dark:text-slate-200">
          {p.narrative_zh}
        </p>
        <div className="mt-2 grid grid-cols-2 gap-2 text-xs text-slate-500 dark:text-slate-400 sm:grid-cols-4">
          <div>方向准确率<br /><span className="font-semibold text-slate-700 dark:text-slate-200">{p.directional_accuracy == null ? '—' : `${Math.round(p.directional_accuracy * 100)}%`}</span></div>
          <div>情报事件<br /><span className="font-semibold text-slate-700 dark:text-slate-200">{p.intel_event_count} 条</span></div>
          <div>波动状态<br /><span className="font-semibold text-slate-700 dark:text-slate-200">{VOL_ZH[p.volatility_regime] || '—'}</span></div>
          <div>置信度<br /><span className="font-semibold text-slate-700 dark:text-slate-200">{CONF_ZH[p.confidence] || '低'}</span></div>
        </div>
        {p.skus?.length > 0 && (
          <table className="mt-3 w-full text-xs">
            <thead>
              <tr className="text-left text-slate-400">
                <th className="py-1 font-medium">物料代码</th>
                <th className="py-1 font-medium">名称</th>
                <th className="py-1 font-medium text-right">预测价</th>
              </tr>
            </thead>
            <tbody>
              {p.skus.map((s) => (
                <tr key={s.material_code} className="border-t border-slate-100 dark:border-slate-700">
                  <td className="py-1 font-mono">{s.material_code}</td>
                  <td className="py-1">{s.name}</td>
                  <td className="py-1 text-right tabular-nums">{fmtPrice(s.forecast_base)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ysk2025/zhanlu_7_30/frontend && node_modules/.bin/vitest run src/pages/MarketDashboard/components/__tests__/DecisionBoard.test.jsx`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30 && git add frontend/src/pages/MarketDashboard/components/DecisionBoard.jsx frontend/src/pages/MarketDashboard/components/__tests__/DecisionBoard.test.jsx && git commit -m "feat(decision-board): expandable card with mini chart, narrative, SKU table"
```

---

### Task 6: Wire into MarketDashboardPage + live verification

**Files:**
- Modify: `frontend/src/pages/MarketDashboard/MarketDashboardPage.jsx` (import line ~9 + render after `<ObservedDataStrip />`, line 148)

**Interfaces:**
- Consumes: `<DecisionBoard group selectedId onSelectProduct />` from Task 4/5.

- [ ] **Step 1: Wire the component**

Add import (with the other component imports, line 6-10):

```jsx
import DecisionBoard from './components/DecisionBoard';
```

Render immediately after `</ObservedDataStrip>` closing tag (line 148):

```jsx
      <DecisionBoard
        group={section}
        selectedId={selectedProductId}
        onSelectProduct={setSelectedProductId}
      />
```

Note: at this point in the render flow `section` is always one of upstream/midstream/downstream (overview returned earlier at line 130), so no extra guard needed.

- [ ] **Step 2: Run the existing page test (regression check)**

Run: `cd /home/ysk2025/zhanlu_7_30/frontend && node_modules/.bin/vitest run src/pages/MarketDashboard/__tests__/MarketDashboardPage.test.jsx`
Expected: PASS (existing tests mock `marketDashboardApi` — if the mock lacks `decisionBoard`, the query throws and the board renders `null` by design; if the test file mocks the whole module factory, confirm no crash and, if needed, add `decisionBoard: vi.fn().mockRejectedValue(new Error('off'))` to that mock)

- [ ] **Step 3: Restart backend + verify endpoint live**

```bash
docker restart zhanlu-backend && sleep 20
TOKEN=$(curl -s -X POST http://localhost:5002/api/apps/local-zhanlu-app/auth/login -H 'Content-Type: application/json' -d '{"email":"admin@zhanlu.dev","password":"admin123"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
curl -s "http://localhost:5002/api/market-dashboard/decision-board?group=midstream&day=7" -H "Authorization: Bearer $TOKEN" | python3 -m json.tool | head -60
```

Expected: JSON with `briefing_zh` + 4 `products`, each with `action_label_zh`, `one_liner_zh`, `narrative_zh`.
(If the login payload shape differs, inspect the response and adjust the field names — do not proceed on a 401.)

- [ ] **Step 4: Commit**

```bash
cd /home/ysk2025/zhanlu_7_30 && git add frontend/src/pages/MarketDashboard/MarketDashboardPage.jsx && git commit -m "feat(decision-board): wire DecisionBoard into product-section pages"
```

- [ ] **Step 5: Visual check**

Frontend dist is bind-mounted (auto-rebuild on save; `cd frontend && npm run build` to force). Open `http://10.10.10.51:8080/market-dashboard/midstream` via `preview_url`, confirm: briefing card on top, 4-row matrix, row click swaps the chart below, chevron expands the card with mini chart + narrative, dark mode OK.

---

## Self-Review

**Spec coverage:** briefing card ✓ (T4), matrix columns incl. name+code ✓ (T4), row→drill ✓ (T4+T6), expandable card w/ mini chart + narrative + stats ✓ (T5), SKU table ✓ (T5), backend endpoint + templates ✓ (T1-T3), missing-data fallbacks ✓ (T1/T2 tests), zh narratives ✓ (T1), dark mode ✓ (T4 classes), tests ✓ (every task), docker restart + curl verify ✓ (T6).

**Placeholder scan:** Task 4 contains a deliberate scope note (expanded row added in T5, not T4) with exact instructions — no "TBD"/"handle later" items remain. LLM narratives, day selector, export are explicitly out of scope per spec.

**Type consistency:** `DecisionBoardProduct` field names identical across schema (T1), service construction (T2), router fixture (T3), frontend fixture + component (T4/T5). `build_decision_board(group, day, db)` signature matches router call `dbs.build_decision_board(group=group, day=day)` and test monkeypatch `lambda group, day: fake`. Fraction-vs-percent semantics documented in Global Constraints and schema comments.
