"""P0 enforcement fixes (2026-10-30) — semantic-store consumption parity.

Closes the four audit defects (docs/audits/2026-10-30-semantic-store-consumption-audit.md):
- P0-1: resolved_policy threaded on the FSM node + delegation fastpath (D2).
- P0-2: delegation sub-loop parity — verified metrics in the sub-agent prompt,
  source-verification check before raw execute_query (D1).
- P0-3: turn_id / conversation_id threaded through run_canonical + the
  nl2sql router (D4 router leg; the sub-loop budget leg was already enforced).
- P0-4: description provenance (human-verified / source-comment / ai-inferred)
  visible to SQL generation in TOC + DDL (D3).
- P0-5: stats()["columns_described"] counts COLUMNS, not tables.

No test references real warehouse table names — synthetic schemas only.
"""

from __future__ import annotations

import os
import sys
import uuid

import pytest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models.knowledge_base import KnowledgeBase  # noqa: E402
from app.models.metric_definition import MetricDefinition  # noqa: E402
from app.services import access_policy_service  # noqa: E402

KB_P = "kb-p0-enforce"  # synthetic policy-probe datasource


def _seed_kb(db, kb_id=KB_P):
    kb = db.get(KnowledgeBase, kb_id)
    if kb:
        return kb
    kb = KnowledgeBase(
        id=kb_id, name="p0-enforce", db_type="mysql",
        source_kind="database", org_id="default-org", app_id="default-app",
    )
    db.add(kb)
    db.commit()
    return kb


def _seed_table(db, kb_id, table_name, *, table_role=None, grain=None,
                description_source=None, description_en=""):
    from app.models.knowledge_catalog import KBTableMeta

    tm = db.query(KBTableMeta).filter(
        KBTableMeta.kb_id == kb_id, KBTableMeta.table_name == table_name,
    ).first()
    if tm is None:
        tm = KBTableMeta(kb_id=kb_id, schema_name="", table_name=table_name)
        db.add(tm)
        db.flush()
    if table_role is not None:
        tm.table_role = table_role
    if grain is not None:
        tm.grain = grain
    if description_source is not None:
        tm.description_source = description_source
    if description_en:
        tm.description_en = description_en
    db.commit()
    return tm


def _seed_column(db, tm, name, ordinal=0, *, description_source=None,
                 description_en=""):
    from app.models.knowledge_catalog import KBColumnMeta

    existing = db.query(KBColumnMeta).filter(
        KBColumnMeta.table_meta_id == tm.id,
        KBColumnMeta.column_name == name,
    ).first()
    if existing is not None:
        # Re-apply provenance/description — earlier runs may have created the
        # row without them (shared dev DB across test invocations).
        if description_source is not None:
            existing.description_source = description_source
        if description_en:
            existing.description_en = description_en
        db.commit()
        return existing
    col = KBColumnMeta(
        table_meta_id=tm.id, column_name=name, ordinal=ordinal,
    )
    if description_source is not None:
        col.description_source = description_source
    if description_en:
        col.description_en = description_en
    db.add(col)
    db.commit()
    return col


def _seed_metric(db, kb_id, name, base_table, base_column):
    if db.query(MetricDefinition).filter(
        MetricDefinition.datasource_id == kb_id, MetricDefinition.name == name,
    ).first():
        return
    db.add(MetricDefinition(
        id=str(uuid.uuid4()), name=name, display_name=name.replace("_", " "),
        datasource_id=kb_id, base_table=base_table, base_column=base_column,
        aggregation="sum", is_enabled=True,
    ))
    db.commit()


# ───────────────────────── P0-1: resolved_policy threading ──────────────────

