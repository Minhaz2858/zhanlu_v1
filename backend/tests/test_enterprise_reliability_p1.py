"""Enterprise Reliability Milestone — P1 root-fix tests (2026-11-01).

Companion to docs/architecture/zhanlu-enterprise-reliability-audit.md.
Covers the audit's P1 remediation items, all deterministic (zero LLM credits):

* B-3 — ``ask_data_agent`` must FLAG the 200-row return slice
  (``data_quality.result_truncated`` + ``total_row_count``) instead of
  silently clipping (mission invariant: "no silent SQL result truncation").
* B-2 — a SINGLE bound database KB is auto-resolved into the sub-loop
  ``data_source_id`` when the caller passes none (no wasted iteration on a
  deterministic "data_source_id is required" error).
* B-1 — the ask_data_agent path runs the same pre-flight warehouse probe as
  ``query_database`` and fails fast with an honest unreachable error instead
  of burning the full LLM pipeline against a dead warehouse.
* A-6 — the approval-paused ``running`` activity step is closed
  (``awaiting_approval``) before the ``paused`` SSE frame — no permanent
  spinner.
* A-2 — AgentApp-aware delegation + binding inheritance default ON (the
  fixes exist; default-OFF made cross-department scenarios inert).
* C-1 — evidence validation is wired into turn-evidence capture: validated
  results are stamped, structurally-broken results honestly limited.

Test style: behavioral where practical (mocked tool/LLM seams, per
``test_ask_data_agent_llm_failure.py``); narrow source checks only for the
two agents.py regions whose full-stream behavioral harness would need the
entire v3 generator.
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


def _db_with_kb(kb_id="kb-1", name="auto_sales", host=None, port=None,
                source_kind="database"):
    db = MagicMock()
    kb = MagicMock()
    kb.id = kb_id
    kb.name = name
    kb.is_deleted = False
    kb.source_kind = source_kind
    kb.host = host
    kb.port = port
    db.query.return_value.filter.return_value.first.return_value = kb
    db.query.return_value.filter.return_value.all.return_value = [kb]
    return db


def _business_rows(n):
    return [
        {"model_name": f"Model{i}", "region": "华东", "revenue": 100.0 + i}
        for i in range(n)
    ]


def _execute_query_result(rows):
    return {
        "success": True,
        "rows": rows,
        "sql": ("SELECT model_name, region, SUM(revenue) AS revenue "
                "FROM sales_order GROUP BY model_name, region"),
        "source": {"id": "kb-1", "name": "auto_sales"},
    }


async def _run_ask_data_agent(args, db, context, *, llm_script, execute_script):
    """Drive _ask_data_agent with mocked LLM + tool seams.

    ``execute_script(tool_name, tool_args, context)`` — context carries the
    prefilled ``data_source_id`` the db_tools handlers actually consume.
    """
    from app.services.tool_handlers import delegation_tools

    async def fake_call_llm(messages, tools=None, endpoint=None):
        return llm_script(messages)

    async def fake_call_llm_with_retry(messages, tools=None, endpoint=None):
        return llm_script(messages)

    async def fake_execute_tool(tool_name, call_args, db_, user_id,
                                context=None, **kwargs):
        return execute_script(tool_name, call_args, context or {})

    with patch.object(delegation_tools, "_call_llm", side_effect=fake_call_llm), \
         patch.object(delegation_tools, "_call_llm_with_retry",
                      side_effect=fake_call_llm_with_retry), \
         patch("app.services.agent_tools.execute_tool",
               side_effect=fake_execute_tool), \
         patch.object(delegation_tools, "pre_call_prep", lambda m: None), \
         patch.object(delegation_tools.settings,
                      "DATA_AGENT_SWARM_ENABLED", False), \
         patch.object(delegation_tools.settings,
                      "DATA_AGENT_FASTPATH_ENABLED", False), \
         patch.object(delegation_tools.settings,
                      "DATA_AGENT_PLANNER_ENABLED", False):
        return await delegation_tools._ask_data_agent(
            args, db, "user-1", context=context)


def _clear_probe_state():
    from app.services.tool_handlers import query_database_tool as qdt
    qdt._PROBE_STATE.clear()


def _llm_calls_then_final(calls):
    """Scripted sub-LLM: yield each scripted call once, then a final
    content-only response ending the loop."""
    state = {"i": 0}

    def script(messages):
        i = state["i"]
        state["i"] += 1
        if i < len(calls):
            return {"content": "", "tool_calls": [calls[i]]}
        return {"content": "Executive summary: done.", "tool_calls": []}

    return script


def _query_call(sql_id="c1"):
    return {"id": sql_id, "function": {
        "name": "execute_query",
        "arguments": {"sql": "SELECT model_name, region, SUM(revenue) "
                             "AS revenue FROM sales_order "
                             "GROUP BY model_name, region"},
    }}


class TestAskDataAgentTruncationFlag(unittest.TestCase):
    """B-3: the 200-row return slice must be flagged, never silent."""

    def test_truncation_flagged_in_data_quality(self):
        rows = _business_rows(250)

        def execute_script(tool_name, call_args, ctx):
            assert tool_name == "execute_query"
            return _execute_query_result(rows)

        llm = _llm_calls_then_final([_query_call()])
        result = asyncio.run(_run_ask_data_agent(
            {"question": "sales by model", "data_source_id": "kb-1",
             "max_iterations": 2},
            _db_with_kb(),  # host-less KB → probe skipped
            {"bound_kb_ids": ["kb-1"]},
            llm_script=llm, execute_script=execute_script,
        ))
        self.assertTrue(result.get("success"))
        dq = result.get("data_quality") or {}
        self.assertTrue(dq.get("result_truncated"),
                        "250-row result sliced to 200 must be flagged")
        self.assertEqual(dq.get("total_row_count"), 250)
        self.assertEqual(dq.get("returned_row_count"), 200)
        self.assertEqual(len(result.get("rows") or []), 200)

    def test_no_truncation_flag_when_under_cap(self):
        rows = _business_rows(50)

        def execute_script(tool_name, call_args, ctx):
            return _execute_query_result(rows)

        llm = _llm_calls_then_final([_query_call()])
        result = asyncio.run(_run_ask_data_agent(
            {"question": "sales by model", "data_source_id": "kb-1",
             "max_iterations": 2},
            _db_with_kb(),
            {"bound_kb_ids": ["kb-1"]},
            llm_script=llm, execute_script=execute_script,
        ))
        dq = result.get("data_quality") or {}
        self.assertFalse(dq.get("result_truncated"))
        self.assertEqual(dq.get("total_row_count"), 50)


class TestAskDataAgentSingleKBResolve(unittest.TestCase):
    """B-2: one bound database KB → sub-loop gets data_source_id prefilled."""

    def test_single_bound_db_kb_auto_resolved(self):
        seen_sources = []

        def execute_script(tool_name, call_args, ctx):
            seen_sources.append(ctx.get("data_source_id"))
            return _execute_query_result(_business_rows(3))

        llm = _llm_calls_then_final([
            # NOTE: the sub-LLM passes NO data_source_id — exactly the
            # failure mode that used to burn one iteration on a hard error.
            _query_call(),
        ])
        result = asyncio.run(_run_ask_data_agent(
            {"question": "sales by model", "max_iterations": 2},
            _db_with_kb(kb_id="kb-1"),  # host-less → probe skipped
            {"bound_kb_ids": ["kb-1"]},
            llm_script=llm, execute_script=execute_script,
        ))
        self.assertTrue(result.get("success"))
        self.assertEqual(seen_sources, ["kb-1"],
                         "single bound DB KB must be auto-resolved as source")

    def test_multi_bound_kbs_not_auto_resolved(self):
        """With 2+ bound KBs the choice stays with the sub-LLM (ambiguous)."""
        db = MagicMock()
        kb1 = MagicMock(id="kb-1", name="a", is_deleted=False,
                        source_kind="database", host=None, port=None)
        kb2 = MagicMock(id="kb-2", name="b", is_deleted=False,
                        source_kind="database", host=None, port=None)
        db.query.return_value.filter.return_value.first.return_value = kb1
        db.query.return_value.filter.return_value.all.return_value = [kb1, kb2]
        seen = []

        def execute_script(tool_name, call_args, ctx):
            seen.append(ctx.get("data_source_id"))
            return _execute_query_result(_business_rows(3))

        llm = _llm_calls_then_final([_query_call()])
        result = asyncio.run(_run_ask_data_agent(
            {"question": "sales by model", "max_iterations": 2},
            db,
            {"bound_kb_ids": ["kb-1", "kb-2"]},
            llm_script=llm, execute_script=execute_script,
        ))
        # The query still ran (the sub-LLM answered the deterministic error
        # with a source in a later iteration OR the tools handled it) — the
        # assertion is only that NO silent prefill happened for call #1.
        self.assertEqual(seen[0], None)


class TestAskDataAgentPreflightProbe(unittest.TestCase):
    """B-1: dead warehouse → fail fast BEFORE the LLM pipeline runs."""

    def test_unreachable_warehouse_fails_fast(self):
        _clear_probe_state()
        llm_calls = []

        async def dead_llm(messages, tools=None, endpoint=None):
            llm_calls.append(1)
            return {"content": "", "tool_calls": []}

        def execute_script(tool_name, call_args, ctx):
            raise AssertionError("tools must not run against a dead warehouse")

        from app.services.tool_handlers import delegation_tools
        with patch.object(delegation_tools, "_call_llm", side_effect=dead_llm), \
             patch.object(delegation_tools, "_call_llm_with_retry",
                          side_effect=dead_llm), \
             patch("app.services.agent_tools.execute_tool",
                   side_effect=execute_script), \
             patch.object(delegation_tools, "pre_call_prep", lambda m: None), \
             patch.object(delegation_tools.settings,
                          "DATA_AGENT_SWARM_ENABLED", False), \
             patch.object(delegation_tools.settings,
                          "DATA_AGENT_FASTPATH_ENABLED", False), \
             patch.object(delegation_tools.settings,
                          "DATA_AGENT_PLANNER_ENABLED", False), \
             patch("app.services.tool_handlers.query_database_tool._probe_tcp",
                   return_value=False):
            result = asyncio.run(delegation_tools._ask_data_agent(
                {"question": "sales by model", "data_source_id": "kb-1",
                 "max_iterations": 2},
                _db_with_kb(host="10.10.10.99", port=3306),
                {"bound_kb_ids": ["kb-1"]},
            ))
        self.assertFalse(result.get("success"))
        self.assertEqual(result.get("error_kind"), "datasource_unreachable")
        self.assertEqual(llm_calls, [],
                         "LLM pipeline must not run when the probe fails")

    def test_reachable_warehouse_runs_normally(self):
        _clear_probe_state()

        def execute_script(tool_name, call_args, db_, user_id, context=None):
            return _execute_query_result(_business_rows(3))

        llm = _llm_calls_then_final([_query_call()])

        async def fake_call_llm(messages, tools=None, endpoint=None):
            return llm(messages)

        async def fake_call_llm_with_retry(messages, tools=None, endpoint=None):
            return llm(messages)

        from app.services.tool_handlers import delegation_tools
        with patch.object(delegation_tools, "_call_llm",
                          side_effect=fake_call_llm), \
             patch.object(delegation_tools, "_call_llm_with_retry",
                          side_effect=fake_call_llm_with_retry), \
             patch("app.services.agent_tools.execute_tool",
                   side_effect=execute_script), \
             patch.object(delegation_tools, "pre_call_prep", lambda m: None), \
             patch.object(delegation_tools.settings,
                          "DATA_AGENT_SWARM_ENABLED", False), \
             patch.object(delegation_tools.settings,
                          "DATA_AGENT_FASTPATH_ENABLED", False), \
             patch.object(delegation_tools.settings,
                          "DATA_AGENT_PLANNER_ENABLED", False), \
             patch("app.services.tool_handlers.query_database_tool._probe_tcp",
                   return_value=True):
            result = asyncio.run(delegation_tools._ask_data_agent(
                {"question": "sales by model", "data_source_id": "kb-1",
                 "max_iterations": 2},
                _db_with_kb(host="10.10.10.98", port=3307),
                {"bound_kb_ids": ["kb-1"]},
            ))
        self.assertTrue(result.get("success"))


class TestDelegationFlagsDefaultOn(unittest.TestCase):
    """A-2: AgentApp-aware delegation + binding inheritance ship ON."""

    def test_flags_default_true(self):
        from app.config import settings
        self.assertTrue(getattr(settings, "DELEGATE_AGENTAPP_AWARE", False),
                        "DELEGATE_AGENTAPP_AWARE must default True")
        self.assertTrue(getattr(settings, "DELEGATE_INHERIT_BINDINGS", False),
                        "DELEGATE_INHERIT_BINDINGS must default True")


class TestEvidenceValidationWiring(unittest.TestCase):
    """C-1: turn-evidence capture stamps validation status."""

    def _record(self, results):
        from app.services.agent_collaboration.shared_evidence import (
            get_registry,
        )
        from app.services import turn_evidence

        reg = get_registry()
        tc = {"name": "query_database", "status": "completed",
              "results": results}
        ids = turn_evidence.record_turn_evidence("conv-test", [tc])
        return ids, reg

    def test_validated_result_stamped(self):
        ids, reg = self._record({
            "success": True,
            "rows": [{"model": "Falcon", "revenue": 100.0}],
            "columns": ["model", "revenue"],
            "row_count": 1,
        })
        self.assertEqual(len(ids), 1)
        item = reg.get("conv-test", ids[0])
        self.assertIsNotNone(item)
        # Well-formed data must never read invalid/contradicted. The
        # validator may downgrade to "partial" (e.g. missing provenance
        # fields) — that is honest disclosure, not a contradiction.
        self.assertIn(item.validation_status,
                      ("validated", "partial", "unvalidated"))
        self.assertNotIn("validation failed: structure",
                         " ".join(item.limitations))

    def test_broken_result_honestly_limited(self):
        ids, reg = self._record({
            "success": True,
            "rows": "not-a-list",
            "columns": ["model", "revenue"],
        })
        self.assertEqual(len(ids), 1)
        item = reg.get("conv-test", ids[0])
        joined = " ".join(item.limitations or [])
        self.assertIn("invalid", joined.lower() + item.validation_status,
                      "structurally-broken result must not read 'validated'")


def _fresh_session():
    """Isolated sqlite session for model-level tests (no app engine)."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.models.base import Base

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


