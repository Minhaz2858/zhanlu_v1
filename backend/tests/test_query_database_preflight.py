"""query_database pre-flight probe, circuit breaker, and plan-node error
propagation.

Root cause fixed here (2026-10-20, conv eaa1bfba / c341f610 — "give me last
week top 10 customer list" took 357s and 218s+):

1. The warehouse MySQL (10.10.10.49) flaps. Every query_database attempt
   burned 30-60s of LLM pipeline calls BEFORE a 15s connect timeout revealed
   the DB was dead, then reliability retries + replans repeated the whole
   thing → 5-6 minute turns.
   Fix: a cached 2.5s TCP probe + circuit breaker runs BEFORE the FSM run
   and fails fast with an honest "unreachable" error.

2. The tool misreported the failure cause: the real reasons ("Can't connect
   ... timed out", "Model did not produce a SQL statement") lived on failed
   PLAN NODES while observations carried neither rows nor errors, so the
   tool claimed "no bound data source reached the pipeline" for a network
   outage. Fix: fsm.py plan_summary now carries failed-node errors and the
   tool reports environmental failures honestly.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.tool_handlers.query_database_tool import (
    _probe_tcp,
    _query_database,
    _reset_probe_state,
)


def _obs(otype, rd, success=True, err=None):
    return SimpleNamespace(
        observation_type=otype, result_data=rd, success=success,
        error_message=err,
    )


class _FakeFSM:
    last_request = None
    observations = []
    plan_summary = None
    calls = 0

    def __init__(self, db):
        pass

    def run(self, req, *a, **kw):
        type(self).calls += 1
        type(self).last_request = req
        return SimpleNamespace(
            observations=list(_FakeFSM.observations),
            plan_summary=_FakeFSM.plan_summary,
            execution=None, verification={},
        )


class _FakeSession:
    def close(self):
        pass


def _kb_row(kb_id="kb-x", host="10.10.10.49", port=3306, name="warehouse"):
    return SimpleNamespace(id=kb_id, host=host, port=port, name=name)


def _run(ctx, observations=None, plan_summary=None, kb_rows=None,
         probe_returns=True):
    """Run the tool with a MagicMock db whose KB lookup returns kb_rows."""
    observations = observations or []
    kb_rows = kb_rows if kb_rows is not None else []
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = list(kb_rows)
    _FakeFSM.last_request = None
    _FakeFSM.calls = 0
    _FakeFSM.observations = observations
    _FakeFSM.plan_summary = plan_summary
    _reset_probe_state()
    with patch("app.services.synexia.fsm.SynexiaFSM", _FakeFSM), \
            patch("app.database.SessionLocal", _FakeSession), \
            patch(
                "app.services.tool_handlers.query_database_tool._probe_tcp",
                return_value=probe_returns,
            ):
        ctx = {"org_id": "default-org", "app_id": "default-app", **(ctx or {})}
        return asyncio.run(_query_database(
            {"question": "top 10 customers last week"},
            db=db, user_id="u1", context=ctx,
        )), _FakeFSM


@pytest.fixture(autouse=True)
def _clean_probe_state():
    _reset_probe_state()
    yield
    _reset_probe_state()


class TestPreflightProbe:
    def test_unreachable_source_fails_fast_without_running_pipeline(self):
        out, fsm = _run(
            {"bound_kb_ids": ["kb-x"]},
            kb_rows=[_kb_row()],
            probe_returns=False,
        )
        assert out["success"] is False
        assert out["unreachable"] is True
        assert "unreachable" in out["error"]
        assert "10.10.10.49:3306" in out["error"]
        assert "network/VPN" in out["detail"]
        assert "Do NOT retry" in out["hint"]
        # The whole point: zero LLM pipeline work against a dead host.
        assert fsm.calls == 0
        assert fsm.last_request is None

    def test_reachable_source_runs_pipeline_normally(self):
        obs = [_obs("nl2sql", {
            "sql": "SELECT ...", "data": [{"c": "ACME", "rev": 1}],
            "source_name": "warehouse",
        })]
        out, fsm = _run(
            {"bound_kb_ids": ["kb-x"]},
            observations=obs,
            kb_rows=[_kb_row()],
            probe_returns=True,
        )
        assert out["success"] is True
        assert fsm.calls == 1

    def test_file_backed_kb_without_host_skips_probe(self):
        out, fsm = _run(
            {"bound_kb_ids": ["kb-file"]},
            kb_rows=[_kb_row(host="")],
            probe_returns=False,  # would fail if a host were probed
        )
        assert fsm.calls == 1  # pipeline ran; no probe target existed
        assert out["success"] is True or out.get("unreachable") is None

    def test_breaker_opens_after_first_failed_probe(self):
        # Call 1: probe fails → breaker opens for the backoff window.
        out1, fsm1 = _run(
            {"bound_kb_ids": ["kb-x"]},
            kb_rows=[_kb_row()],
            probe_returns=False,
        )
        assert out1["unreachable"] is True
        assert "probe failed" in out1["error"]
        # Call 2: breaker is OPEN — no second probe, instant honest error.
        with patch(
            "app.services.tool_handlers.query_database_tool._probe_tcp",
            side_effect=AssertionError("probe must not run while breaker open"),
        ) as probe_mock:
            db = MagicMock()
            db.query.return_value.filter.return_value.all.return_value = [
                _kb_row()
            ]
            with patch("app.services.synexia.fsm.SynexiaFSM", _FakeFSM), \
                    patch("app.database.SessionLocal", _FakeSession):
                out2 = asyncio.run(_query_database(
                    {"question": "top 10 customers last week"},
                    db=db, user_id="u1",
                    context={"org_id": "default-org", "app_id": "default-app",
                             "bound_kb_ids": ["kb-x"]},
                ))
        probe_mock.assert_not_called()
        assert out2["unreachable"] is True
        assert "circuit breaker" in out2["error"]

    def test_probe_success_cached_no_repeated_probes(self):
        # Two consecutive calls with a reachable host: probe called once
        # (TTL cache), pipeline runs both times.
        with patch(
            "app.services.tool_handlers.query_database_tool._probe_tcp",
            return_value=True,
        ) as probe_mock:
            for _ in range(2):
                db = MagicMock()
                db.query.return_value.filter.return_value.all.return_value = [
                    _kb_row()
                ]
                with patch("app.services.synexia.fsm.SynexiaFSM", _FakeFSM), \
                        patch("app.database.SessionLocal", _FakeSession):
                    asyncio.run(_query_database(
                        {"question": "top 10 customers last week"},
                        db=db, user_id="u1",
                        context={"org_id": "default-org",
                                 "app_id": "default-app",
                                 "bound_kb_ids": ["kb-x"]},
                    ))
        assert probe_mock.call_count == 1

    def test_probe_disabled_runs_pipeline_without_check(self):
        # Flag-off path: settings override via patch — pipeline runs, no probe.
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [_kb_row()]
        _FakeFSM.calls = 0
        _FakeFSM.observations = []
        _FakeFSM.plan_summary = None
        _reset_probe_state()
        with patch(
            "app.services.tool_handlers.query_database_tool._probe_tcp",
            side_effect=AssertionError("probe disabled — must not run"),
        ), patch(
            "app.services.synexia.fsm.SynexiaFSM", _FakeFSM
        ), patch("app.database.SessionLocal", _FakeSession), \
                patch("app.config.settings.NL2SQL_PREFLIGHT_PROBE_ENABLED",
                      False):
            out = asyncio.run(_query_database(
                {"question": "top 10 customers last week"},
                db=db, user_id="u1",
                context={"org_id": "default-org", "app_id": "default-app",
                         "bound_kb_ids": ["kb-x"]},
            ))
        assert _FakeFSM.calls == 1


class TestPlanNodeErrorPropagation:
    def test_connect_timeout_on_plan_node_is_reported_as_unreachable(self):
        # The exact live failure shape (conv c341f610): no observations with
        # rows/errors, but the plan node holds the truth.
        plan_summary = {"nodes": [
            {"seq": 1, "name": "Retrieve totals", "node_type": "nl2sql",
             "status": "failed",
             "error": (
                 "Query failed: (pymysql.err.OperationalError) (2003, "
                 "\"Can't connect to MySQL server on '10.10.10.49' "
                 "(timed out)\")"
             )},
            {"seq": 2, "name": "Finalize", "node_type": "synthesize",
             "status": "completed"},
        ]}
        out, _ = _run({"bound_kb_ids": ["kb-x"]}, plan_summary=plan_summary)
        assert out["success"] is False
        assert out["unreachable"] is True
        assert "could not be reached" in out["error"]
        assert "timed out" in out["error"]
        assert "environment problem" in out["hint"]
        # NOT the old misleading message:
        assert "no bound data source reached the pipeline" not in out["error"]

    def test_structural_node_failure_keeps_binding_failure_message(self):
        plan_summary = {"nodes": [
            {"seq": 1, "name": "Generate SQL", "node_type": "nl2sql",
             "status": "failed",
             "error": "Model did not produce a SQL statement."},
        ]}
        out, _ = _run({"bound_kb_ids": ["kb-x"]}, plan_summary=plan_summary)
        assert out["success"] is False
        assert out.get("unreachable") is None
        assert "did not execute" in out["error"]
        # The node's real error is now visible in detail for diagnosis.
        assert "Model did not produce a SQL statement" in out["detail"]

    def test_observations_still_take_priority_over_node_errors(self):
        obs = [_obs("nl2sql", {
            "sql": "SELECT ...", "data": [{"c": "ACME"}],
            "source_name": "warehouse",
        })]
        plan_summary = {"nodes": [
            {"seq": 1, "name": "Retrieve totals", "node_type": "nl2sql",
             "status": "failed", "error": "some stale failure"},
        ]}
        out, _ = _run(
            {"bound_kb_ids": ["kb-x"]},
            observations=obs,
            plan_summary=plan_summary,
        )
        assert out["success"] is True
        assert out["row_count"] == 1