class TestP01ResolvedPolicyThreading:
    def test_shared_context_resolver_no_scoping(self):
        from app.services.access_policy_service import resolve_policy_from_context

        policy = resolve_policy_from_context(None, None, None)
        assert policy.has_policies is False

    def test_shared_context_resolver_delegates_with_resource_fields(self, monkeypatch):
        from app.services.access_policy_service import resolve_policy_from_context

        sentinel = access_policy_service.ResolvedPolicy(has_policies=True)
        captured = {}

        def fake_resolve(db, **kwargs):
            captured.update(kwargs)
            return sentinel

        monkeypatch.setattr(
            access_policy_service, "resolve_user_policies", fake_resolve
        )
        ctx = {
            "resource_type": "project", "resource_id": "proj-1",
            "bound_kb_ids": [KB_P],
        }
        policy = resolve_policy_from_context(None, "user-1", ctx)
        assert policy is sentinel
        assert captured["user_id"] == "user-1"
        assert captured["resource_type"] == "project"
        assert captured["resource_id"] == "proj-1"

    def test_fsm_nl2sql_node_passes_resolved_policy(self, monkeypatch):
        import app.services.synexia.capability_router as cr
        import app.services.db as db_pkg

        _seed_kb(SessionLocal())
        captured = {}

        class _StubSvc:
            def __init__(self, db):
                pass

            async def answer(self, kb_id, question, **kwargs):
                captured.update(kwargs)
                return {"success": True, "rows": [], "sql": "SELECT 1",
                        "answer": "ok"}

        monkeypatch.setattr(db_pkg, "NLAnswerService", _StubSvc)
        monkeypatch.setattr(
            cr, "_record_observation",
            lambda *a, **k: ("obs", {}),
        )

        class _Exec:
            id = "exec-1"
            user_message = "question"
            user_id = "user-7"

        class _Node:
            inputs = {"question": "question"}
            description = "data node"
            name = "nl2sql"

        ctx_extras = {
            "bound_kb_ids": [KB_P],
            "user_id": "user-7",
            "resource_type": "project",
            "resource_id": "proj-9",
        }
        # No real policies exist for this synthetic project — stub the
        # resolver to prove the node PASSES the resolved policy through.
        sentinel = access_policy_service.ResolvedPolicy(has_policies=True)
        monkeypatch.setattr(
            access_policy_service, "resolve_user_policies",
            lambda db, **kw: sentinel,
        )
        cr._execute_nl2sql_node(None, _Exec(), _Node(), ctx_extras, None)
        assert "resolved_policy" in captured, (
            "FSM data node must resolve and pass the user's access policy"
        )
        assert captured["resolved_policy"].has_policies is True

    async def test_delegation_fastpath_passes_resolved_policy(self, monkeypatch):
        import app.services.tool_handlers.delegation_tools as dt
        import app.services.db as db_pkg

        db = SessionLocal()
        _seed_kb(db)
        captured = {}

        class _StubSvc:
            def __init__(self, db):
                pass

            async def answer(self, kb_id, question, **kwargs):
                captured.update(kwargs)
                return {"success": True, "rows": [], "sql": "SELECT 1",
                        "answer": "ok", "source_id": KB_P}

        monkeypatch.setattr(db_pkg, "NLAnswerService", _StubSvc)
        sentinel = access_policy_service.ResolvedPolicy(has_policies=True)
        monkeypatch.setattr(
            access_policy_service, "resolve_user_policies",
            lambda db_, **kw: sentinel,
        )

        result = await dt._try_nl_fastpath(
            "show me totals", KB_P, [KB_P], db,
            user_id="user-3",
            context={
                "resource_type": "project", "resource_id": "proj-2",
                "bound_kb_ids": [KB_P],
            },
        )
        assert result is not None
        assert "resolved_policy" in captured, (
            "delegation fastpath must resolve and pass the user's access policy"
        )


# ───────────────────────── P0-2: sub-loop enforcement parity ────────────────

