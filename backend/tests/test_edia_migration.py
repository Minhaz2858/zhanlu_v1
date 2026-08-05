"""Tests for EDIA migration (Section 6).

Validates: migration script correctness (product map, seasonal rules,
causal elasticities, MAPE baselines, below-naive flags, pack JSON),
feature flags (default False), agent definitions (ecisco_bi_assistant,
perception_agent, rag_research_agent, diagnosis_agent, pricing_agent,
forecast_agent, report_agent in BUILTIN_AGENTS), and the ecisco_bi_assistant
system agent config.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from app.database import Base, engine, SessionLocal
from app.models.forecasting import (
    ForecastTarget,
    ForecastRun,
    ForecastAccuracyLog,
    ForecastBusinessRule,
    DomainPackInstall,
)
from app.config import settings
from app.services.agent_definitions import BUILTIN_AGENTS
from app.services.system_agents import _build_system_agent_configs

# ── Migration script constants (re-imported for testing) ─────────────────
# We test the migration script's data constants directly to validate
# that they match EDIA's source code.
import scripts.migrate_edia_to_zhanlu as mig

# ── Fixtures ──────────────────────────────────────────────────────────

_NEEDED_TABLES = [
    ForecastTarget.__table__,
    ForecastBusinessRule.__table__,
    ForecastAccuracyLog.__table__,
    DomainPackInstall.__table__,
]


@pytest.fixture(autouse=True)
def _migrate_schema():
    """Drop and recreate needed tables before each test for isolation."""
    Base.metadata.drop_all(engine, tables=_NEEDED_TABLES)
    Base.metadata.create_all(engine, tables=_NEEDED_TABLES)
    yield
    Base.metadata.drop_all(engine, tables=_NEEDED_TABLES)


@pytest.fixture
def db():
    """Provide a transient DB session that is rolled back after each test."""
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


# ── Product Map Tests ────────────────────────────────────────────────

class TestProductMap:
    """Validate _PRODUCT_MAP conversion to canonical targets."""

    def test_canonical_product_count(self):
        """13 canonical products after alias collapse (excluding aliases)."""
        canonical_keys = set()
        for edia_key, (canonical, _) in mig._PRODUCT_MAP.items():
            canonical_keys.add(canonical)
        assert len(canonical_keys) == 16  # 13 weekly + 3 model-only (crude, butadiene, sbs)

    def test_weekly_report_products(self):
        """13 products in weekly report (excluding crude_oil, butadiene, sbs)."""
        weekly_products = {
            k for k, info in mig._CANONICAL_PRODUCTS.items()
            if info["include_in_weekly_report"]
        }
        assert len(weekly_products) == 13

    def test_model_only_products(self):
        """3 products excluded from weekly report."""
        model_only = {
            k for k, info in mig._CANONICAL_PRODUCTS.items()
            if not info["include_in_weekly_report"]
        }
        assert model_only == {"crude_oil", "butadiene", "sbs"}

    def test_alias_collisions(self):
        """4 alias pairs collapse to their canonical product."""
        alias_pairs = {
            ("mixed_c5", "cracked_c5"),
            ("mixed_c9", "cracked_c9"),
            ("sis_rubber", "sis"),
            ("c9_resin", "hydrogenated_c9"),
        }
        for alias, canonical in alias_pairs:
            assert mig._PRODUCT_MAP[alias][0] == canonical

    def test_aliases_stored_on_canonical(self):
        """Each canonical product stores its aliases list."""
        assert mig._CANONICAL_PRODUCTS["cracked_c5"]["aliases"] == ["mixed_c5"]
        assert mig._CANONICAL_PRODUCTS["cracked_c9"]["aliases"] == ["mixed_c9"]
        assert mig._CANONICAL_PRODUCTS["sis"]["aliases"] == ["sis_rubber"]
        assert mig._CANONICAL_PRODUCTS["hydrogenated_c9"]["aliases"] == ["c9_resin"]

    def test_no_alias_products(self):
        """Products without aliases have empty alias list."""
        no_alias_products = [
            "naphtha", "isoprene", "piperylene", "dcpd",
            "raffinate_c5", "blowing_agent", "c5_resin",
            "trimethylbenzene", "styrene",
        ]
        for product in no_alias_products:
            assert mig._CANONICAL_PRODUCTS[product]["aliases"] == []

    def test_price_sources(self):
        """Each canonical product has a price_source hint."""
        for product_key, info in mig._CANONICAL_PRODUCTS.items():
            assert "price_source" in info
            assert info["price_source"] is not None

    def test_naphtha_price_source(self):
        """Naphtha has japan_cif price source."""
        assert mig._CANONICAL_PRODUCTS["naphtha"]["price_source"] == "japan_cif"

    def test_report_order_values(self):
        """Weekly report products have sequential report_order 1-13."""
        weekly_orders = [
            (k, info["report_order"]) for k, info in mig._CANONICAL_PRODUCTS.items()
            if info["include_in_weekly_report"]
        ]
        orders = sorted(weekly_orders, key=lambda x: x[1])
        assert [o for _, o in orders] == list(range(1, 14))


# ── Seasonal Rules Tests ─────────────────────────────────────────────

class TestSeasonalRules:
    """Validate _SEASONAL_ADJ_MAP structure and values."""

    def test_products_with_seasonal_rules(self):
        """7 products have seasonal adjustment rules."""
        assert len(mig._SEASONAL_ADJ_MAP) == 7

    def test_c5_resin_seasonal(self):
        """c5_resin has 2 adjustment groups: -2.5 in winter, +1.0 in summer."""
        rules = mig._SEASONAL_ADJ_MAP["c5_resin"]
        assert len(rules) == 2
        assert rules[0]["adjustment_pct"] == -2.5
        assert rules[0]["months"] == [11, 12, 1, 2]
        assert rules[1]["adjustment_pct"] == 1.0
        assert rules[1]["months"] == [3, 4, 5, 6, 7, 8, 9]

    def test_dcpd_seasonal(self):
        """dcpd has 2 adjustment groups: -3.0 in winter, +1.5 in summer."""
        rules = mig._SEASONAL_ADJ_MAP["dcpd"]
        assert rules[0]["adjustment_pct"] == -3.0
        assert rules[1]["adjustment_pct"] == 1.5

    def test_isoprene_seasonal(self):
        """isoprene has +0.8 in Feb-Mar."""
        rules = mig._SEASONAL_ADJ_MAP["isoprene"]
        assert rules[0]["months"] == [2, 3]
        assert rules[0]["adjustment_pct"] == 0.8

    def test_cracked_c5_seasonal(self):
        """cracked_c5 has -1.0 in Jan-Feb, +0.5 in Mar-May."""
        rules = mig._SEASONAL_ADJ_MAP["cracked_c5"]
        assert len(rules) == 2
        assert rules[0]["months"] == [1, 2]
        assert rules[0]["adjustment_pct"] == -1.0
        assert rules[1]["months"] == [3, 4, 5]
        assert rules[1]["adjustment_pct"] == 0.5

    def test_piperylene_seasonal(self):
        """piperylene has +1.2 in Feb-Mar."""
        rules = mig._SEASONAL_ADJ_MAP["piperylene"]
        assert rules[0]["months"] == [2, 3]
        assert rules[0]["adjustment_pct"] == 1.2

    def test_styrene_seasonal(self):
        """styrene has -1.5 in Nov-Dec-Jan."""
        rules = mig._SEASONAL_ADJ_MAP["styrene"]
        assert rules[0]["months"] == [11, 12, 1]
        assert rules[0]["adjustment_pct"] == -1.5

    def test_butadiene_seasonal(self):
        """butadiene has -2.0 in winter months."""
        rules = mig._SEASONAL_ADJ_MAP["butadiene"]
        assert rules[0]["months"] == [11, 12, 1, 2]
        assert rules[0]["adjustment_pct"] == -2.0


# ── Causal Elasticities Tests ─────────────────────────────────────────

class TestCausalElasticities:
    """Validate _ELASTICITIES and cascade factors."""

    def test_elasticity_count(self):
        """17 products have elasticity entries."""
        assert len(mig._ELASTICITIES) == 17

    def test_naphtha_elasticity(self):
        """naphtha elasticity is 1.00 (baseline)."""
        assert mig._ELASTICITIES["naphtha"] == 1.00

    def test_cracked_c5_elasticity(self):
        """cracked_c5 elasticity is 0.75."""
        assert mig._ELASTICITIES["cracked_c5"] == 0.75

    def test_cascade_factor(self):
        """naphtha→C5 cascade factor is 0.82."""
        assert mig._NAPHTHA_C5_CASCADE == 0.82

    def test_crude_naphtha_cascade(self):
        """crude→naphtha factor is 1.35."""
        assert mig._CRUDE_NAPHTHA_CASCADE == 1.35

    def test_alias_elasticities_match_canonical(self):
        """Alias products inherit the same elasticity as their canonical."""
        assert mig._ELASTICITIES["mixed_c5"] == mig._ELASTICITIES["cracked_c5"]
        assert mig._ELASTICITIES["mixed_c9"] == mig._ELASTICITIES["cracked_c9"]
        assert mig._ELASTICITIES["sis_rubber"] == mig._ELASTICITIES["sis"]
        assert mig._ELASTICITIES["c9_resin"] == mig._ELASTICITIES["hydrogenated_c9"]

    def test_all_elasticities_between_0_and_1(self):
        """All elasticity values are between 0 and 1."""
        for product, elasticity in mig._ELASTICITIES.items():
            assert 0.0 < elasticity <= 1.0


# ── Below-Naive Products Tests ─────────────────────────────────────────

class TestBelowNaiveProducts:
    """Validate _BELOW_NAIVE_PRODUCTS honesty gate."""

    def test_below_naive_count(self):
        """4 products are flagged as below-naive (from actual EDIA code)."""
        assert len(mig._BELOW_NAIVE_PRODUCTS) == 4

    def test_below_naive_members(self):
        """Actual EDIA code below-naive set: cracked_c5, cracked_c9, mixed_c9, styrene."""
        assert mig._BELOW_NAIVE_PRODUCTS == frozenset({"cracked_c5", "cracked_c9", "mixed_c9", "styrene"})

    def test_below_naive_not_design_doc(self):
        """Design doc listed different products; migration uses actual EDIA code."""
        # Design doc claimed: c5_resin, blowing_agent, cracked_c9, mixed_c9
        # Actual EDIA code: cracked_c5, cracked_c9, mixed_c9, styrene
        design_doc_set = frozenset({"c5_resin", "blowing_agent", "cracked_c9", "mixed_c9"})
        assert mig._BELOW_NAIVE_PRODUCTS != design_doc_set

    def test_mape_confirms_below_naive(self):
        """MAPE baselines confirm the 4 below-naive products have skill_vs_naive > 0."""
        for product in mig._BELOW_NAIVE_PRODUCTS:
            canonical = mig._PRODUCT_MAP.get(product, (product, ""))[0]
            baseline = mig._MAPE_BASELINES.get(canonical)
            if baseline and baseline["skill_vs_naive"] is not None:
                assert baseline["skill_vs_naive"] > 0


# ── Post-Holiday Override Tests ────────────────────────────────────────

class TestPostHolidayOverride:
    """Validate post-holiday override data."""

    def test_post_holiday_products_count(self):
        """5 products have post-holiday overrides."""
        assert len(mig._POST_HOLIDAY_PRODUCTS) == 5

    def test_post_holiday_products(self):
        """Post-holiday override applies to piperylene, c5_resin, cracked_c5, isoprene, raffinate_c5."""
        assert mig._POST_HOLIDAY_PRODUCTS == frozenset({
            "piperylene", "c5_resin", "cracked_c5", "isoprene", "raffinate_c5"
        })

    def test_post_holiday_months(self):
        """Post-holiday override applies in months 2 and 3 (Feb-Mar)."""
        assert mig._POST_HOLIDAY_MONTHS == [2, 3]

    def test_post_holiday_adjustment(self):
        """Post-holiday adjustment is +1.2%."""
        assert mig._POST_HOLIDAY_ADJUSTMENT_PCT == 1.2


# ── MAPE Baselines Tests ───────────────────────────────────────────────

class TestMAPEBaselines:
    """Validate MAPE baseline data from MAPE_BASELINE.md."""

    def test_mape_baseline_products(self):
        """MAPE baselines cover all 16 canonical products."""
        assert len(mig._MAPE_BASELINES) == 16

    def test_cracked_c5_mape(self):
        """cracked_c5 MAPE=0.0431, naive=0.0370, skill_vs_naive=+0.0061."""
        baseline = mig._MAPE_BASELINES["cracked_c5"]
        assert baseline["mape"] == 0.0431
        assert baseline["naive_mape"] == 0.0370
        assert baseline["skill_vs_naive"] == 0.0061

    def test_isoprene_skill_beats_naive(self):
        """isoprene skill_vs_naive=-0.0217 (model beats naive)."""
        baseline = mig._MAPE_BASELINES["isoprene"]
        assert baseline["skill_vs_naive"] < 0

    def test_null_mape_products(self):
        """raffinate_c5, crude_oil, butadiene, sbs have null MAPE."""
        null_products = ["raffinate_c5", "crude_oil", "butadiene", "sbs"]
        for product in null_products:
            assert mig._MAPE_BASELINES[product]["mape"] is None

    def test_naive_mape_na_values(self):
        """sis and trimethylbenzene have n/a naive MAPE (stored as None)."""
        assert mig._MAPE_BASELINES["sis"]["naive_mape"] is None
        assert mig._MAPE_BASELINES["trimethylbenzene"]["naive_mape"] is None


# ── ARIMA Orders Tests ──────────────────────────────────────────────────

class TestARIMAOrders:
    """Validate ARIMA order data."""

    def test_non_default_arima_products(self):
        """3 products have non-default ARIMA orders."""
        assert len(mig._ARIMA_ORDERS) == 3

    def test_dcpd_arima(self):
        """dcpd ARIMA order = (3,1,2)."""
        assert mig._ARIMA_ORDERS["dcpd"] == (3, 1, 2)

    def test_sis_arima(self):
        """sis ARIMA order = (3,1,3)."""
        assert mig._ARIMA_ORDERS["sis"] == (3, 1, 3)

    def test_styrene_arima(self):
        """styrene ARIMA order = (3,1,0)."""
        assert mig._ARIMA_ORDERS["styrene"] == (3, 1, 0)

    def test_default_arima_order(self):
        """Default ARIMA order = (2,1,2)."""
        assert mig._DEFAULT_ARIMA_ORDER == (2, 1, 2)


# ── Pack JSON Tests ──────────────────────────────────────────────────────

class TestPackJSON:
    """Validate domain pack JSON structure."""

    def test_pack_key(self):
        """Pack key is ecisco_c5c9."""
        assert mig.PACK_KEY == "ecisco_c5c9"

    def test_pack_version(self):
        """Pack version is 1."""
        assert mig.PACK_VERSION == "1"

    def test_build_pack_json_has_products(self):
        """Pack JSON contains all canonical products."""
        targets = mig._build_canonical_targets("org1", "app1")
        pack = mig._build_pack_json(targets)
        assert len(pack["products"]) == len(targets)

    def test_pack_json_has_guardrails(self):
        """Pack JSON has guardrails section."""
        targets = mig._build_canonical_targets("org1", "app1")
        pack = mig._build_pack_json(targets)
        assert "guardrails" in pack
        assert pack["guardrails"]["min_history"] == 14
        assert pack["guardrails"]["max_mape"] == 0.30

    def test_pack_json_has_report_template(self):
        """Pack JSON has report_template section."""
        targets = mig._build_canonical_targets("org1", "app1")
        pack = mig._build_pack_json(targets)
        assert "report_template" in pack
        assert "sections" in pack["report_template"]

    def test_pack_json_below_naive_flag(self):
        """Products in _BELOW_NAIVE_PRODUCTS have below_naive_flag=True in pack."""
        targets = mig._build_canonical_targets("org1", "app1")
        pack = mig._build_pack_json(targets)
        flagged = [p for p in pack["products"] if p.get("below_naive_flag")]
        # cracked_c5, cracked_c9, styrene (mixed_c9 is alias, not canonical)
        assert len(flagged) == 3  # cracked_c5, cracked_c9, styrene

    def test_static_pack_json_file_exists(self):
        """The static pack JSON file exists on disk."""
        pack_path = Path(__file__).parent.parent / "app" / "services" / "forecasting" / "packs" / "ecisco_c5c9_v1.json"
        assert pack_path.exists()

    def test_static_pack_json_is_valid(self):
        """The static pack JSON file parses and has correct pack_key."""
        pack_path = Path(__file__).parent.parent / "app" / "services" / "forecasting" / "packs" / "ecisco_c5c9_v1.json"
        pack = json.loads(pack_path.read_text())
        assert pack["pack_key"] == "ecisco_c5c9"
        assert pack["pack_version"] == "1"
        assert len(pack["products"]) > 0

    def test_static_pack_json_has_all_weekly_products(self):
        """Static pack JSON has 13 products with include_in_weekly_report=True."""
        pack_path = Path(__file__).parent.parent / "app" / "services" / "forecasting" / "packs" / "ecisco_c5c9_v1.json"
        pack = json.loads(pack_path.read_text())
        weekly = [p for p in pack["products"] if p["report_config"]["include_in_weekly_report"]]
        assert len(weekly) == 13


# ── Feature Flags Tests ──────────────────────────────────────────────────

class TestFeatureFlags:
    """Validate feature flags default to False."""

    def test_forecasting_enabled_default_false(self):
        """FORECASTING_ENABLED defaults to False."""
        assert settings.FORECASTING_ENABLED is False

    def test_forecasting_enabled_is_bool(self):
        """FORECASTING_ENABLED is a bool field."""
        assert isinstance(settings.FORECASTING_ENABLED, bool)


# ── Agent Definitions Tests ──────────────────────────────────────────────

class TestAgentDefinitions:
    """Validate forecast_agent and report_agent in BUILTIN_AGENTS."""

    def test_forecast_agent_in_builtin_agents(self):
        """forecast_agent exists in BUILTIN_AGENTS."""
        names = [a.name for a in BUILTIN_AGENTS]
        assert "forecast_agent" in names

    def test_report_agent_in_builtin_agents(self):
        """report_agent exists in BUILTIN_AGENTS."""
        names = [a.name for a in BUILTIN_AGENTS]
        assert "report_agent" in names

    def test_forecast_agent_tools(self):
        """forecast_agent has 5 tools: discover, run, get, accuracy, rules."""
        agent = next(a for a in BUILTIN_AGENTS if a.name == "forecast_agent")
        assert agent.tools == [
            "forecast_discover",
            "forecast_run",
            "forecast_get",
            "forecast_accuracy",
            "forecast_rules",
        ]

    def test_report_agent_tools(self):
        """report_agent has 3 tools: report, ppt, create_artifact."""
        agent = next(a for a in BUILTIN_AGENTS if a.name == "report_agent")
        assert agent.tools == [
            "forecast_report",
            "forecast_ppt",
            "create_artifact",
        ]

    def test_forecast_agent_source_builtin(self):
        """forecast_agent source is builtin."""
        agent = next(a for a in BUILTIN_AGENTS if a.name == "forecast_agent")
        assert agent.source == "builtin"

    def test_report_agent_source_builtin(self):
        """report_agent source is builtin."""
        agent = next(a for a in BUILTIN_AGENTS if a.name == "report_agent")
        assert agent.source == "builtin"

    def test_forecast_agent_permission_mode_default(self):
        """forecast_agent permission_mode is default."""
        agent = next(a for a in BUILTIN_AGENTS if a.name == "forecast_agent")
        assert agent.permission_mode == "default"

    def test_report_agent_permission_mode_default(self):
        """report_agent permission_mode is default."""
        agent = next(a for a in BUILTIN_AGENTS if a.name == "report_agent")
        assert agent.permission_mode == "default"

    def test_forecast_agent_has_system_prompt(self):
        """forecast_agent has a non-empty system_prompt."""
        agent = next(a for a in BUILTIN_AGENTS if a.name == "forecast_agent")
        assert agent.system_prompt is not None
        assert len(agent.system_prompt) > 0

    def test_report_agent_has_system_prompt(self):
        """report_agent has a non-empty system_prompt."""
        agent = next(a for a in BUILTIN_AGENTS if a.name == "report_agent")
        assert agent.system_prompt is not None
        assert len(agent.system_prompt) > 0

    def test_forecast_agent_mentions_honesty_gate(self):
        """forecast_agent system_prompt mentions below_naive_baseline honesty gate."""
        agent = next(a for a in BUILTIN_AGENTS if a.name == "forecast_agent")
        assert "below_naive" in agent.system_prompt.lower()

    # ── EDIA Pipeline Agent Tests ────────────────────────────────────────

    def test_ecisco_bi_assistant_in_builtin_agents(self):
        """ecisco_bi_assistant exists in BUILTIN_AGENTS."""
        names = [a.name for a in BUILTIN_AGENTS]
        assert "ecisco_bi_assistant" in names

    def test_perception_agent_in_builtin_agents(self):
        """perception_agent exists in BUILTIN_AGENTS."""
        names = [a.name for a in BUILTIN_AGENTS]
        assert "perception_agent" in names

    def test_rag_research_agent_in_builtin_agents(self):
        """rag_research_agent exists in BUILTIN_AGENTS."""
        names = [a.name for a in BUILTIN_AGENTS]
        assert "rag_research_agent" in names

    def test_diagnosis_agent_in_builtin_agents(self):
        """diagnosis_agent exists in BUILTIN_AGENTS."""
        names = [a.name for a in BUILTIN_AGENTS]
        assert "diagnosis_agent" in names

    def test_pricing_agent_in_builtin_agents(self):
        """pricing_agent exists in BUILTIN_AGENTS."""
        names = [a.name for a in BUILTIN_AGENTS]
        assert "pricing_agent" in names

    def test_ecisco_bi_assistant_has_delegation_tools(self):
        """ecisco_bi_assistant has delegation tools for all EDIA sub-agents."""
        agent = next(a for a in BUILTIN_AGENTS if a.name == "ecisco_bi_assistant")
        expected = [
            "ask_perception", "ask_rag_research", "ask_diagnosis",
            "ask_forecast_agent", "ask_pricing", "ask_data_agent",
            "ask_report_agent",
        ]
        for tool in expected:
            assert tool in agent.tools, f"ecisco_bi_assistant missing tool: {tool}"

    def test_perception_agent_has_data_and_web_tools(self):
        """perception_agent has ask_data_agent, web_search, search_documents."""
        agent = next(a for a in BUILTIN_AGENTS if a.name == "perception_agent")
        assert "ask_data_agent" in agent.tools
        assert "web_search" in agent.tools
        assert "search_documents" in agent.tools

    def test_rag_research_agent_has_rag_tools(self):
        """rag_research_agent has search_documents and answer_from_documents."""
        agent = next(a for a in BUILTIN_AGENTS if a.name == "rag_research_agent")
        assert "search_documents" in agent.tools
        assert "answer_from_documents" in agent.tools

    def test_diagnosis_agent_has_delegation_tools(self):
        """diagnosis_agent can delegate to perception and rag_research."""
        agent = next(a for a in BUILTIN_AGENTS if a.name == "diagnosis_agent")
        assert "ask_perception" in agent.tools
        assert "ask_rag_research" in agent.tools

    def test_pricing_agent_has_delegation_tools(self):
        """pricing_agent can delegate to forecast and diagnosis."""
        agent = next(a for a in BUILTIN_AGENTS if a.name == "pricing_agent")
        assert "ask_forecast_agent" in agent.tools
        assert "ask_diagnosis" in agent.tools

    def test_ecisco_bi_assistant_has_system_prompt(self):
        """ecisco_bi_assistant has a non-empty system_prompt."""
        agent = next(a for a in BUILTIN_AGENTS if a.name == "ecisco_bi_assistant")
        assert agent.system_prompt is not None
        assert len(agent.system_prompt) > 0

    def test_perception_agent_has_system_prompt(self):
        """perception_agent has a non-empty system_prompt."""
        agent = next(a for a in BUILTIN_AGENTS if a.name == "perception_agent")
        assert agent.system_prompt is not None
        assert len(agent.system_prompt) > 0

    def test_rag_research_agent_has_system_prompt(self):
        """rag_research_agent has a non-empty system_prompt."""
        agent = next(a for a in BUILTIN_AGENTS if a.name == "rag_research_agent")
        assert agent.system_prompt is not None
        assert len(agent.system_prompt) > 0

    def test_diagnosis_agent_has_system_prompt(self):
        """diagnosis_agent has a non-empty system_prompt."""
        agent = next(a for a in BUILTIN_AGENTS if a.name == "diagnosis_agent")
        assert agent.system_prompt is not None
        assert len(agent.system_prompt) > 0

    def test_pricing_agent_has_system_prompt(self):
        """pricing_agent has a non-empty system_prompt."""
        agent = next(a for a in BUILTIN_AGENTS if a.name == "pricing_agent")
        assert agent.system_prompt is not None
        assert len(agent.system_prompt) > 0

    def test_pricing_agent_mentions_9_step_ladder(self):
        """pricing_agent system_prompt mentions the 9-step pricing ladder."""
        agent = next(a for a in BUILTIN_AGENTS if a.name == "pricing_agent")
        assert "9-step" in agent.system_prompt.lower() or "9 step" in agent.system_prompt.lower()


# ── System Agent Tests ──────────────────────────────────────────────────

class TestSystemAgent:
    """Validate ecisco_bi_assistant system agent config (replaces ecisco_bi/orchestrator)."""

    def test_ecisco_bi_assistant_in_configs(self):
        """ecisco_bi_assistant IS always in configs (no feature flag needed)."""
        configs = _build_system_agent_configs()
        names = [c["name"] for c in configs]
        assert "ecisco_bi_assistant" in names

    def test_ecisco_bi_assistant_is_silent_system(self):
        """ecisco_bi_assistant is a silent platform system agent (is_system=True).

        The agent is auto-selected by Chat.jsx when the user opens a chat
        in the Ecisco BI project, so end users must never see it in any
        UI surface. Hiding is done at the frontend via filterUserAgents()
        matching on the is_system flag, and at the API layer via
        entity_service.soft_delete_record refusing to delete system
        agents. This test guards the seeder contract; the
        entity-service guard is tested in test_ecisco_bi_silent_agent.py.
        """
        configs = _build_system_agent_configs()
        orch = next(c for c in configs if c["name"] == "ecisco_bi_assistant")
        assert orch.get("is_system") is True, (
            f"ecisco_bi_assistant should be is_system=True so the frontend "
            f"hides it, got is_system={orch.get('is_system')}"
        )

    def test_ecisco_bi_assistant_project_is_ecisco_bi(self):
        """ecisco_bi_assistant project is 'Ecisco BI'."""
        configs = _build_system_agent_configs()
        orch = next(c for c in configs if c["name"] == "ecisco_bi_assistant")
        assert orch["project"] == "Ecisco BI"

    def test_ecisco_bi_assistant_has_delegation_tools(self):
        """ecisco_bi_assistant has all EDIA delegation tools in allowed boundaries."""
        configs = _build_system_agent_configs()
        orch = next(c for c in configs if c["name"] == "ecisco_bi_assistant")
        raw_tools = orch["manifest_json"]["boundaries"]["allowed"]
        expected_tools = [
            "ask_perception", "ask_rag_research", "ask_diagnosis",
            "ask_forecast_agent", "ask_pricing", "ask_data_agent",
            "ask_report_agent", "create_artifact", "web_search", "web_extract",
        ]
        for tool in expected_tools:
            assert tool in raw_tools, f"ecisco_bi_assistant missing tool in boundaries: {tool}"

    def test_ecisco_bi_assistant_has_base_harness(self):
        """ecisco_bi_assistant has trace_enabled, log_level, memory_scope, output_contract."""
        configs = _build_system_agent_configs()
        orch = next(c for c in configs if c["name"] == "ecisco_bi_assistant")
        assert "trace_enabled" in orch
        assert orch["trace_enabled"] is True
        assert "output_contract" in orch
        assert "memory_scope" in orch

    def test_ecisco_bi_assistant_risk_tier_low(self):
        """ecisco_bi_assistant risk_tier is low."""
        configs = _build_system_agent_configs()
        orch = next(c for c in configs if c["name"] == "ecisco_bi_assistant")
        assert orch["manifest_json"]["risk_tier"] == "low"


# ── DB Row Write Tests ──────────────────────────────────────────────────

class TestDBRowWrites:
    """Validate migration script writes correct DB rows."""

    def test_build_canonical_targets_creates_rows(self):
        """_build_canonical_targets produces 16 target row dicts."""
        targets = mig._build_canonical_targets("org1", "app1")
        assert len(targets) == 16

    def test_target_rows_have_source_pack(self):
        """All target rows have source='pack'."""
        targets = mig._build_canonical_targets("org1", "app1")
        for target in targets:
            assert target["source"] == "pack"

    def test_target_rows_have_org_id(self):
        """All target rows have org_id set."""
        targets = mig._build_canonical_targets("test_org", "test_app")
        for target in targets:
            assert target["org_id"] == "test_org"
            assert target["app_id"] == "test_app"

    def test_target_rows_have_model_config(self):
        """All target rows have model_config with arima_order."""
        targets = mig._build_canonical_targets("org1", "app1")
        for target in targets:
            assert "model_config" in target
            assert "arima_order" in target["model_config"]

    def test_non_default_arima_in_model_config(self):
        """dcpd, sis, styrene have non-default arima_order in model_config."""
        targets = mig._build_canonical_targets("org1", "app1")
        for target in targets:
            if target["product_key"] in mig._ARIMA_ORDERS:
                expected = list(mig._ARIMA_ORDERS[target["product_key"]])
                assert target["model_config"]["arima_order"] == expected
            else:
                assert target["model_config"]["arima_order"] == list(mig._DEFAULT_ARIMA_ORDER)

    def test_seasonal_rules_count(self):
        """Seasonal rules produce correct number of individual month entries."""
        target_id_map = {k: str(uuid4()) for k in mig._CANONICAL_PRODUCTS}
        rules = mig._build_seasonal_rules("org1", "app1", target_id_map)
        # Count total month entries across all _SEASONAL_ADJ_MAP products
        total_months = 0
        for product_key, adjustments in mig._SEASONAL_ADJ_MAP.items():
            for adj in adjustments:
                total_months += len(adj["months"])
        assert len(rules) == total_months

    def test_causal_rules_count(self):
        """Causal rules produce 17 entries (one per elasticity)."""
        target_id_map = {k: str(uuid4()) for k in mig._CANONICAL_PRODUCTS}
        rules = mig._build_causal_rules("org1", "app1", target_id_map)
        assert len(rules) == len(mig._ELASTICITIES)

    def test_event_override_rules_count(self):
        """Event override rules produce 5 entries (one per post-holiday product)."""
        target_id_map = {k: str(uuid4()) for k in mig._CANONICAL_PRODUCTS}
        rules = mig._build_event_override_rules("org1", "app1", target_id_map)
        assert len(rules) == len(mig._POST_HOLIDAY_PRODUCTS)

    def test_guardrail_rules_count(self):
        """1 global guardrail rule."""
        rules = mig._build_guardrail_rules("org1", "app1")
        assert len(rules) == 1

    def test_accuracy_log_rows_skip_null_mape(self):
        """Products with null MAPE are skipped in accuracy log."""
        target_id_map = {k: str(uuid4()) for k in mig._CANONICAL_PRODUCTS}
        rows = mig._build_accuracy_log_rows("org1", "app1", target_id_map)
        # Products with null MAPE: raffinate_c5, crude_oil, butadiene, sbs = 4
        expected = len(mig._MAPE_BASELINES) - 4  # 16 - 4 = 12
        assert len(rows) == expected

    def test_accuracy_log_below_naive_flag(self):
        """Products in _BELOW_NAIVE_PRODUCTS have below_naive_baseline=True."""
        target_id_map = {k: str(uuid4()) for k in mig._CANONICAL_PRODUCTS}
        rows = mig._build_accuracy_log_rows("org1", "app1", target_id_map)
        # Find rows for below-naive products
        below_naive_rows = [
            r for r in rows
            if any(mig._PRODUCT_MAP.get(p, (p, ""))[0] for p in mig._BELOW_NAIVE_PRODUCTS)
        ]
        # All below-naive products should have below_naive_baseline=True
        for r in rows:
            product_key = None
            for pk, tid in target_id_map.items():
                if tid == r["target_id"]:
                    product_key = pk
                    break
            if product_key in mig._BELOW_NAIVE_PRODUCTS or (
                product_key in mig._MAPE_BASELINES and
                mig._MAPE_BASELINES[product_key]["skill_vs_naive"] is not None and
                mig._MAPE_BASELINES[product_key]["skill_vs_naive"] > 0
            ):
                assert r["below_naive_baseline"] is True


# ── Idempotency Tests ───────────────────────────────────────────────────

class TestIdempotency:
    """Validate migration script is idempotent — running twice produces same state."""

    def test_dry_run_produces_no_db_writes(self):
        """Dry run does not write to the database."""
        summary = mig.run_migration(org_id="test_org", app_id="test_app", dry_run=True)
        assert summary["dry_run"] is True
        # Verify no rows in DB
        db_session = SessionLocal()
        targets = db_session.query(ForecastTarget).filter(
            ForecastTarget.org_id == "test_org"
        ).all()
        assert len(targets) == 0
        db_session.close()

    def test_second_run_no_extra_targets(self):
        """Running migration twice produces same number of target rows."""
        # First run
        mig.run_migration(org_id="test_org", app_id="test_app", dry_run=False)
        db1 = SessionLocal()
        count1 = db1.query(ForecastTarget).filter(
            ForecastTarget.org_id == "test_org"
        ).count()
        db1.close()

        # Second run (should be idempotent)
        mig.run_migration(org_id="test_org", app_id="test_app", dry_run=False)
        db2 = SessionLocal()
        count2 = db2.query(ForecastTarget).filter(
            ForecastTarget.org_id == "test_org"
        ).count()
        db2.close()

        assert count1 == count2
