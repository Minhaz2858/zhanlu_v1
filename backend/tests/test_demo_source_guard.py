"""Tests for the demo/test source guard (2026-08-29).

ROOT CAUSE FIXED
----------------
Observed: a user asked for a supply-chain snapshot in the C5_C9 project.
The agent had TWO bound sources — `kb-demo-001` ("Demo E2E PostgreSQL",
bound at the agent level) and `b1b9145d` (the real ERP, bound at the
project level). fetch_data_batch failed twice with a generic
"multiple bound data sources" error (which did not enumerate the
sources), the agent guessed the DEMO source, and built the deliverable
from demo data — user reported "fabricated fake data".

Fixes:
1. `exclude_demo_sources_when_real_present` — when BOTH a demo-marked
   source and a real database source are bound, the demo source is
   dropped from the agent's bound set (LLM never sees it).
2. `_build_data_source_prompt_section` — demo-only sources are labeled
   "DEMO/TEST source — do NOT present its data as real business data".
3. `fetch_data_batch` — the "multiple bound data sources" error now
   enumerates the available sources (name, database, id) so the LLM
   can pick correctly instead of guessing.

DB-agnostic: detection is metadata-only (name + database_name markers),
never hardcoded ids, never table/column names.
"""
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

import pytest

from app.services.data_source_runtime.data_source_runtime import (
    _build_data_source_prompt_section,
    _demo_source_markers,
    exclude_demo_sources_when_real_present,
    is_demo_source_name,
    split_demo_and_real_kb_ids,
)


# ── Unit: marker detection ────────────────────────────────────────────────

class TestIsDemoSourceName:
    def test_id_carries_marker(self):
        assert is_demo_source_name("kb-demo-001", None)

    def test_name_carries_marker(self):
        assert is_demo_source_name("Demo E2E PostgreSQL", "demo_e2e")

    def test_database_name_carries_marker(self):
        assert is_demo_source_name("Sales Warehouse", "sales_test")

    def test_real_source_not_marked(self):
        assert not is_demo_source_name("aipdp_data_warehouse_prod", "aipdp_data_warehouse_prod")

    def test_ecisco_like_name_not_marked(self):
        # A production petrochemical source must never be flagged.
        assert not is_demo_source_name("ecisco_sales_warehouse", "ecisco_prod")

    def test_empty_returns_false(self):
        assert not is_demo_source_name("", "")
        assert not is_demo_source_name(None, None)

    def test_case_insensitive(self):
        assert is_demo_source_name("DEMO_WH", "DEMO_WH")


# ── Unit: partition + exclusion ───────────────────────────────────────────

class TestExcludeDemoSources:
    def test_demo_plus_real_keeps_real_only(self):
        bound = ["kb-demo-001", "b1b9145d-5b6b-4c0e-ba82-919dde4620d7"]
        name_map = {
            "kb-demo-001": ("Demo E2E PostgreSQL", "demo_e2e"),
            "b1b9145d-5b6b-4c0e-ba82-919dde4620d7": ("aipdp_data_warehouse_prod", "aipdp_data_warehouse_prod"),
        }
        out = exclude_demo_sources_when_real_present(bound, name_map=name_map)
        assert out == ["b1b9145d-5b6b-4c0e-ba82-919dde4620d7"]

    def test_demo_only_unchanged(self):
        bound = ["kb-demo-001"]
        out = exclude_demo_sources_when_real_present(bound)
        assert out == ["kb-demo-001"]

    def test_real_only_unchanged(self):
        bound = ["kb-real-1", "kb-real-2"]
        out = exclude_demo_sources_when_real_present(bound)
        assert out == ["kb-real-1", "kb-real-2"]

    def test_name_based_demo_without_id_marker(self):
        # id has no marker but the KB NAME is demo — must still be dropped
        # when a real source is present.
        bound = ["11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"]
        name_map = {
            "11111111-1111-1111-1111-111111111111": ("Demo Warehouse", "wh_demo"),
            "22222222-2222-2222-2222-222222222222": ("Prod Warehouse", "prod_wh"),
        }
        out = exclude_demo_sources_when_real_present(bound, name_map=name_map)
        assert out == ["22222222-2222-2222-2222-222222222222"]

    def test_empty_list(self):
        assert exclude_demo_sources_when_real_present([]) == []

    def test_split_returns_both_groups(self):
        bound = ["kb-demo-001", "kb-real-1"]
        demo, real = split_demo_and_real_kb_ids(bound)
        assert demo == ["kb-demo-001"]
        assert real == ["kb-real-1"]

    def test_markers_setting_default(self):
        assert "demo" in _demo_source_markers()


