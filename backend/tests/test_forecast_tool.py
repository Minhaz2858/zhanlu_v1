"""Tests for forecast agent tools (Section 3).

Validates all 5 registered tools: schema format, handler dispatch, KB scoping,
error cases, and the forecast_rules CRUD workflow.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from app.database import Base, engine, SessionLocal
from app.models.forecasting import (
    ForecastTarget,
    ForecastRun,
    ForecastAccuracyLog,
    ForecastBusinessRule,
)
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User
from app.services.tool_handlers.forecast_tool import (
    _forecast_discover,
    _forecast_run,
    _forecast_get,
    _forecast_accuracy,
    _forecast_rules,
    FORECAST_DISCOVER_SCHEMA,
    FORECAST_RUN_SCHEMA,
    FORECAST_GET_SCHEMA,
    FORECAST_ACCURACY_SCHEMA,
    FORECAST_RULES_SCHEMA,
    _resolve_org_context,
    _serialize_target,
    _serialize_rule,
)

# ── Fixtures ──────────────────────────────────────────────────────────


_NEEDED_TABLES = [
    ForecastTarget.__table__,
    ForecastRun.__table__,
    ForecastAccuracyLog.__table__,
    ForecastBusinessRule.__table__,
    KnowledgeBase.__table__,
    User.__table__,
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
    """Clean DB session (rollback after each test)."""
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


@pytest.fixture
def org_context():
    return {"org_id": "test-org", "app_id": "test-app"}


@pytest.fixture
def user_id():
    return "user-001"


@pytest.fixture
def kb(db):
    """Create a test KnowledgeBase entry."""
    kb = KnowledgeBase(
        id="kb-test-001",
        org_id="test-org",
        name="Test KB",
        data_source_config={"type": "postgres"},
    )
    db.add(kb)
    db.commit()
    db.refresh(kb)
    return kb


@pytest.fixture
def target(db):
    """Create a minimal ForecastTarget."""
    t = ForecastTarget(
        id="target-001",
        org_id="test-org",
        app_id="test-app",
        product_key="sales_daily",
        name="Daily Sales",
        level="product",
        source="forecast_discover",
        status="active",
        quality_grade="B",
        include_in_weekly_report=True,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


# Set db to in-memory SQLite
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")


# ======================================================================
# Schema validation tests
# ======================================================================


class TestSchemaFormat:
    """Every tool schema must match OpenAI function-calling format."""

    def _validate(self, schema: dict, name: str):
        """Shared assertions for a single schema."""
        assert schema["type"] == "function"
        fn = schema["function"]
        assert fn["name"] == name
        assert isinstance(fn["description"], str) and len(fn["description"]) > 10
        params = fn["parameters"]
        assert params["type"] == "object"
        assert "properties" in params
        # required may be absent — that's valid

    def test_discover_schema(self):
        self._validate(FORECAST_DISCOVER_SCHEMA, "forecast_discover")
        req = FORECAST_DISCOVER_SCHEMA["function"]["parameters"].get("required", [])
        assert "data_source_id" in req

    def test_run_schema(self):
        self._validate(FORECAST_RUN_SCHEMA, "forecast_run")
        props = FORECAST_RUN_SCHEMA["function"]["parameters"]["properties"]
        assert "target_id" in props
        assert "horizons" in props
        assert "seasonal_period" in props

    def test_get_schema(self):
        self._validate(FORECAST_GET_SCHEMA, "forecast_get")
        req = FORECAST_GET_SCHEMA["function"]["parameters"].get("required", [])
        assert "target_id" in req

    def test_accuracy_schema(self):
        self._validate(FORECAST_ACCURACY_SCHEMA, "forecast_accuracy")
        req = FORECAST_ACCURACY_SCHEMA["function"]["parameters"].get("required", [])
        assert "target_id" in req

    def test_rules_schema(self):
        self._validate(FORECAST_RULES_SCHEMA, "forecast_rules")
        props = FORECAST_RULES_SCHEMA["function"]["parameters"]["properties"]
        assert "action" in props
        assert "rule_type" in props
        assert props["action"].get("enum") == ["list", "propose", "activate", "pause"]


# ======================================================================
# Helper tests
# ======================================================================


class TestResolveOrgContext:
    def test_defaults(self):
        org, app = _resolve_org_context(None)
        assert org == "default-org"
        assert app == "default-app"

    def test_from_context(self):
        org, app = _resolve_org_context({"org_id": "X", "app_id": "Y"})
        assert org == "X"
        assert app == "Y"

    def test_partial_context(self):
        org, app = _resolve_org_context({"org_id": "X"})
        assert org == "X"
        assert app == "default-app"


class TestSerializeTarget:
    def test_full(self, db, target):
        d = _serialize_target(target)
        assert d["id"] == "target-001"
        assert d["product_key"] == "sales_daily"
        assert d["quality_grade"] == "B"
        assert d["status"] == "active"

    def test_no_datasource(self, db, target):
        d = _serialize_target(target)
        assert d["granularity"] is None


class TestSerializeRule:
    def test_full(self, db):
        rule = ForecastBusinessRule(
            id="rule-001",
            org_id="test-org",
            target_id="target-001",
            rule_type="seasonal",
            params={"month": 12, "adjustment_pct": 0.15},
            status="active",
            source="chat",
            confidence=0.90,
        )
        d = _serialize_rule(rule)
        assert d["id"] == "rule-001"
        assert d["rule_type"] == "seasonal"
        assert d["status"] == "active"
        assert d["params"] == {"month": 12, "adjustment_pct": 0.15}

    def test_no_approved_at(self, db):
        rule = ForecastBusinessRule(
            id="rule-002",
            org_id="test-org",
            rule_type="guardrail",
            params={"min_history": 60, "max_mape": 0.30},
            status="proposed",
            source="chat",
        )
        d = _serialize_rule(rule)
        assert d["id"] == "rule-002"
        assert d["approved_at"] is None


# ======================================================================
# forecast_discover
# ======================================================================


class TestForecastDiscover:
    def test_missing_data_source_id(self, db, org_context):
        result = asyncio_run(
            _forecast_discover({"data_source_id": ""}, db, "user-001", org_context)
        )
        assert result["success"] is False
        assert "error" in result

    def test_requires_kb_in_context(self, db, org_context):
        """Without a bound KB, discovery should fail."""
        result = asyncio_run(
            _forecast_discover({"data_source_id": "kb-nonexistent"}, db, "user-001", org_context)
        )
        assert result["success"] is False
        assert "error" in result


# ======================================================================
# forecast_get
# ======================================================================


class TestForecastGet:
    def test_missing_target_id(self, db, org_context):
        result = asyncio_run(
            _forecast_get({}, db, "user-001", org_context)
        )
        assert result["success"] is False
        assert result["error"] == "target_id is required."

    def test_target_not_found(self, db, org_context):
        result = asyncio_run(
            _forecast_get({"target_id": "nonexistent"}, db, "user-001", org_context)
        )
        assert result["success"] is False
        assert "No forecast found" in result["error"]

    def test_returns_forecast_when_exists(self, db, target, org_context):
        # Seed a ForecastRun so get_forecast succeeds
        run = ForecastRun(
            org_id="test-org",
            app_id="test-app",
            target_id=target.id,
            results={
                "3": {"base": [100, 102, 104], "bull": [105, 108, 111], "bear": [95, 96, 97]},
                "7": {"base": [100, 102, 104, 106, 108, 110, 112]},
            },
            below_naive_baseline=False,
            confidence="high",
            model_detail={"ensemble_weights": {"ets": 0.4, "arima": 0.6}},
        )
        db.add(run)
        db.commit()

        result = asyncio_run(
            _forecast_get({"target_id": target.id}, db, "user-001", org_context)
        )
        assert result["success"] is True
        fc = result["forecast"]
        assert "results" in fc
        assert fc["below_naive_baseline"] is False
        assert fc["confidence"] == "high"


# ======================================================================
# forecast_accuracy
# ======================================================================


class TestForecastAccuracy:
    def test_missing_target_id(self, db, org_context):
        result = asyncio_run(
            _forecast_accuracy({}, db, "user-001", org_context)
        )
        assert result["success"] is False
        assert "target_id is required" in result["error"]

    def test_returns_logs_when_exist(self, db, target, org_context):
        log = ForecastAccuracyLog(
            org_id="test-org",
            app_id="test-app",
            target_id=target.id,
            horizon_days=3,
            mape=0.08,
            naive_mape=0.12,
            skill_vs_naive=0.33,
            below_naive_baseline=False,
            per_model={"ets": 0.06, "arima": 0.09},
        )
        db.add(log)
        db.commit()

        result = asyncio_run(
            _forecast_accuracy({"target_id": target.id}, db, "user-001", org_context)
        )
        assert result["success"] is True
        assert result["count"] == 1
        entry = result["accuracy_log"][0]
        assert entry["horizon_days"] == 3
        assert entry["mape"] == 0.08
        assert "per_model" in entry


# ======================================================================
# forecast_rules
# ======================================================================


class TestForecastRules:
    # ── list ────────────────────────────────────────────────────

    def test_list_empty(self, db, org_context):
        result = asyncio_run(
            _forecast_rules({"action": "list"}, db, "user-001", org_context)
        )
        assert result["success"] is True
        assert result["count"] == 0
        assert result["rules"] == []

    def test_list_with_rule(self, db, org_context):
        rule = ForecastBusinessRule(
            org_id="test-org",
            app_id="test-app",
            rule_type="guardrail",
            params={"min_history": 60},
            status="active",
            source="chat",
        )
        db.add(rule)
        db.commit()

        result = asyncio_run(
            _forecast_rules({"action": "list"}, db, "user-001", org_context)
        )
        assert result["success"] is True
        assert result["count"] == 1
        assert result["rules"][0]["rule_type"] == "guardrail"

    def test_list_filter_by_target(self, db, target, org_context):
        rule1 = ForecastBusinessRule(
            org_id="test-org", app_id="test-app", target_id=target.id,
            rule_type="seasonal", params={"month": 6}, status="active", source="chat",
        )
        rule2 = ForecastBusinessRule(
            org_id="test-org", app_id="test-app", target_id=None,
            rule_type="guardrail", params={"min_history": 60}, status="active", source="chat",
        )
        db.add_all([rule1, rule2])
        db.commit()

        result = asyncio_run(
            _forecast_rules({"action": "list", "target_id": target.id}, db, "user-001", org_context)
        )
        assert result["success"] is True
        assert result["count"] == 1
        assert result["rules"][0]["rule_type"] == "seasonal"

    # ── propose ─────────────────────────────────────────────────

    def test_propose_seasonal(self, db, target, org_context):
        result = asyncio_run(
            _forecast_rules(
                {
                    "action": "propose",
                    "target_id": target.id,
                    "rule_type": "seasonal",
                    "params": {"month": 12, "adjustment_pct": 0.15},
                },
                db, "user-001", org_context,
            )
        )
        assert result["success"] is True
        assert result["status"] == "proposed"
        rule_id = result["rule"]["id"]
        assert rule_id is not None

        # Verify persisted
        rule = db.get(ForecastBusinessRule, rule_id)
        assert rule is not None
        assert rule.rule_type == "seasonal"
        assert rule.status == "proposed"
        assert rule.source == "chat"
        assert rule.params["month"] == 12

    def test_propose_global_guardrail(self, db, org_context):
        result = asyncio_run(
            _forecast_rules(
                {
                    "action": "propose",
                    "rule_type": "guardrail",
                    "params": {"min_history": 60, "max_mape": 0.30},
                },
                db, "user-001", org_context,
            )
        )
        assert result["success"] is True
        assert result["rule"]["target_id"] is None

    def test_propose_missing_rule_type(self, db, org_context):
        result = asyncio_run(
            _forecast_rules(
                {"action": "propose", "params": {"x": 1}},
                db, "user-001", org_context,
            )
        )
        assert result["success"] is False
        assert "rule_type is required" in result["error"]

    def test_propose_invalid_rule_type(self, db, org_context):
        result = asyncio_run(
            _forecast_rules(
                {"action": "propose", "rule_type": "bogus", "params": {}},
                db, "user-001", org_context,
            )
        )
        assert result["success"] is False
        assert "Unknown rule_type" in result["error"]

    def test_propose_non_dict_params(self, db, org_context):
        result = asyncio_run(
            _forecast_rules(
                {"action": "propose", "rule_type": "seasonal", "params": "not-dict"},
                db, "user-001", org_context,
            )
        )
        assert result["success"] is False
        assert "params must be a JSON object" in result["error"]

    # ── activate ────────────────────────────────────────────────

    def test_activate_rule(self, db, org_context):
        rule = ForecastBusinessRule(
            org_id="test-org", app_id="test-app",
            rule_type="seasonal", params={"month": 6}, status="proposed", source="chat",
        )
        db.add(rule)
        db.commit()
        db.refresh(rule)

        result = asyncio_run(
            _forecast_rules(
                {"action": "activate", "rule_id": rule.id},
                db, "user-001", org_context,
            )
        )
        assert result["success"] is True
        assert result["status"] == "active"
        assert result["rule"]["approved_by_id"] == "user-001"
        assert result["rule"]["approved_at"] is not None

        # Verify persisted
        db.refresh(rule)
        assert rule.status == "active"
        assert rule.approved_by_id == "user-001"

    def test_activate_missing_rule_id(self, db, org_context):
        result = asyncio_run(
            _forecast_rules({"action": "activate"}, db, "user-001", org_context)
        )
        assert result["success"] is False
        assert "rule_id is required" in result["error"]

    def test_activate_nonexistent(self, db, org_context):
        result = asyncio_run(
            _forecast_rules(
                {"action": "activate", "rule_id": "nonexistent"},
                db, "user-001", org_context,
            )
        )
        assert result["success"] is False
        assert "not found" in result["error"]

    # ── pause ───────────────────────────────────────────────────

    def test_pause_rule(self, db, org_context):
        rule = ForecastBusinessRule(
            org_id="test-org", app_id="test-app",
            rule_type="causal_driver", params={"driver": "price"}, status="active", source="chat",
        )
        db.add(rule)
        db.commit()
        db.refresh(rule)

        result = asyncio_run(
            _forecast_rules(
                {"action": "pause", "rule_id": rule.id},
                db, "user-001", org_context,
            )
        )
        assert result["success"] is True
        assert result["status"] == "paused"

        db.refresh(rule)
        assert rule.status == "paused"

    def test_pause_missing_rule_id(self, db, org_context):
        result = asyncio_run(
            _forecast_rules({"action": "pause"}, db, "user-001", org_context)
        )
        assert result["success"] is False
        assert "rule_id is required" in result["error"]

    # ── full lifecycle ──────────────────────────────────────────

    def test_full_lifecycle(self, db, target, org_context):
        """propose → list → activate → pause → list"""
        # propose
        p = asyncio_run(_forecast_rules(
            {"action": "propose", "target_id": target.id,
             "rule_type": "event_override",
             "params": {"event": "black_friday", "month": 11, "adjustment_pct": 0.35}},
            db, "user-001", org_context,
        ))
        assert p["success"] is True
        rid = p["rule"]["id"]

        # list — should show proposed
        ls = asyncio_run(_forecast_rules(
            {"action": "list", "target_id": target.id}, db, "user-001", org_context
        ))
        assert ls["count"] == 1
        assert ls["rules"][0]["status"] == "proposed"

        # activate
        a = asyncio_run(_forecast_rules(
            {"action": "activate", "rule_id": rid}, db, "user-001", org_context
        ))
        assert a["success"] is True
        assert a["status"] == "active"

        # pause
        pa = asyncio_run(_forecast_rules(
            {"action": "pause", "rule_id": rid}, db, "user-001", org_context
        ))
        assert pa["success"] is True
        assert pa["status"] == "paused"

        # list — should show paused
        ls2 = asyncio_run(_forecast_rules(
            {"action": "list", "target_id": target.id}, db, "user-001", org_context
        ))
        assert ls2["count"] == 1
        assert ls2["rules"][0]["status"] == "paused"

    # ── unknown action ──────────────────────────────────────────

    def test_unknown_action(self, db, org_context):
        result = asyncio_run(
            _forecast_rules({"action": "delete"}, db, "user-001", org_context)
        )
        assert result["success"] is False
        assert "Unknown action" in result["error"]


# ======================================================================
# forecast_run  (error paths only — actual ML requires dependencies)
# ======================================================================


class TestForecastRun:
    def test_target_not_found(self, db, org_context):
        result = asyncio_run(
            _forecast_run({"target_id": "nonexistent"}, db, "user-001", org_context)
        )
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_no_targets_no_op(self, db, org_context):
        """Running all targets when none are active returns empty list."""
        result = asyncio_run(
            _forecast_run({}, db, "user-001", org_context)
        )
        assert result["success"] is True
        assert result["count"] == 0


# ======================================================================
# Async test runner
# ======================================================================

import asyncio


def asyncio_run(coro):
    """Synchronous bridge for async handlers in pytest."""
    return asyncio.get_event_loop().run_until_complete(coro)
