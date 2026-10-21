"""query_database inherits the caller's data scope + honest pipeline failure.

End-to-end root cause (2026-10-20, conv fa045471 - "give me last week top 10
customer list" produced a degenerate single-day stats report while the same
question on the sibling stack returned a perfect ranked table):

1. The v3 loop resolved the turn's data scope (bound KB 74c8d56d via the
   conversation's project) and passed it in the tool ctx as bound_kb_ids.
2. _query_database IGNORED that and re-ran prepare_data_source_runtime
   from scratch - where the general_assistant no-project anti-leak guard
   ZEROED the scope.
3. With no bound KBs the FSM's nl2sql nodes took the "schema-only" fallback:
   SQL was GENERATED but never EXECUTED against any database.
4. The extractor found no rows/data keys and returned
   "success: true, rows: [], Say the data has no matching rows" - a lie:
   nothing was ever queried.

Contract pinned here:
- ctx bound_kb_ids are INHERITED (authoritative); re-derivation is fallback;
- generated-but-never-executed SQL -> success=False with an explicit
  pipeline-failure error and a retry hint - never "no matching rows";
- an executed query that genuinely matched 0 rows stays an honest empty.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services.tool_handlers.query_database_tool import _query_database


def _obs(otype, rd, success=True, err=None):
    return SimpleNamespace(
        observation_type=otype, result_data=rd, success=success,
        error_message=err,
    )


class _FakeFSM:
    last_request = None
    observations = []

    def __init__(self, db):
        pass

    def run(self, req, *a, **kw):
        type(self).last_request = req
        return SimpleNamespace(
            observations=list(_FakeFSM.observations),
            execution=None, verification={},
        )


class _FakeSession:
    def close(self):
        pass


def _run(ctx, observations=None):
    observations = observations or []
    db = MagicMock()
    _FakeFSM.last_request = None
    _FakeFSM.observations = observations
    with patch("app.services.synexia.fsm.SynexiaFSM", _FakeFSM), \
            patch("app.database.SessionLocal", _FakeSession):
        ctx = {"org_id": "default-org", "app_id": "default-app", **(ctx or {})}
        return asyncio.run(_query_database(
            {"question": "top 10 customers last week"},
            db=db, user_id="u1", context=ctx,
        ))


class TestScopeInheritance:
    def test_ctx_bound_kb_ids_are_inherited(self):
        _run({"bound_kb_ids": ["kb-74c8d56d"], "agent_name": "general_assistant"})
        req = _FakeFSM.last_request
        assert req is not None
        assert (req.data_ctx_extras or {}).get("bound_kb_ids") == ["kb-74c8d56d"]

    def test_no_ctx_scope_falls_back_to_reprep(self):
        with patch(
            "app.services.data_source_runtime.data_source_runtime."
            "prepare_data_source_runtime",
            return_value=([], "", {"bound_kb_ids": ["kb-derived"]}),
        ):
            _run({})
        req = _FakeFSM.last_request
        assert (req.data_ctx_extras or {}).get("bound_kb_ids") == ["kb-derived"]


class TestHonestPipelineFailure:
    def test_generated_but_never_executed_sql_is_a_failure_not_empty(self):
        obs = [
            _obs("nl2sql", {"sql": "SELECT customer_name ...", "valid": True}),
            _obs("nl2sql", {"sql": "SELECT SUM(FALLAMOUNT) ...", "valid": True}),
            _obs("synthesize", {"summary": "No data returned for this query."}),
        ]
        out = _run({"bound_kb_ids": ["kb-x"]}, obs)
        assert out["success"] is False
        assert "did not execute" in out["error"]
        assert "no matching rows" not in out.get("note", "")
        assert "ask_data_agent" in out["hint"]
        assert out["sql"]

    def test_executed_query_with_zero_rows_is_honest_empty(self):
        obs = [_obs("nl2sql", {
            "sql": "SELECT ... WHERE 1=0", "data": [],
            "source_name": "aipdp_data_warehouse_prod",
        })]
        out = _run({"bound_kb_ids": ["kb-x"]}, obs)
        assert out["success"] is True
        assert out["row_count"] == 0
        assert "returned no rows" in out["note"]
        assert out["sql"] == "SELECT ... WHERE 1=0"

    def test_failed_nl2sql_observations_are_pipeline_failures(self):
        # 2026-10-20 contract UPDATE: a connection-level failure is now
        # reported as an UNREACHABLE environment error (the real cause),
        # not mislabeled as a binding failure.
        obs = [_obs("nl2sql", None, success=False,
                    err="Data source query failed: connection refused")]
        out = _run({"bound_kb_ids": ["kb-x"]}, obs)
        assert out["success"] is False
        assert "could not be reached" in out["error"]
        assert out["unreachable"] is True
        assert "connection refused" in out["detail"]
        assert "no matching rows" not in out.get("hint", "")


class TestHappyPath:
    def test_grounded_rows_extracted(self):
        obs = [_obs("nl2sql", {
            "sql": "SELECT ...", "data": [{"customer": "ACME", "rev": 100}],
            "source_name": "aipdp_data_warehouse_prod",
        })]
        out = _run({"bound_kb_ids": ["kb-x"]}, obs)
        assert out["success"] is True
        assert out["row_count"] == 1
        assert out["rows"] == [{"customer": "ACME", "rev": 100}]
        assert out["source"] == "aipdp_data_warehouse_prod"