# ── Prompt section: demo labeling ─────────────────────────────────────────

class TestPromptSectionDemoLabel:
    def test_demo_source_labeled(self):
        meta = [{
            "id": "kb-demo-001",
            "name": "Demo E2E PostgreSQL",
            "source_kind": "database",
            "db_type": "postgresql",
            "database_name": "demo_e2e",
        }]
        section = _build_data_source_prompt_section(meta)
        assert "DEMO/TEST source" in section
        assert "do NOT present its data as real" in section

    def test_real_source_not_labeled(self):
        meta = [{
            "id": "b1b9145d-5b6b-4c0e-ba82-919dde4620d7",
            "name": "aipdp_data_warehouse_prod",
            "source_kind": "database",
            "db_type": "mysql",
            "database_name": "aipdp_data_warehouse_prod",
        }]
        section = _build_data_source_prompt_section(meta)
        assert "DEMO/TEST source" not in section

    def test_mixed_sources_only_demo_labeled(self):
        meta = [
            {
                "id": "kb-demo-001",
                "name": "Demo E2E PostgreSQL",
                "source_kind": "database",
                "db_type": "postgresql",
                "database_name": "demo_e2e",
            },
            {
                "id": "b1b9145d-5b6b-4c0e-ba82-919dde4620d7",
                "name": "aipdp_data_warehouse_prod",
                "source_kind": "database",
                "db_type": "mysql",
                "database_name": "aipdp_data_warehouse_prod",
            },
        ]
        section = _build_data_source_prompt_section(meta)
        assert "DEMO/TEST source" in section
        # Both source names present (label is per-source, not a filter here).
        assert "Demo E2E PostgreSQL" in section
        assert "aipdp_data_warehouse_prod" in section


# ── fetch_data_batch: ambiguity error enumerates sources ────────────────

class TestFetchDataBatchErrorEnumeratesSources:
    def test_multiple_bound_sources_enumerated(self):
        """When data_source_id is omitted and >1 source is bound, the error
        must name the available sources so the LLM can pick correctly."""
        from unittest.mock import MagicMock

        from app.services.tool_handlers.delegation_tools import _fetch_data_batch

        kb_rows = [
            MagicMock(id="kb-demo-001", name="Demo E2E PostgreSQL", database_name="demo_e2e"),
            MagicMock(id="kb-real-1", name="Prod Warehouse", database_name="prod_wh"),
        ]
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = kb_rows

        result = _run_fetch_batch(db, bound_kb_ids=["kb-demo-001", "kb-real-1"])
        assert result["success"] is False
        # Top-level error is the aggregate; per-query details carry the
        # enumerated source list.
        validation = result.get("validation") or []
        assert validation, "expected per-query validation details"
        err = validation[0].get("error", "")
        assert "data_source_id required" in err
        assert "Demo E2E PostgreSQL" in err
        assert "Prod Warehouse" in err
        assert "kb-real-1" in err

    def test_single_bound_source_auto_used(self):
        """One bound source → auto-filled, no error (legacy behavior)."""
        from unittest.mock import MagicMock

        from app.services.tool_handlers.delegation_tools import _fetch_data_batch

        kb_rows = [
            MagicMock(id="kb-demo-001", name="Demo E2E PostgreSQL", database_name="demo_e2e"),
        ]
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = kb_rows

        # Single bound source: fetch proceeds (execution path will fail on
        # the mock db, but the error must NOT be the ambiguity error).
        result = _run_fetch_batch(db, bound_kb_ids=["kb-demo-001"])
        assert result["success"] is False
        assert "multiple bound data sources" not in result.get("error", "")


def _run_fetch_batch(db, bound_kb_ids, queries=None):
    """Run _fetch_data_batch with a stubbed db + context."""
    import asyncio

    from app.services.tool_handlers.delegation_tools import _fetch_data_batch

    queries = queries or [
        {"sql": "SELECT 1 AS x", "label": "q1"},
    ]
    return asyncio.run(
        _fetch_data_batch(
            {"queries": queries},
            db=db,
            user_id="u1",
            context={"bound_kb_ids": bound_kb_ids},
        )
    )


# ── Layer-2 provenance assertion (artifact pipeline backstop) ──────────────

