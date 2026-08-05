"""Pydantic response shapes for the EDIA market dashboard mirror.

Field names mirror EDIA's frontend interfaces
(MarketSnapshotItem, RegionalPriceData, etc.) so the ported EDIA React
components can consume the payload without remapping.
"""
from typing import Optional
from pydantic import BaseModel, Field


class MarketSnapshot(BaseModel):
    id: str
    name: str
    group: str
    price: Optional[float] = None
    date: Optional[str] = None
    unit: str = "CNY/mt"
    change_pct: Optional[float] = None
    weekly_change_pct: Optional[float] = None
    daily_gap_value: Optional[float] = None
    weekly_gap_value: Optional[float] = None
    daily_reference_date: Optional[str] = None
    weekly_reference_date: Optional[str] = None
    data_source_detail: Optional[str] = None
    note: Optional[str] = None  # set when mapping is missing


class PriceHistoryPoint(BaseModel):
    date: str
    price: float
    volume: Optional[float] = None


class RegionalPriceRegion(BaseModel):
    price_type: str
    label: str
    is_internal: bool = False
    price: float
    change_pct: Optional[float] = None
    date: str
    days_stale: int = 0


class RegionalPriceData(BaseModel):
    product_id: str
    regions: list[RegionalPriceRegion]
    spread_cny: Optional[float] = None  # v1: not computed; left null
    spread_level: Optional[str] = None  # v1: not computed
    price_realization_pct: Optional[float] = None  # v1: not computed
    note: Optional[str] = None


class NewsListItem(BaseModel):
    """v1 placeholder — real news requires EDIA's AI event pipeline."""
    event_id: str
    headline: str
    detected_at: str
    source_url: Optional[str] = None
    summary: Optional[str] = None
    group: Optional[str] = None


class NewsListResponse(BaseModel):
    items: list[NewsListItem] = Field(default_factory=list)
    note: Optional[str] = None


class EnabledResponse(BaseModel):
    enabled: bool
    reachable: bool
    reason: Optional[str] = None  # "not_configured" | "configured_unreachable" | "ok"


# ── Phase 7 (verbatim port): batch + new endpoints ─────────────────

class RegionalBatchResponse(BaseModel):
    """Batch response for /regional-batch — one RegionalPriceData per product."""
    items: dict[str, RegionalPriceData] = Field(default_factory=dict)
    errors: dict[str, str] = Field(default_factory=dict)


# ── Phase 6: Forecast overlay metadata (intelligence + policy) ──────

class IntelligenceOverlayMeta(BaseModel):
    """Per-product intelligence overlay snapshot."""
    active_event_count: int = 0
    bias_direction: str = "neutral"     # "up" | "down" | "neutral"
    impact_tier: Optional[str] = None   # "tier1" | "tier2" | "tier3"
    confidence: Optional[float] = None
    summary: Optional[str] = None


class PolicyOverlayMeta(BaseModel):
    """Per-product policy service snapshot."""
    volatility_regime: Optional[str] = None  # "NORMAL" | "MODERATE" | "HIGH"
    vol_multiplier: Optional[float] = None
    bias_pct: Optional[float] = None
    diagnosis_bias: Optional[float] = None
    applied_factor: Optional[float] = None


class TrustTierMeta(BaseModel):
    """User-facing trust tier for a product's forecast (Phase B)."""
    tier: str  # "high" | "medium" | "directional" | "low"
    badge_color: str  # "green" | "yellow" | "orange" | "red"
    badge_label_zh: str
    badge_label_en: str
    reason_zh: Optional[str] = None
    reason_en: Optional[str] = None


class ProbabilityOverlayMeta(BaseModel):
    """Price-change probability for one horizon (Phase D)."""
    horizon: int
    p_rise: float  # P(price[T+h] > price[T])
    p_rise_gt: dict[str, float] = {}  # threshold% -> P(rise > threshold)
    expected_change_pct: Optional[float] = None


class DirectionalEdgeMeta(BaseModel):
    """Phase E1 directional classifier result (per horizon)."""
    horizon: int
    accuracy: float  # walk-forward CV accuracy [0,1]
    n_test: int      # number of test samples used
    p_value: float   # binomial p-value against null p=0.5
    status: str      # "edge" | "no_edge"


