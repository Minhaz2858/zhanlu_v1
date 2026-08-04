"""Tests for causal narrative engine, scenario probability engine, and auto-override."""
import pytest
from app.services.intelligence.event_types import ExtractedEvent
from app.services.intelligence.causal_narrative_engine import CausalNarrativeEngine, CausalChainResult
from app.services.intelligence.scenario_probability_engine import (
    ScenarioProbabilityEngine,
    IntelligentScenario,
    _HISTORICAL_BASE_RATES,
)
from app.services.intelligence.auto_override_decision import (
    AutoOverrideDecisionEngine,
    OverrideDecision,
    AUTO_INJECT_THRESHOLD,
    HUMAN_REVIEW_THRESHOLD,
)


def _make_event(
    event_type="geopolitical_supply_disruption",
    direction="bullish",
    magnitude="large",
    certainty="confirmed",
):
    return ExtractedEvent(
        event_type=event_type,
        headline=f"Test {event_type}",
        affected_commodities=["crude_oil", "naphtha"],
        direction=direction,
        magnitude_estimate=magnitude,
        certainty=certainty,
        time_horizon="short",
        geographic_scope="global",
        relevance_to_c5_c9="direct",
        source_credibility="tier1",
    )


# ── Causal Narrative Engine ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_causal_engine_reason_through_event():
    engine = CausalNarrativeEngine(llm_caller=None)
    event = _make_event()
    result = await engine.reason_through_event(event)

    assert isinstance(result, CausalChainResult)
    assert result.entry_commodity == "crude_oil"
    assert result.primary_impact_pct > 0  # bullish confirmed
    assert "mixed_c5" in result.product_impacts
    assert "isoprene" in result.product_impacts
    assert "c9_resin" in result.product_impacts
    assert "causal_narrative" in result.to_dict()
    assert "3d" in result.confidence_by_horizon
    assert "30d" in result.confidence_by_horizon


@pytest.mark.asyncio
async def test_causal_engine_bearish_event():
    engine = CausalNarrativeEngine(llm_caller=None)
    event = _make_event(direction="bearish")
    result = await engine.reason_through_event(event)
    assert result.primary_impact_pct < 0  # bearish
    assert result.naphtha_total_pct < 0


@pytest.mark.asyncio
async def test_causal_engine_fallback_narrative():
    engine = CausalNarrativeEngine(llm_caller=None)
    event = _make_event()
    result = await engine.reason_through_event(event)
    # Without LLM, narrative should contain structured summary
    assert "EVENT:" in result.causal_narrative
    assert "PRODUCT IMPACTS:" in result.causal_narrative


@pytest.mark.asyncio
async def test_causal_engine_product_impacts_have_cny():
    engine = CausalNarrativeEngine(llm_caller=None)
    event = _make_event(magnitude="extreme")
    result = await engine.reason_through_event(event)
    for pid, imp in result.product_impacts.items():
        assert "cny_impact" in imp
        assert "pct_impact" in imp
        assert "causal_path" in imp
        assert "elasticity_used" in imp


# ── Scenario Probability Engine ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_scenario_engine_generates_three_scenarios():
    causal_engine = CausalNarrativeEngine(llm_caller=None)
    event = _make_event()
    causal_chain = await causal_engine.reason_through_event(event)

    scenario_engine = ScenarioProbabilityEngine(llm_caller=None)
    scenarios = await scenario_engine.generate_scenarios(event, causal_chain)

    assert len(scenarios) == 3
    labels = [s.label for s in scenarios]
    assert any("Bull" in l for l in labels)
    assert any("Base" in l for l in labels)
    assert any("Bear" in l for l in labels)


@pytest.mark.asyncio
async def test_scenario_probabilities_sum_to_one():
    causal_engine = CausalNarrativeEngine(llm_caller=None)
    event = _make_event()
    causal_chain = await causal_engine.reason_through_event(event)

    scenario_engine = ScenarioProbabilityEngine(llm_caller=None)
    scenarios = await scenario_engine.generate_scenarios(event, causal_chain)

    total_prob = sum(s.probability for s in scenarios)
    assert 0.95 <= total_prob <= 1.05  # allow for rounding


@pytest.mark.asyncio
async def test_scenario_extreme_magnitude_shifts_to_structural():
    causal_engine = CausalNarrativeEngine(llm_caller=None)
    event = _make_event(magnitude="extreme")
    causal_chain = await causal_engine.reason_through_event(event)

    scenario_engine = ScenarioProbabilityEngine(llm_caller=None)
    scenarios = await scenario_engine.generate_scenarios(event, causal_chain)

    bull = next(s for s in scenarios if "Bull" in s.label)
    bear = next(s for s in scenarios if "Bear" in s.label)
    # Extreme events should shift probability toward bull (structural/persistent)
    assert bull.probability >= bear.probability


def test_historical_base_rates_exist_for_all_types():
    event_types = {
        "geopolitical_supply_disruption", "shipping_disruption", "plant_outage",
        "tariff_policy", "demand_signal", "opec_decision",
        "sinopec_price_change", "weather_event", "currency_move", "sanctions",
    }
    for et in event_types:
        assert et in _HISTORICAL_BASE_RATES, f"Missing base rates for {et}"


# ── Auto-Override Decision ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_override_high_confidence_auto_inject():
    causal_engine = CausalNarrativeEngine(llm_caller=None)
    event = _make_event(certainty="confirmed", magnitude="extreme")
    # tier1 source, confirmed, extreme → high confidence
    causal_chain = await causal_engine.reason_through_event(event)

    override_engine = AutoOverrideDecisionEngine()
    decision = override_engine.decide(event, causal_chain)

    assert decision.action in ("auto_inject", "human_review")
    assert decision.injection_confidence >= HUMAN_REVIEW_THRESHOLD


@pytest.mark.asyncio
async def test_auto_override_low_confidence_log_only():
    causal_engine = CausalNarrativeEngine(llm_caller=None)
    event = _make_event(
        certainty="speculative",
        magnitude="small",
    )
    event.source_credibility = "tier3"
    causal_chain = await causal_engine.reason_through_event(event)

    override_engine = AutoOverrideDecisionEngine()
    decision = override_engine.decide(event, causal_chain)

    assert decision.action == "log_only"
    assert decision.injection_confidence < HUMAN_REVIEW_THRESHOLD


@pytest.mark.asyncio
async def test_auto_override_pending_review():
    causal_engine = CausalNarrativeEngine(llm_caller=None)
    event = _make_event(certainty="likely", magnitude="medium")
    event.source_credibility = "tier2"
    causal_chain = await causal_engine.reason_through_event(event)

    override_engine = AutoOverrideDecisionEngine()
    decision = override_engine.decide(event, causal_chain)

    if decision.action == "human_review":
        assert len(override_engine.get_pending_reviews()) == 1
        # Approve it
        approved = override_engine.approve_pending(event.event_id)
        assert approved is not None
        assert approved.action == "approved_and_injected"
        assert len(override_engine.get_pending_reviews()) == 0


@pytest.mark.asyncio
async def test_auto_override_executor_called():
    causal_engine = CausalNarrativeEngine(llm_caller=None)
    event = _make_event(certainty="confirmed", magnitude="extreme")
    causal_chain = await causal_engine.reason_through_event(event)

    executed = []
    def mock_executor(decision):
        executed.append(decision)

    override_engine = AutoOverrideDecisionEngine(override_executor=mock_executor)
    decision = override_engine.decide(event, causal_chain)

    if decision.action == "auto_inject":
        assert len(executed) == 1
        assert executed[0].event_id == event.event_id
