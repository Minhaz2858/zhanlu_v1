"""EBOM — governed metric compute endpoint (Experience A backend).

Verifies the additive `GET /api/ontology/metrics/{ref}/compute` and
`GET /api/ontology/metrics` reuse the deterministic, typed-unavailable
`compute_metric` path. No LLM may compute a figure; an unavailable metric is
reported, never fabricated.
"""

import os
import tempfile
from types import SimpleNamespace

import pytest

from app.routers.ontology import compute_metric_endpoint, list_metrics
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


def test_list_metrics_returns_governed_catalog():
    s = _ctx()
    out = list_metrics(org_id=None, db=s, user=_user())
    ids = {m["id"] for m in out["metrics"]}
    assert METRIC_GROSS_MARGIN in ids
    gm = next(m for m in out["metrics"] if m["id"] == METRIC_GROSS_MARGIN)
    # All lifecycles returned so the UI can show honest unavailable states.
    assert gm["lifecycle"] == "published"
    assert gm["is_enabled"] is True


def test_compute_endpoint_returns_typed_value():
    s = _ctx()
    out = compute_metric_endpoint(
        METRIC_GROSS_MARGIN, filters='{"sale_month":"2026-10"}',
        org_id=None, db=s, user=_user())
    assert out["success"] is True
    assert out["unavailable"] is False
    # Deterministic: sum of margin over the six 2026-10 rows = 1880.0
    assert out["value"] == 1880.0
    assert out["provenance"]["base_table"] == "sales_transactions"
    # D3 — dependency edges are surfaced for governance visibility.
    assert out["metric"]["depends_on_metrics"]


def test_compute_endpoint_unavailable_for_missing_metric():
    s = _ctx()
    out = compute_metric_endpoint("does-not-exist", filters=None,
                                  org_id=None, db=s, user=_user())
    assert out["success"] is False
    assert out["unavailable"] is True
    assert "not found" in out["reason"]


def test_compute_endpoint_rejects_bad_filters_json():
    s = _ctx()
    with pytest.raises(Exception):  # HTTPException 400
        compute_metric_endpoint(
            METRIC_GROSS_MARGIN, filters="not-json", org_id=None, db=s, user=_user())