class TestP02SubLoopParity:
    async def test_execute_query_refuses_detail_question_on_reporting_view(self, monkeypatch):
        """P-SV parity: a detail-intent question hitting a reporting view via
        raw execute_query must be refused BEFORE execution, like the funnel."""
        import app.services.tool_handlers.db_tools as dbt

        db = SessionLocal()
        _seed_kb(db)
        _tm = _seed_table(db, KB_P, "monthly_rollup", table_role="reporting_view")
        _seed_column(db, _tm, "m1", 0)

        executed = {}

        class _StubQS:
            def __init__(self, db):
                pass

            def execute(self, *a, **k):
                executed["ran"] = True
                return {"success": True, "rows": [{"x": 1}]}

        monkeypatch.setattr(dbt, "QueryService", _StubQS)
        # Synthetic KB has no real lifecycle record — bypass the readiness
        # gate (orthogonal to what this test verifies).
        monkeypatch.setattr(dbt, "_refuse_if_not_ready", lambda db_, kb_: None)
        old_flag = settings.NL2SQL_SOURCE_VERIFICATION_ENABLED
        settings.NL2SQL_SOURCE_VERIFICATION_ENABLED = True
        try:
            result = await dbt._execute_query(
                {"data_source_id": KB_P,
                 "sql": "SELECT m1 FROM monthly_rollup"},
                db, None,
                context={"question": "list all detail records"},
            )
        finally:
            settings.NL2SQL_SOURCE_VERIFICATION_ENABLED = old_flag
        assert result.get("success") is False
        assert result.get("error_kind") == "source_verification_clarify"
        assert not executed.get("ran"), "SQL must NOT execute after a P-SV refuse"

    async def test_execute_query_without_question_skips_source_verification(
        self, monkeypatch,
    ):
        """No question in context (other callers) → behavior unchanged."""
        import app.services.tool_handlers.db_tools as dbt

        db = SessionLocal()
        _seed_kb(db)
        _tm = _seed_table(db, KB_P, "monthly_rollup", table_role="reporting_view")
        _seed_column(db, _tm, "m1", 0)

        class _StubQS:
            def __init__(self, db):
                pass

            def execute(self, *a, **k):
                return {"success": True, "rows": [{"x": 1}]}

        monkeypatch.setattr(dbt, "QueryService", _StubQS)
        monkeypatch.setattr(dbt, "_refuse_if_not_ready", lambda db_, kb_: None)
        # This test isolates the P-SV skip behavior — silence the global
        # schema validator (same pattern as test_query_contract).
        monkeypatch.setattr(
            settings, "SCHEMA_GRAPH_SQL_VALIDATOR_ENABLED", False,
        )
        result = await dbt._execute_query(
            {"data_source_id": KB_P,
             "sql": "SELECT m1 FROM monthly_rollup"},
            db, None, context={},
        )
        assert result.get("success") is True

    def test_sub_agent_prompt_includes_verified_metrics(self):
        from app.services.tool_handlers.delegation_tools import (
            _build_sub_agent_prompt,
        )

        db = SessionLocal()
        _seed_kb(db)
        # P0-6 integrity guard: bindings whose base table is not in the
        # indexed catalog are skipped as stale — seed the table it binds to.
        from app.models.knowledge_catalog import KBTableMeta

        if (
            db.query(KBTableMeta)
            .filter(KBTableMeta.kb_id == KB_P, KBTableMeta.table_name == "order_line")
            .first()
            is None
        ):
            db.add(KBTableMeta(
                id=str(uuid.uuid4()), kb_id=KB_P, schema_name="",
                table_name="order_line", table_type="TABLE",
            ))
            db.commit()
        _seed_metric(db, KB_P, "net_total", "order_line", "famt")

        prompt = _build_sub_agent_prompt(
            "show me totals", [KB_P], KB_P, db=db,
        )
        assert "net_total" in prompt
        assert "order_line" in prompt
        assert "famt" in prompt
        assert "<verified-metrics>" in prompt

    def test_sub_agent_prompt_without_db_has_no_semantic_block(self):
        from app.services.tool_handlers.delegation_tools import (
            _build_sub_agent_prompt,
        )

        prompt = _build_sub_agent_prompt("show me totals", [KB_P], KB_P)
        assert "<verified-metrics>" not in prompt

    def test_sub_context_carries_question(self):
        """The sub-loop must thread the question into the tool context so the
        execution-time source-verification check can judge intent."""
        import inspect

        import app.services.tool_handlers.delegation_tools as dt

        src = inspect.getsource(dt)
        assert src.count('sub_context["question"] = question') >= 1, (
            "sub-loop tool context must carry the question for P-SV intent"
        )


# ───────────────────────── P0-3: turn identity through the router ───────────

class TestP03TurnIdentityThroughRouter:
    def test_run_canonical_threads_turn_and_conversation(self, monkeypatch):
        from app.services.nl2sql import compat

        captured = {}

        class _StubSvc:
            def __init__(self, db):
                pass

            async def answer(self, kb_id, question, **kwargs):
                captured.update(kwargs)
                return {"success": True}

        import app.services.db.nl_answer_service as nlas
        monkeypatch.setattr(nlas, "NLAnswerService", _StubSvc)

        compat.run_canonical(
            None, KB_P, "q", turn_id="turn-1", conversation_id="conv-1",
        )
        assert captured.get("turn_id") == "turn-1"
        assert captured.get("conversation_id") == "conv-1"

    def test_ask_threads_turn_and_conversation(self, monkeypatch):
        from app.services import nl2sql

        db = SessionLocal()
        _seed_kb(db)
        captured = {}

        def fake_run_canonical(db_, kb_id, question, **kwargs):
            captured.update(kwargs)
            return {"success": True}

        monkeypatch.setattr(nl2sql.compat, "run_canonical", fake_run_canonical)
        nl2sql.ask(
            "q", kb_id=KB_P, db=db,
            turn_id="turn-2", conversation_id="conv-2",
        )
        assert captured.get("turn_id") == "turn-2"
        assert captured.get("conversation_id") == "conv-2"


