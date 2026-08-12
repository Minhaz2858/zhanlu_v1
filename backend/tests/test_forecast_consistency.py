"""Consistency contract tests — the BI chat agent and the market dashboard
must quote the SAME forecast number for the same product + horizon.

Both surfaces read the persisted ForecastRun table:
- dashboard card  → get_forecast_base_batch_v2(product_id, day)
- chat agent      → forecast_get / forecast_run canonical answer block

These tests pin the shared read-model (forecasting.answer_format) and the
end-to-end equality of the two surfaces against one seeded run.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.base import Base
from app.models.forecasting import ForecastTarget, ForecastRun
from app.services.forecasting.answer_format import (
    DEFAULT_FORECAST_DAY,
    format_run_answer,
    pick_horizon_payload,
    select_headline_day,
)
from app.services.market_dashboard_service import get_forecast_base_batch_v2
from app.services.tool_handlers.forecast_tool import _forecast_get


def _run(coro):
    """Drive a coroutine without disturbing the global event-loop policy.

    asyncio.run() closes the main-thread loop, which breaks other test
    modules' asyncio.get_event_loop() helpers when run in the same session.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def db_session():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng, tables=[
        ForecastTarget.__table__,
        ForecastRun.__table__,
    ])
    Session = sessionmaker(bind=eng)
    session = Session()
    yield session
    session.close()


def _seed_run(db, product_key="ecisco.crude_oil", base_7d=84.5, base_3d=34.5):
    target = ForecastTarget(
        org_id="default-org",
        app_id="default-app",
        product_key=product_key,
        name=product_key,
        datasource={"source": "edia_mysql"},
        level=2,
        status="active",
        source="seed",
    )
    db.add(target)
    db.flush()
    run = ForecastRun(
        org_id="default-org",
        app_id="default-app",
        target_id=target.id,
        results={
            "3d": {"base": [base_3d, base_3d + 1, base_3d + 2],
                   "bull": [base_3d + 100], "bear": [base_3d - 100]},
            "7d": {"base": [base_7d] * 7,
                   "bull": [base_7d + 100] * 7, "bear": [base_7d - 100] * 7},
            "30d": {"base": [base_7d + 200] * 30,
                    "bull": [base_7d + 300] * 30, "bear": [base_7d + 100] * 30},
        },
        below_naive_baseline=False,
        confidence="Medium",
        as_of_date=datetime.now(timezone.utc),
        model_detail={"models_run": ["arima", "ets"]},
        explanation={"analyst_brief": {"7": {"market_update_zh": " stored rationale "}}},
    )
    db.add(run)
    db.commit()
    return target, run


# ======================================================================
# End-to-end: chat tool answer == dashboard card value
# ======================================================================

class TestChatMatchesDashboard:
    def test_7d_point_estimate_matches(self, db_session):
        target, _run_row = _seed_run(db_session)

        dashboard = get_forecast_base_batch_v2(["crude_oil"], day=7, db=db_session)
        chat = _run(_forecast_get(
            {"target_id": target.id, "day": 7}, db_session, "user-1", None,
        ))

        assert chat["success"] is True
        assert chat["answer"]["point_estimate"] == 84.5
        assert dashboard.base_by_product["crude_oil"] == chat["answer"]["point_estimate"]

    def test_3d_point_estimate_matches(self, db_session):
        target, _run_row = _seed_run(db_session)

        dashboard = get_forecast_base_batch_v2(["crude_oil"], day=3, db=db_session)
        chat = _run(_forecast_get(
            {"target_id": target.id, "day": 3}, db_session, "user-1", None,
        ))

        assert chat["success"] is True
        assert chat["answer"]["point_estimate"] == 34.5
        assert dashboard.base_by_product["crude_oil"] == chat["answer"]["point_estimate"]

    def test_default_day_is_next_week(self, db_session):
        target, _run_row = _seed_run(db_session)
        chat = _run(_forecast_get(
            {"target_id": target.id}, db_session, "user-1", None,
        ))
        assert chat["answer"]["day"] == DEFAULT_FORECAST_DAY == 7
        assert chat["answer"]["point_estimate"] == 84.5

    def test_explanation_passed_through_verbatim(self, db_session):
        target, run = _seed_run(db_session)
        chat = _run(_forecast_get(
            {"target_id": target.id, "day": 7}, db_session, "user-1", None,
        ))
        assert chat["answer"]["explanation"] == run.explanation

    def test_legacy_forecast_shape_preserved(self, db_session):
        target, _run_row = _seed_run(db_session)
        chat = _run(_forecast_get(
            {"target_id": target.id}, db_session, "user-1", None,
        ))
        legacy = chat["forecast"]
        assert "results" in legacy and "7d" in legacy["results"]
        assert legacy["below_naive_baseline"] is False
        assert legacy["confidence"] == "Medium"