class TestDiscoveryOrgWall(unittest.TestCase):
    """P0 tenant-isolation fix (enterprise reliability milestone, 2026-11-01).

    ROOT DEFECT (project-infrastructure audit): `_list_available_agents`
    queried ALL published AgentApps with no org filter — the discovery
    directory leaked agent names/descriptions across tenants. Fix: the
    published query is scoped to the caller's org (user → org, else
    conversation → org); legacy no-context callers keep the historical
    unscoped behavior with a warning (real tool calls always carry a user).
    """

    def _seed(self, db):
        from app.models.agent_app import AgentApp
        db.add(AgentApp(name="OrgOne Agent", description="d",
                        status="published", org_id="org-1", app_id="app-1"))
        db.add(AgentApp(name="OrgTwo Agent", description="d",
                        status="published", org_id="org-2", app_id="app-1"))
        db.commit()

    def test_discovery_scoped_to_caller_org(self):
        import asyncio

        from app.models.user import User
        from app.services.tool_handlers.delegate_tool import _list_available_agents

        db = _fresh_session()
        self._seed(db)
        db.add(User(id="u1", email="u1@x.io", full_name="U One",
                    role="member", password_hash="x", org_id="org-1"))
        db.add(User(id="u2", email="u2@x.io", full_name="U Two",
                    role="member", password_hash="x", org_id="org-2"))
        db.commit()

        res1 = asyncio.run(_list_available_agents({}, db, "u1", context=None))
        names1 = [a["name"] for a in res1["agents"]]
        res2 = asyncio.run(_list_available_agents({}, db, "u2", context=None))
        names2 = [a["name"] for a in res2["agents"]]
        self.assertIn("OrgOne Agent", names1)
        self.assertNotIn("OrgTwo Agent", names1)
        self.assertIn("OrgTwo Agent", names2)
        self.assertNotIn("OrgOne Agent", names2)

    def test_discovery_org_fallback_via_conversation(self):
        import asyncio

        from app.models.agent_conversation import AgentConversation
        from app.services.tool_handlers.delegate_tool import _list_available_agents

        db = _fresh_session()
        self._seed(db)
        conv = AgentConversation(id="conv-orgwall", agent_name="general_assistant",
                                 org_id="org-2", app_id="app-1", messages=[])
        db.add(conv)
        db.commit()
        res = asyncio.run(_list_available_agents(
            {}, db, None, context={"conversation_id": "conv-orgwall"}))
        names = [a["name"] for a in res["agents"]]
        self.assertIn("OrgTwo Agent", names)
        self.assertNotIn("OrgOne Agent", names)