# ───────────────────────── P0-4: description provenance ─────────────────────

class TestP04DescriptionProvenance:
    def test_toc_entries_carry_provenance(self):
        from app.services.knowledge_graph.schema_linker import build_full_toc

        db = SessionLocal()
        _seed_kb(db)
        _seed_table(db, KB_P, "t_human", description_source="human",
                    description_en="human written")
        _seed_table(db, KB_P, "t_llm", description_source="llm",
                    description_en="ai guessed")
        _seed_table(db, KB_P, "t_comment", description_source="source_comment",
                    description_en="from db comment")
        _seed_table(db, KB_P, "t_none", description_en="")

        toc = build_full_toc(db, [KB_P])
        by_name = {t["table_name"]: t for t in toc}
        assert by_name["t_human"]["description_provenance"] == "human-verified"
        assert by_name["t_llm"]["description_provenance"] == "ai-inferred"
        assert by_name["t_comment"]["description_provenance"] == "source-comment"
        assert by_name["t_none"]["description_provenance"] == ""

    def test_selection_surface_renders_provenance(self):
        from app.services.knowledge_graph.schema_linker import (
            build_full_toc,
            format_toc_for_selection,
        )

        db = SessionLocal()
        _seed_kb(db)
        _seed_table(db, KB_P, "t_llm2", description_source="llm",
                    description_en="ai guessed")

        toc = build_full_toc(db, [KB_P])
        rendered = format_toc_for_selection(toc, candidates=["t_llm2"])
        assert "ai-inferred" in rendered

    def test_selected_ddl_renders_provenance(self):
        from app.services.knowledge_graph.schema_linker import (
            get_selected_tables_ddl,
        )

        db = SessionLocal()
        _seed_kb(db)
        tm = _seed_table(db, KB_P, "ddl_probe", description_source="llm",
                         description_en="ai guessed table")
        _seed_column(db, tm, "amount", 0, description_source="human",
                     description_en="human verified amount")

        ddl = get_selected_tables_ddl(db, [KB_P], ["ddl_probe"])
        assert "ai-inferred" in ddl
        assert "human-verified" in ddl


# ───────────────────────── P0-5: stats correctness ──────────────────────────

class TestP05StatsCorrectness:
    def test_columns_described_counts_columns(self):
        from app.services.db.semantic_store import get_semantic_store

        db = SessionLocal()
        # Fresh KB id per run: other test classes seed described columns
        # under the shared KB, which would leak into this count.
        kb_id = f"kb-p0-stats-{uuid.uuid4().hex[:8]}"
        _seed_kb(db, kb_id)
        tm_a = _seed_table(db, kb_id, "stat_t1", description_source="human",
                           description_en="d1")
        tm_b = _seed_table(db, kb_id, "stat_t2", description_source="human",
                           description_en="d2")
        # 4 described columns, 6 undescribed.
        _seed_column(db, tm_a, "c1", 0, description_source="human",
                     description_en="x")
        _seed_column(db, tm_a, "c2", 1, description_source="llm",
                     description_en="y")
        _seed_column(db, tm_a, "c3", 2)
        _seed_column(db, tm_b, "c1", 0, description_source="source_comment",
                     description_en="z")
        _seed_column(db, tm_b, "c2", 1, description_source="human",
                     description_en="w")
        _seed_column(db, tm_b, "c3", 2)
        _seed_column(db, tm_b, "c4", 3)
        _seed_column(db, tm_b, "c5", 4)

        store = get_semantic_store(db, [kb_id], None, None)
        stats = store.stats()
        assert stats["columns_described"] == 4, (
            "columns_described must count COLUMN rows with descriptions, "
            "not described tables"
        )
        assert stats["tables_described"] == 2