# ======================================================================
# Shared read-model semantics
# ======================================================================

class TestPickHorizonPayload:
    def test_exact_d_suffix_and_plain_keys(self):
        assert pick_horizon_payload({"7d": {"base": [1]}}, 7) == ({"base": [1]}, 7)
        assert pick_horizon_payload({"7": {"base": [2]}}, 7) == ({"base": [2]}, 7)

    def test_nearest_fallback_tie_prefers_larger(self):
        results = {"5": {"base": [1]}, "9": {"base": [2]}}
        payload, h = pick_horizon_payload(results, 7)
        assert h == 9  # tie (both 2 away) → larger horizon
        assert payload == {"base": [2]}

    def test_empty_results(self):
        assert pick_horizon_payload({}, 7) == ({}, None)
        assert pick_horizon_payload(None, 7) == ({}, None)


class TestFormatRunAnswer:
    def test_fallback_horizon_curve_sliced_to_requested_day(self, db_session):
        _t, run = _seed_run(db_session)
        answer = format_run_answer(run, day=7)
        # Exact 7d key exists → no slicing, effective == requested
        assert answer["effective_horizon"] == 7
        assert len(answer["scenarios"]["base"]) == 7
        assert answer["point_estimate"] == 84.5

    def test_larger_horizon_fallback_slices(self, db_session):
        target = ForecastTarget(
            org_id="default-org", app_id="default-app",
            product_key="ecisco.naphtha", name="naphtha",
            datasource={"source": "edia_mysql"}, level=2,
            status="active", source="seed",
        )
        db_session.add(target)
        db_session.flush()
        run = ForecastRun(
            org_id="default-org", app_id="default-app", target_id=target.id,
            results={"30d": {"base": list(range(30))}},
            below_naive_baseline=False, confidence="Low",
        )
        db_session.add(run)
        db_session.commit()

        answer = format_run_answer(run, day=7)
        assert answer["effective_horizon"] == 30
        assert answer["scenarios"]["base"] == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
        assert answer["point_estimate"] == 0.0

    def test_scalar_base_normalised(self, db_session):
        target = ForecastTarget(
            org_id="default-org", app_id="default-app",
            product_key="ecisco.sis", name="sis",
            datasource={"source": "edia_mysql"}, level=2,
            status="active", source="seed",
        )
        db_session.add(target)
        db_session.flush()
        run = ForecastRun(
            org_id="default-org", app_id="default-app", target_id=target.id,
            results={"7": {"base": 9100}},
            below_naive_baseline=False, confidence="Low",
        )
        db_session.add(run)
        db_session.commit()

        answer = format_run_answer(run, day=7)
        assert answer["point_estimate"] == 9100.0


class TestSelectHeadlineDay:
    def test_prefers_seven_when_requested(self):
        assert select_headline_day([3, 7, 30]) == 7
        assert select_headline_day([7]) == 7

    def test_falls_back_to_smallest(self):
        assert select_headline_day([30, 3]) == 3
        assert select_headline_day([30]) == 30

    def test_empty_defaults_to_seven(self):
        assert select_headline_day([]) == 7


# ======================================================================
# Prompt contract
# ======================================================================

class TestPromptConsistencyContract:
    def test_forecast_agent_prompt_has_consistency_contract(self):
        from app.services.agent_definitions import FORECAST_AGENT_PROMPT

        text = FORECAST_AGENT_PROMPT.lower()
        assert "consistency contract" in text
        assert "dashboard" in text
        assert "verbatim" in text
        assert "next week" in text

    def test_ecisco_bi_assistant_prompt_quotes_dashboard_values(self):
        # ECISCO_BI_ASSISTANT_PROMPT is the user-facing chat agent whose
        # answers must match the dashboard.
        from app.services.agent_definitions import ECISCO_BI_ASSISTANT_PROMPT

        text = ECISCO_BI_ASSISTANT_PROMPT.lower()
        assert "dashboard" in text
        assert "verbatim" in text
