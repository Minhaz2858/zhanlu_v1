"""EBOM — governed period-aware metric analysis (Experiences A + B backend).

Verifies:
- filter_overrides changes the period WITHOUT bypassing MetricDefinition
  (base_table/base_column/aggregation stay authoritative).
- compare_metric_periods flags declining segments deterministically.
- discover_distinct returns segment values through the execution gate.
- trend_metric returns a multi-period series (unavailable periods = null).
All numbers come from the governed compute path — no LLM, no fabrication.
"""

import os
import tempfile
from types import SimpleNamespace

import pytest

from app.routers.ontology import (
    metric_compare_endpoint,
    metric_segments_compare_endpoint,
    metric_trend_endpoint,
)
from app.services.ontology.metrics import (
    compare_metric_periods, compute_metric, discover_distinct,
    resolve_metric, trend_metric,
)
from tests.sandbox_seed import (
    METRIC_GROSS_MARGIN, ORG, build_sandbox, create_data_db,
    make_engine, make_session,
)


def _user():
    return SimpleNamespace(id="u-owner", org_id=ORG, role="org_admin")


def _ctx():
    eng = make_engine()
    d = tempfile.mkdtemp()
    p = os.path.join(d, "s.db")
    create_data_db(p)
    s = make_session(eng)
    build_sandbox(s, p)
    return s


def test_filter_overrides_period_aware():
    s = _ctx()
    cur = compute_metric(
        s, ORG, METRIC_GROSS_MARGIN,
        filter_overrides={"sale_month": "2026-10"})
    prev = compute_metric(
        s, ORG, METRIC_GROSS_MARGIN,
        filter_overrides={"sale_month": "2026-09"})
    assert cur["success"] and cur["value"] == 1880.0   # 2026-10 total
    assert prev["success"] and prev["value"] == 1960.0  # 2026-09 total
    # MetricDefinition stays authoritative: base_table/column unchanged.
    assert prev["provenance"]["base_table"] == "sales_transactions"
    assert "sale_month" in prev["provenance"]["filter_overrides_applied"]


def test_compare_segments_flags_decline():
    s = _ctx()
    out = compare_metric_periods(
        s, ORG, METRIC_GROSS_MARGIN,
        current_period="2026-10", prior_period="2026-09",
        segment_column="model_id", segments=["v-001", "v-002", "v-003"])
    assert out["metric"]["name"] == "GrossMargin"
    assert out["declining_segments"] == ["v-001"]  # only Model X declined
    rows = {r["segment"]: r for r in out["segments"]}
    assert rows["v-001"]["current_value"] == 750.0
    assert rows["v-001"]["prior_value"] == 890.0
    assert rows["v-001"]["declining"] is True
    assert rows["v-002"]["declining"] is False


def test_discover_distinct_models():
    s = _ctx()
    metric, _ = resolve_metric(s, ORG, METRIC_GROSS_MARGIN)
    found = discover_distinct(s, ORG, metric, "model_id", periods=["2026-09", "2026-10"])
    assert set(found) == {"v-001", "v-002", "v-003"}


def test_trend_series():
    s = _ctx()
    out = trend_metric(
        s, ORG, METRIC_GROSS_MARGIN, periods=["2026-09", "2026-10"])
    assert [p["value"] for p in out["periods"]] == [1960.0, 1880.0]
    # A missing period is reported as null, never fabricated.
    missing = trend_metric(s, ORG, METRIC_GROSS_MARGIN, periods=["2026-07"])
    assert missing["periods"][0]["value"] is None


def test_segments_compare_endpoint_autodiscovery():
    s = _ctx()
    out = metric_segments_compare_endpoint(
        METRIC_GROSS_MARGIN, segment_column="model_id",
        current_period="2026-10", prior_period="2026-09",
        segments=None, extra_filters=None, org_id=None, db=s, user=_user())
    assert out["declining_segments"] == ["v-001"]


def test_compare_endpoint_unavailable_metric():
    s = _ctx()
    out = metric_compare_endpoint(
        "does-not-exist", current_period="2026-10", prior_period="2026-09",
        extra_filters=None, org_id=None, db=s, user=_user())
    assert out["success"] is False and out["unavailable"] is True


def test_trend_endpoint_bad_periods_json():
    s = _ctx()
    with pytest.raises(Exception):  # HTTPException 400
        metric_trend_endpoint(
            METRIC_GROSS_MARGIN, periods="not-json", org_id=None, db=s, user=_user())


def test_compare_autodiscovers_periods():
    s = _ctx()
    # No periods passed -> latest two discovered (2026-10, 2026-09).
    out = metric_compare_endpoint(
        METRIC_GROSS_MARGIN, current_period=None, prior_period=None,
        extra_filters=None, org_id=None, db=s, user=_user())
    assert out["current_period"] == "2026-10"
    assert out["prior_period"] == "2026-09"
    assert out["current"]["value"] == 1880.0
    assert out["prior"]["value"] == 1960.0


def test_segments_compare_autodiscovers_periods_and_segments():
    s = _ctx()
    out = metric_segments_compare_endpoint(
        METRIC_GROSS_MARGIN, segment_column="model_id",
        current_period=None, prior_period=None, segments=None,
        extra_filters=None, org_id=None, db=s, user=_user())
    assert out["current_period"] == "2026-10"
    assert out["declining_segments"] == ["v-001"]


def test_trend_autodiscovers_periods():
    s = _ctx()
    out = metric_trend_endpoint(
        METRIC_GROSS_MARGIN, periods=None, extra_filters=None,
        org_id=None, db=s, user=_user())
    # Discovered latest 6 -> at least the two seeded months, ascending.
    vals = [p["period"] for p in out["periods"]]
    assert "2026-10" in vals and "2026-09" in vals
