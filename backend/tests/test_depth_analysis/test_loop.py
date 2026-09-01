"""Depth Analysis Loop — iteration cap, critic gate, evidence pack, soft-fail."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.config import settings
from app.database import Base
from app.services.depth_analysis.loop import (
    DepthAnalysisResult,
    run_depth_loop,
)


@pytest.fixture
def db(tmp_path):
    db_file = tmp_path / f"test_{uuid.uuid4().hex[:8]}.db"
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    original = getattr(settings, "DEPTH_ANALYSIS_LOOP_ENABLED", False)
    settings.DEPTH_ANALYSIS_LOOP_ENABLED = True
    try:
        yield s
    finally:
        settings.DEPTH_ANALYSIS_LOOP_ENABLED = original
        s.close()
        engine.dispose()


PID = "proj-1"
KB_ID = "kb-1"


class TestDepthLoopShape:
    def test_returns_depth_analysis_result(self, db):
        mock_llm = AsyncMock(return_value={
            "response": "The data shows a 15% decline in revenue.",
            "data": {"hypothesis": "Check revenue trend", "refinement": ""},
        })
        mock_linker = MagicMock(return_value={
            "slice_text": "Table: orders (columns: amount, date)",
            "tables": [{"table_name": "orders"}],
        })
        mock_qs = MagicMock()
        mock_qs.execute.return_value = {"rows": [{"amount": 100}], "row_count": 1}

        with patch("app.services.depth_analysis.loop.call_llm", mock_llm), \
             patch("app.services.depth_analysis.loop.link_schema", mock_linker), \
             patch("app.services.depth_analysis.loop.QueryService", return_value=mock_qs):
            result = run_depth_loop(
                "why did revenue decline?", PID, db, kb_id=KB_ID,
            )

        assert isinstance(result, DepthAnalysisResult)
        assert result.answer
        assert result.iterations >= 1
        assert 0.0 <= result.confidence <= 1.0
        assert isinstance(result.degraded, bool)
        assert "queries" in result.evidence_pack


class TestIterationCap:
    def test_max_3_iterations_enforced(self, db):
        # LLM always says "need more data" → should cap at 3
        mock_llm = AsyncMock(return_value={
            "response": "Need more data.",
            "data": {"hypothesis": "check", "refinement": "check again"},
        })
        mock_linker = MagicMock(return_value={
            "slice_text": "t", "tables": [{"table_name": "t"}],
        })
        mock_qs = MagicMock()
        mock_qs.execute.return_value = {"rows": [], "row_count": 0}

        with patch("app.services.depth_analysis.loop.call_llm", mock_llm), \
             patch("app.services.depth_analysis.loop.link_schema", mock_linker), \
             patch("app.services.depth_analysis.loop.QueryService", return_value=mock_qs):
            result = run_depth_loop(
                "why?", PID, db, kb_id=KB_ID, max_iterations=3,
            )

        assert result.iterations <= 3


class TestCriticGate:
    def test_early_exit_when_evidence_sufficient(self, db):
        # First iteration returns rich data → should exit after 1 iteration
        mock_llm = AsyncMock(return_value={
            "response": "Revenue declined 15% due to fewer orders.",
            "data": {"hypothesis": "check revenue"},
        })
        mock_linker = MagicMock(return_value={
            "slice_text": "t", "tables": [{"table_name": "orders"}],
        })
        mock_qs = MagicMock()
        mock_qs.execute.return_value = {
            "rows": [{"month": "Jan", "revenue": 100}, {"month": "Feb", "revenue": 85}],
            "row_count": 2,
        }

        with patch("app.services.depth_analysis.loop.call_llm", mock_llm), \
             patch("app.services.depth_analysis.loop.link_schema", mock_linker), \
             patch("app.services.depth_analysis.loop.QueryService", return_value=mock_qs):
            result = run_depth_loop(
                "why did revenue decline?", PID, db, kb_id=KB_ID,
            )

        assert result.iterations == 1
        assert result.confidence > 0.5


class TestSoftFail:
    def test_llm_failure_returns_degraded(self, db):
        mock_llm = AsyncMock(side_effect=RuntimeError("LLM down"))
        with patch("app.services.depth_analysis.loop.call_llm", mock_llm):
            result = run_depth_loop("why?", PID, db, kb_id=KB_ID)
        assert result.degraded is True
        assert result.confidence == 0.0
        assert result.answer  # still has a fallback message

    def test_query_failure_returns_degraded(self, db):
        mock_llm = AsyncMock(return_value={
            "response": "ans", "data": {"hypothesis": "h"},
        })
        mock_linker = MagicMock(return_value={
            "slice_text": "t", "tables": [{"table_name": "t"}],
        })
        mock_qs = MagicMock()
        mock_qs.execute.side_effect = RuntimeError("DB down")
        with patch("app.services.depth_analysis.loop.call_llm", mock_llm), \
             patch("app.services.depth_analysis.loop.link_schema", mock_linker), \
             patch("app.services.depth_analysis.loop.QueryService", return_value=mock_qs):
            result = run_depth_loop("why?", PID, db, kb_id=KB_ID)
        assert result.degraded is True


class TestEvidencePack:
    def test_evidence_pack_contains_queries_and_rows(self, db):
        mock_llm = AsyncMock(return_value={
            "response": "Revenue declined.", "data": {"hypothesis": "check"},
        })
        mock_linker = MagicMock(return_value={
            "slice_text": "t", "tables": [{"table_name": "orders"}],
        })
        mock_qs = MagicMock()
        mock_qs.execute.return_value = {
            "rows": [{"revenue": 100}], "row_count": 1,
        }

        with patch("app.services.depth_analysis.loop.call_llm", mock_llm), \
             patch("app.services.depth_analysis.loop.link_schema", mock_linker), \
             patch("app.services.depth_analysis.loop.QueryService", return_value=mock_qs):
            result = run_depth_loop("why?", PID, db, kb_id=KB_ID)

        ep = result.evidence_pack
        assert "queries" in ep
        assert len(ep["queries"]) >= 1
        assert "rows_examined" in ep
        assert ep["rows_examined"] >= 1
        assert "confidence" in ep
        assert "citations" in ep