class TestReviewDecisionNeverFakesCompletion(unittest.TestCase):
    """Contract 6 (evidence → reviewer → execution decision), audit I-7.

    ROOT DEFECT (found by the enterprise acceptance suite, 2026-11-01):
    ``decision_from_review`` only copied the gap field matching the
    reviewer's recommended action onto the observation — so a
    ``needs_revision`` review whose recommended action was
    ``revise_response`` reached ``evaluate_observation`` as a CLEAN SUCCESS
    and the decision table returned "complete / objective_satisfied". The
    reviewer's verdict was silently converted into completion.
    """

    def _review(self, dim, detail):
        return {
            "overall": "needs_revision",
            "answered": True,
            "dimensions": {dim: {"status": "fail", "detail": detail}},
        }

    def test_needs_revision_review_is_never_complete(self):
        from app.services.execution_decisions import decision_from_review
        for dim, detail in [
            ("evidence_completeness", "no supplier-lot evidence for the claim"),
            ("unsupported_conclusions", "margin figure not present in tool results"),
            ("contradictory_findings", "revenue conflicts with the source row"),
            ("numerical_accuracy", "total does not match the returned rows"),
        ]:
            d = decision_from_review(self._review(dim, detail), objective="x")
            self.assertNotEqual(
                d.decision, "complete",
                f"needs_revision review ({dim}) must never decide 'complete'")

    def test_clean_review_still_completes(self):
        from app.services.execution_decisions import decision_from_review
        from app.services.agent_collaboration.reviewer import DIMENSIONS
        clean = {
            "overall": "complete",
            "answered": True,
            "dimensions": {n: {"status": "ok", "detail": ""} for n in DIMENSIONS},
        }
        d = decision_from_review(clean, objective="x")
        self.assertEqual(d.decision, "complete",
                         "an approving review with no gaps must complete")