class DecisionMeta(BaseModel):
    """Phase E2 buy/hold/sell decision (per horizon)."""
    horizon: int
    action: str              # "buy" | "hold" | "sell" | "watch"
    confidence: str          # "high" | "medium" | "low"
    rationale: str


class ForecastOverlayMeta(BaseModel):
    """Combined per-product overlay metadata bundle."""
    product_id: str
    intelligence: Optional[IntelligenceOverlayMeta] = None
    policy: Optional[PolicyOverlayMeta] = None
    trust_tier: Optional[TrustTierMeta] = None
    probability: Optional[list[ProbabilityOverlayMeta]] = None
    directional: Optional[list[DirectionalEdgeMeta]] = None  # Phase E1
    decision: Optional[list[DecisionMeta]] = None             # Phase E2


class SkuForecastMeta(BaseModel):
    """Metadata for one SKU (material_code) within a product family."""
    material_code: str
    target_id: str
    name: str
    report_order: int
    is_primary: bool = False
    row_count: Optional[int] = None
    forecast_base: Optional[float] = None
    forecast_confidence: Optional[float] = None


class SkuBrief(BaseModel):
    """Compact SKU row for the decision board expanded card."""
    material_code: str
    name: str
    forecast_base: Optional[float] = None


class AnalystBriefMeta(BaseModel):
    """Structured AI 简评 — evidence-grounded analyst brief."""
    summary_zh: str = ""
    why_price_zh: str = ""
    why_action_zh: str = ""
    watch_triggers_zh: list[str] = []
    risk_zh: str = ""
    source: str = "template"           # "llm" | "template"
    day: Optional[int] = None
    generated_at: Optional[str] = None


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
    analyst_brief: Optional[AnalystBriefMeta] = None
    skus: list[SkuBrief] = []


class DecisionBoardResponse(BaseModel):
    """GET /market-dashboard/decision-board payload."""
    group: str
    day: int
    generated_at: str
    briefing_zh: str
    products: list[DecisionBoardProduct] = []


class ForecastBaseBatchResponse(BaseModel):
    """Batch response for /forecast-base-batch — 7-day-ahead forecast prices.

    Phase 6: includes overlay metadata per product.
    Phase 7: includes SKU drill-down data for ERP products.
    """
    day: int
    base_by_product: dict[str, Optional[float]] = Field(default_factory=dict)
    overlay_by_product: dict[str, Optional[ForecastOverlayMeta]] = Field(default_factory=dict)
    sku_forecasts_by_product: dict[str, list[SkuForecastMeta]] = Field(default_factory=dict)


class FxRateResponse(BaseModel):
    """USD-CNY FX rate (v1: static)."""
    price: Optional[float]
    unit: str
    as_of_date: Optional[str]
    series_product_id: str
    series_price_type: str
    data_source: str
    available: bool


class EventItem(BaseModel):
    """An intelligence event from EDIA's /api/events/latest (verbatim shape)."""
    event_id: str
    headline: str
    detected_at: str
    source_url: Optional[str] = None
    event_type: Optional[str] = None
    direction: Optional[str] = None
    impact_magnitude: Optional[float] = None
    certainty: Optional[str] = None
    source_credibility: Optional[str] = None
    usefulness_score: Optional[float] = None
    relevance_score: Optional[float] = None
    affected_commodities: list[str] = Field(default_factory=list)
    causal_chain_hint: Optional[str] = None
    raw_quote: Optional[str] = None
    full_text: Optional[str] = None
    relevance_to_c5_c9: Optional[str] = None
    source_label: Optional[str] = None
    summary: Optional[str] = None
    quality_tier: Optional[str] = None
    time_horizon: Optional[str] = None
    geographic_scope: Optional[str] = None
    key_entities: list[str] = Field(default_factory=list)


class EventsResponse(BaseModel):
    """Response for /events/latest."""
    events: list[EventItem] = Field(default_factory=list)
    count: int = 0