class _FakeKB:
    def __init__(self, id, name, database_name=None, is_deleted=False):
        self.id = id
        self.name = name
        self.database_name = database_name
        self.is_deleted = is_deleted


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows


class _FakeDB:
    """Minimal stand-in: query(AgentApp|KnowledgeBase) → filtered rows."""

    def __init__(self, agent=None, kbs=None):
        self._agent = agent
        self._kbs = kbs or []

    def query(self, model):
        from app.models.agent_app import AgentApp
        from app.models.knowledge_base import KnowledgeBase

        if model is AgentApp:
            return _FakeQuery([self._agent] if self._agent else [])
        if model is KnowledgeBase:
            return _FakeQuery(self._kbs)
        return _FakeQuery([])


class _FakeExecution:
    def __init__(self, result):
        self.result = result


class TestAssertArtifactSourceProvenance:
    def _run(self, monkeypatch, payload, *, bound_ids, name_map, execution=None, context=None):
        from app.services.data_source_runtime import data_source_runtime as dsr

        monkeypatch.setattr(dsr, "_resolve_kb_name_map", lambda db, ids: name_map)
        ctx = dict(context or {})
        ctx.setdefault("bound_kb_ids", bound_ids)
        return dsr.assert_artifact_source_provenance(
            None, payload=payload, execution=execution, context=ctx,
        )

    def test_reject_demo_cited_with_real_bound(self, monkeypatch):
        prov = self._run(
            monkeypatch,
            {"source": "Demo E2E PostgreSQL", "summary": "snapshot"},
            bound_ids=["kb-real-001"],
            name_map={"kb-real-001": ("aipdp_data_warehouse_prod", "aipdp_data_warehouse_prod")},
        )
        assert prov["ok"] is False
        assert prov["reason"] == "demo_source_citation"
        assert prov["real_present"] is True
        assert prov["demo_cited"][0]["name"] == "Demo E2E PostgreSQL"

    def test_reject_source_kb_id_resolves_to_demo_named_kb(self, monkeypatch):
        """Innocent-looking id whose KB NAME carries a demo marker → reject."""
        prov = self._run(
            monkeypatch,
            {"source": "Warehouse", "source_kb_id": "kb-0001"},
            bound_ids=["kb-0001", "kb-real-001"],
            name_map={
                "kb-0001": ("E2E Sample DB", "sample_orders"),
                "kb-real-001": ("aipdp_data_warehouse_prod", "aipdp_data_warehouse_prod"),
            },
        )
        assert prov["ok"] is False
        assert prov["demo_cited"][0]["id"] == "kb-0001"
        assert prov["demo_cited"][0]["name"] == "E2E Sample DB"

    def test_reject_execution_result_source(self, monkeypatch):
        execution = _FakeExecution({
            "source": {"id": "kb-demo-001", "name": "Demo E2E PostgreSQL", "db_type": "postgresql"},
        })
        prov = self._run(
            monkeypatch,
            {"summary": "x"},
            bound_ids=["kb-real-001"],
            name_map={"kb-real-001": ("aipdp_data_warehouse_prod", "aipdp_data_warehouse_prod")},
            execution=execution,
        )
        assert prov["ok"] is False
        assert prov["demo_cited"][0]["id"] == "kb-demo-001"

    def test_allow_demo_only_workspace(self, monkeypatch):
        """Demo cited but NO real source bound → allow (layer-1 parity)."""
        prov = self._run(
            monkeypatch,
            {"source": "Demo E2E PostgreSQL"},
            bound_ids=["kb-demo-001"],
            name_map={"kb-demo-001": ("Demo E2E PostgreSQL", "demo_e2e")},
        )
        assert prov["ok"] is True
        assert prov["real_present"] is False

    def test_allow_real_citation_with_mixed_bindings(self, monkeypatch):
        prov = self._run(
            monkeypatch,
            {"source": "aipdp_data_warehouse_prod"},
            bound_ids=["kb-demo-001", "kb-real-001"],
            name_map={
                "kb-demo-001": ("Demo E2E PostgreSQL", "demo_e2e"),
                "kb-real-001": ("aipdp_data_warehouse_prod", "aipdp_data_warehouse_prod"),
            },
        )
        assert prov["ok"] is True
        assert prov["real_present"] is True
        assert prov["demo_cited"] == []

    def test_allow_no_citation_tokens(self, monkeypatch):
        prov = self._run(
            monkeypatch,
            {"title": "Notes", "summary": "no source fields"},
            bound_ids=["kb-real-001"],
            name_map={"kb-real-001": ("aipdp_data_warehouse_prod", "aipdp_data_warehouse_prod")},
        )
        assert prov["ok"] is True

    def test_allow_guard_off(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "ARTIFACT_PROVENANCE_GUARD", "off")
        prov = self._run(
            monkeypatch,
            {"source": "Demo E2E PostgreSQL"},
            bound_ids=["kb-real-001"],
            name_map={"kb-real-001": ("aipdp_data_warehouse_prod", "aipdp_data_warehouse_prod")},
        )
        assert prov["ok"] is True
        assert prov["reason"] == "guard_off"

    def test_warn_mode_returns_ok_false_with_mode_warn(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "ARTIFACT_PROVENANCE_GUARD", "warn")
        prov = self._run(
            monkeypatch,
            {"source": "Demo E2E PostgreSQL"},
            bound_ids=["kb-real-001"],
            name_map={"kb-real-001": ("aipdp_data_warehouse_prod", "aipdp_data_warehouse_prod")},
        )
        assert prov["ok"] is False
        assert prov["mode"] == "warn"

    def test_sources_list_dict_items(self, monkeypatch):
        payload = {
            "sources": [
                {"name": "Sales Test DB", "id": "kb-t-9", "database_name": "sales_test"},
                {"name": "aipdp_data_warehouse_prod", "id": "kb-real-001"},
            ]
        }
        prov = self._run(
            monkeypatch,
            payload,
            bound_ids=["kb-real-001"],
            name_map={"kb-real-001": ("aipdp_data_warehouse_prod", "aipdp_data_warehouse_prod")},
        )
        assert prov["ok"] is False
        assert prov["demo_cited"][0]["name"] == "Sales Test DB"

    def test_report_card_nested_source(self, monkeypatch):
        payload = {"report_card_payload": {"source": "Demo E2E PostgreSQL"}}
        prov = self._run(
            monkeypatch,
            payload,
            bound_ids=["kb-real-001"],
            name_map={"kb-real-001": ("aipdp_data_warehouse_prod", "aipdp_data_warehouse_prod")},
        )
        assert prov["ok"] is False

    def test_recompute_bound_ids_from_agent_when_context_missing(self, monkeypatch):
        """Orchestrator paths pass only agent_app_id — recompute + extend."""
        from app.services.data_source_runtime import data_source_runtime as dsr

        agent = _FakeKB("agent-1", "general_assistant")  # reuse: id/name only
        agent.knowledge_bases = ["kb-demo-001"]
        db = _FakeDB(agent=agent)
        monkeypatch.setattr(dsr, "_resolve_kb_name_map", lambda d, ids: {
            "kb-demo-001": ("Demo E2E PostgreSQL", "demo_e2e"),
            "kb-real-001": ("aipdp_data_warehouse_prod", "aipdp_data_warehouse_prod"),
        })
        monkeypatch.setattr(dsr, "_extend_with_project_kbs", lambda d, a, ids, project_id=None: ids + ["kb-real-001"])
        prov = dsr.assert_artifact_source_provenance(
            db,
            payload={"source": "Demo E2E PostgreSQL"},
            context={"agent_app_id": "agent-1"},
        )
        assert prov["ok"] is False
        assert "kb-real-001" in prov["bound_ids"]

    def test_artifact_tool_guard_rejects_demo_citation(self, monkeypatch):
        """Integration: _create_artifact_tool raises ToolExecutionError when
        the payload cites a demo source and a real source is bound."""
        from unittest.mock import MagicMock

        import asyncio

        from app.services.tool_handlers import artifact_tool as at
        from app.services.tool_handlers.artifact_tool import ToolExecutionError

        async def _run():
            return await at._create_artifact_tool(
                {
                    "type": "docx",
                    "title": "Supply chain snapshot",
                    "payload": {"source": "Demo E2E PostgreSQL", "summary": "x"},
                },
                db=MagicMock(),
                user_id="u1",
                context={
                    # conversation_id left absent so the sibling-html
                    # enrichment path is skipped (it would touch the mock db);
                    # the provenance guard fires before any persistence.
                    "bound_kb_ids": ["kb-real-001"],
                    "agent_app_id": "agent-1",
                },
            )

        # The provenance guard must raise BEFORE any persistence; the mock db
        # would raise on query, but the guard fires first with a clear reason.
        with pytest.raises(ToolExecutionError, match="provenance guard"):
            asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