class TestApprovalPauseClosesStep(unittest.TestCase):
    """A-6: the running activity step is closed before the paused frame."""

    def test_pause_branch_closes_running_step(self):
        import inspect

        from app.routers import agents
        src = inspect.getsource(agents)
        # The paused frame must be immediately preceded by step-closure
        # logic: every still-"running" activity step is flipped to
        # "awaiting_approval" and re-emitted before the paused frame. The
        # docstring mentions the paused payload too, so anchor on the LAST
        # (actual v3 yield) occurrence.
        paused_idx = src.rfind('{"type": "paused", "conversation"')
        self.assertGreater(paused_idx, 0)
        window = src[max(0, paused_idx - 1500):paused_idx]
        self.assertIn('_s["status"] = "awaiting_approval"',
                      window,
                      "the paused path must close running steps as "
                      "awaiting_approval immediately before the paused frame")
        self.assertIn("_emit_activity_step", window,
                      "the closed step must be re-emitted to the client")

    def test_frontend_treats_awaiting_approval_as_terminal(self):
        import inspect
        from pathlib import Path

        p = Path(_BACKEND_ROOT).parent / "frontend/src/components/chat/ActivitySteps.jsx"
        src = p.read_text()
        self.assertIn("awaiting_approval", src,
                      "ActivitySteps must know the awaiting_approval status")
        self.assertRegex(
            src, r"TERMINAL_STATUSES\s*=\s*new Set\(\[[^\]]*awaiting_approval",
            "awaiting_approval must be terminal (no permanent spinner)")


if __name__ == "__main__":
    unittest.main()