class OverviewBundle(BaseModel):
    """One-shot payload for the chemical market overview page.

    Returns everything the EDIA overview dashboard needs in a single
    round-trip so the page renders without waiting on a waterfall of
    separate /snapshot /regional-batch /forecast-base-batch /fx calls.

    Phase 6: includes forecast_overlay_by_product.
    """
    snapshot: list[MarketSnapshot] = Field(default_factory=list)
    regional: dict[str, RegionalPriceData] = Field(default_factory=dict)
    regional_errors: dict[str, str] = Field(default_factory=dict)
    forecast_day: int = 7
    forecast_base_by_product: dict[str, Optional[float]] = Field(default_factory=dict)
    forecast_overlay_by_product: dict[str, Optional[ForecastOverlayMeta]] = Field(default_factory=dict)  # Phase 6
    fx: FxRateResponse
    cached: bool = False  # True when this response came from the in-memory cache
    cache_age_ms: int = 0  # how old the cached payload is (0 when freshly built)


# ── Forecast chart (past + forecast merged timeline) ────────────────

class ForecastChartPoint(BaseModel):
    """Single date point on the merged past+forecast chart."""
    date: str
    actual: Optional[float] = None       # past actual price
    previous_ai: Optional[float] = None  # previous AI rec price (step-function)
    prev_ai_low: Optional[float] = None  # previous AI bear (range band lower)
    prev_ai_high: Optional[float] = None # previous AI bull (range band upper)
    exec_in: Optional[float] = None      # actual price when executed IN range (green dot)
    exec_out: Optional[float] = None     # actual price when executed OUTSIDE range (red dot)
    base: Optional[float] = None         # forecast base
    bull: Optional[float] = None         # forecast bull (upper)
    bear: Optional[float] = None         # forecast bear (lower)
    zone: str = "past"                   # "past" | "today" | "forecast"


class ForecastAnalysisMeta(BaseModel):
    """AI 分析依据 — structured reasoning behind the forecast.

    Powers the chart-page collapsible "AI 分析依据" panel and is reused
    by the decision-board narrative builder. All fields optional so a
    product with thin data degrades gracefully.
    """
    # Trend (computed from actual price history)
    chg_7d_pct: Optional[float] = None
    chg_30d_pct: Optional[float] = None
    ma30: Optional[float] = None
    above_ma30: Optional[bool] = None
    # Uncertainty (from latest run bull/bear)
    bull_price: Optional[float] = None
    bear_price: Optional[float] = None
    spread_pct: Optional[float] = None
    # Model ensemble (from run.model_detail)
    model_count: int = 0
    model_names: list[str] = Field(default_factory=list)
    ensemble_mape: Optional[float] = None
    naive_mape: Optional[float] = None
    beats_naive: Optional[bool] = None
    below_naive_baseline: Optional[bool] = None
    # Directional classifier + decision rationale
    directional_accuracy: Optional[float] = None
    directional_status: Optional[str] = None     # "edge" | "no_edge"
    directional_n_test: Optional[int] = None
    rationale: Optional[str] = None              # decision rationale (en)
    # Pre-built Chinese narrative fragments
    basis_zh: str = ""                           # 【预测依据】
    action_logic_zh: str = ""                    # 【建议逻辑】


class ForecastChartResponse(BaseModel):
    """Full forecast chart payload for a product.

    Phase 6: includes overlay metadata (intelligence + policy).
    """
    product_id: str
    points: list[ForecastChartPoint] = Field(default_factory=list)
    data_quality: str = "good"           # "good" | "insufficient"
    as_of_date: Optional[str] = None     # forecast run date
    previous_ai_date: Optional[str] = None
    product_name: str = ""
    executed_in_range_count: int = 0     # count of past dates inside AI range
    executed_outside_range_count: int = 0  # count of past dates outside AI range
    overlay: Optional[ForecastOverlayMeta] = None  # Phase 6
    analysis: Optional[ForecastAnalysisMeta] = None  # AI 分析依据


# ── Price transmission chain (3-tier upstream flow) ─────────────────

class PriceChainNode(BaseModel):
    """One product node in the price transmission chain."""
    product_id: str
    name_en: str
    name_zh: str
    current_price: Optional[float] = None
    change_pct_30d: Optional[float] = None
    sparkline: list[float] = Field(default_factory=list)
    percentile: Optional[float] = None
    percentile_remark: Optional[str] = None
    erp_exec_price: Optional[float] = None
    trend_start_date: Optional[str] = None
    trend_end_date: Optional[str] = None
    group: str = "upstream"


class PriceChainResponse(BaseModel):
    """3-tier price transmission chain."""
    tiers: list[list[PriceChainNode]] = Field(default_factory=list)
    as_of_date: Optional[str] = None

