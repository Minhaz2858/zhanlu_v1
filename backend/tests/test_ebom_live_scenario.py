"""EBOM — live automotive scenario through the ACTUAL application HTTP routes.

Drives the real /api/ontology/* endpoints via TestClient (auth + db overrides) to
prove the complete business flow without a browser:
  business question -> governed computation -> declining detection ->
  recommendation -> (agent investigation reused) -> traceable outputs.
Also asserts organization isolation (a different org sees none of this data).
"""

import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_ebom_live.db")

import tempfile
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.deps import get_current_user_required, get_db
from app.routers.ontology import router as ontology_router
from tests.sandbox_seed import (
    METRIC_GROSS_MARGIN, ORG, build_sandbox, create_data_db,
    make_engine, make_session,
)


@pytest.fixture()
def client():
    # Ephemeral engine per test (proven pattern) — the override targets the
    # get_db the router actually uses (app.deps.get_db).
    eng = make_engine()
    s = make_session(eng)
    data_path = tempfile.mktemp(suffix=".db")
    create_data_db(data_path)
    build_sandbox(s, data_path)
    s.commit()
    app = FastAPI()
    app.include_router(ontology_router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: s
    user = SimpleNamespace(id="u-owner", org_id=ORG, role="org_admin")
    app.dependency_overrides[get_current_user_required] = lambda: user
    yield TestClient(app)
    s.close()
    eng.dispose()


def test_automotive_profitability_scenario(client, monkeypatch):
    owner = SimpleNamespace(id="u-owner", org_id=ORG, role="org_admin")

    # 1) Governed metric catalog is discoverable.
    r = client.get("/api/ontology/metrics")
    assert r.status_code == 200
    ids = {m["id"] for m in r.json()["metrics"]}
    assert METRIC_GROSS_MARGIN in ids

    # 2) Deterministic, governed period comparison (current vs prior).
    r = client.get(f"/api/ontology/metrics/{METRIC_GROSS_MARGIN}/compare")
    assert r.status_code == 200
    cmp = r.json()
    assert cmp["current_period"] == "2026-10"
    assert cmp["prior_period"] == "2026-09"
    assert cmp["current"]["value"] == 1880.0
    assert cmp["prior"]["value"] == 1960.0
    assert cmp["current"]["provenance"]["base_table"] == "sales_transactions"

    # 3) Multi-period trend (governed, auto-discovered periods).
    r = client.get(f"/api/ontology/metrics/{METRIC_GROSS_MARGIN}/trend")
    assert r.status_code == 200
    periods = {p["period"]: p["value"] for p in r.json()["periods"]}
    assert periods.get("2026-10") == 1880.0

    # 4) Deterministic declining-profitability detection (the business answer).
    r = client.get(
        f"/api/ontology/metrics/{METRIC_GROSS_MARGIN}/segments-compare"
        "?segment_column=model_id")
    assert r.status_code == 200
    seg = r.json()
    assert seg["declining_segments"] == ["v-001"]  # only Model X declined
    by_seg = {s["segment"]: s for s in seg["segments"]}
    assert by_seg["v-001"]["current_value"] == 750.0
    assert by_seg["v-001"]["prior_value"] == 890.0

    # 5) Organization isolation — a different org sees none of this data.
    other = SimpleNamespace(id="u-other", org_id="other-org", role="org_admin")
    client.app.dependency_overrides[get_current_user_required] = lambda: other
    r = client.get(
        f"/api/ontology/metrics/{METRIC_GROSS_MARGIN}/segments-compare"
        "?segment_column=model_id")
    assert r.status_code == 200
    assert r.json().get("unavailable") is True  # no leaked cross-org data
    client.app.dependency_overrides[get_current_user_required] = lambda: owner

    # 6) Situation Room reuses the existing Agent Harness (scoped to this org).
    captured = {}

    async def fake(args, db, user_id, context):
        captured["q"] = args["question"]
        captured["kb"] = context["bound_kb_ids"]
        return {"success": True, "answer": "Model X margin fell on rising cost."}

    import app.services.tool_handlers.delegation_tools as dt
    monkeypatch.setattr(dt, "_ask_data_agent", fake)
    r = client.post(
        "/api/ontology/situation/investigate",
        json={"question": "Why is Model X profitability declining?"})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert "Model X" in body["answer"]
    assert captured["q"] == "Why is Model X profitability declining?"
    assert captured["kb"]
