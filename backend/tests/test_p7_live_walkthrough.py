"""P7 live enterprise scenario (workstream 5) — the integrated business answer.

Business question: gross margin by vehicle model this month, and which models
have experienced declining profitability. Proves the FULL chain end-to-end using
only the governed compute path (no fabricated formulas, deterministic, with
provenance). Run: python3 -m pytest tests/test_p7_live_walkthrough.py -q
"""

from __future__ import annotations

import pytest
from tests.sandbox_seed import (
    ORG, METRIC_GROSS_MARGIN, create_data_db, build_sandbox, make_engine,
    make_session, run_business_question,
)


def test_live_business_question(env):
    eng, session, _ = env
    out = run_business_question(session, None)
    by_model = out["gross_margin_by_model"]
    # Model X current-month margin = 750 (verified above)
    x = by_model["Model X"]
    assert x["success"] is True
    assert x["value"] == 750.0
    # Provenance + execution trace are carried (traceable visualization)
    assert out["provenance"] is not None
    assert out["trace"] is not None
    assert out["trace"].get("gate") == "passed"
    # Declining profitability correctly flags Model X (750 < 890 prior month)
    declining = {d["model"] for d in out["declining_models"]}
    assert "Model X" in declining
    # The other two models did NOT decline
    assert "Model Y" not in declining and "Model Z" not in declining


@pytest.fixture()
def env(tmp_path):
    eng = make_engine()
    data_path = str(tmp_path / "sales.db")
    create_data_db(data_path)
    session = make_session(eng)
    build_sandbox(session, data_path)
    return eng, session, data_path
