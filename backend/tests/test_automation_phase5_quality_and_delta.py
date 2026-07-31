"""Phase 5 (Manus parity) tests.

Covers:
  * Tier A #2 — the deliverable quality gate in ``_render_and_save_files``
    holds back low-confidence output (mirrors the chat FINALIZE gate) and
    ships when confidence is sufficient or when no FSM confidence exists.
  * Tier B #4 — ``_extract_structured_summary`` / ``_previous_run_context``
    produce a structured cross-run delta (headings + metrics) instead of a
    raw head+tail dump.
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services import automation_executor as ax


# ---------------------------------------------------------------------------
# Tier A #2 — quality gate on the deliverable
# ---------------------------------------------------------------------------

def _make_task_and_execution():
    task = MagicMock()
    task.id = "t1"; task.name = "Weekly Report"
    task.output_format = "html"
    task.org_id = "o"; task.app_id = "a"; task.created_by_id = "u"

    execution = MagicMock()
    execution.id = "exec1"
    return task, execution


def _patch_generate_document():
    """Patch generate_document so no real file is written."""
    fake_path = MagicMock()
    fake_path.exists.return_value = True
    fake_path.stat.return_value = MagicMock(st_size=123)
    return patch(
        "app.services.document_generator.generate_document",
        return_value=(fake_path, "/files/x.html", "text/html"),
    )


def test_render_holds_back_deliverable_when_confidence_below_threshold():
    """When the FSM confidence is below the quality-gate threshold, no file
    is generated and the gate decision is returned with passed=False."""
    task, execution = _make_task_and_execution()
    db = MagicMock()

    with _patch_generate_document() as gen:
        files, gate = ax._render_and_save_files(
            db, task, execution, "## Report\ncontent", "prompt",
            fsm_meta={"confidence": 0.1, "state": "done"},
        )
    # Held back: no files, gate fired.
    assert files == []
    assert gate is not None
    assert gate["passed"] is False
    # generate_document must NOT have been called (the file was held back).
    assert gen.call_count == 0


def test_render_ships_deliverable_when_confidence_above_threshold():
    """When confidence meets the threshold, the file is generated and the
    gate passes."""
    task, execution = _make_task_and_execution()
    db = MagicMock()

    with _patch_generate_document() as gen:
        files, gate = ax._render_and_save_files(
            db, task, execution, "## Report\ncontent", "prompt",
            fsm_meta={"confidence": 0.9, "state": "done"},
        )
    assert len(files) == 1
    assert gate is not None
    assert gate["passed"] is True
    assert gen.call_count == 1


def test_render_ships_without_gate_when_no_fsm_confidence():
    """ReAct-loop runs (fsm_meta is None) ship as before — no gate applied."""
    task, execution = _make_task_and_execution()
    db = MagicMock()

    with _patch_generate_document() as gen:
        files, gate = ax._render_and_save_files(
            db, task, execution, "## Report\ncontent", "prompt",
            fsm_meta=None,
        )
    assert len(files) == 1
    assert gate is None  # no FSM → no gate
    assert gen.call_count == 1


# ---------------------------------------------------------------------------
# Tier B #4 — structured cross-run delta
# ---------------------------------------------------------------------------

def test_extract_structured_summary_pulls_headings_and_metrics():
    text = (
        "# Q3 Revenue Report\n\n"
        "## Overview\n"
        "Total revenue reached $4.2M, up 12% vs last quarter.\n\n"
        "## By Region\n"
        "- North America: $2.1M (50% share)\n"
        "- Europe: $1.4M\n\n"
        "Conversion rate improved to 3.4%.\n"
    )
    summary = ax._extract_structured_summary(text)
    assert summary["title"] == "Q3 Revenue Report"
    assert "Overview" in summary["headings"]
    assert "By Region" in summary["headings"]
    # Metric lines contain a currency/percentage/unit marker.
    assert any("$4.2M" in m for m in summary["metrics"])
    assert any("12%" in m for m in summary["metrics"])
    assert any("3.4%" in m for m in summary["metrics"])
    # Bullets are captured.
    assert any("North America" in b for b in summary["bullets"])


def test_extract_structured_summary_handles_empty_text():
    summary = ax._extract_structured_summary("")
    assert summary == {
        "title": "", "headings": [], "metrics": [], "bullets": [],
        "raw_head": "",
    }


def test_previous_run_context_returns_structured_summary():
    """_previous_run_context returns a 3-tuple whose third element is the
    structured summary dict (for persistence as cross_run_delta)."""
    prev = MagicMock()
    prev.id = "prevexec1234"
    prev.completed_at = None
    prev.output_text = (
        "## Last Week\nRevenue was $3.8M, up 8%.\n\n## Notes\n- Stable growth.\n"
    )

    db = MagicMock()
    # The lookup query chain.
    db.query.return_value.filter.return_value.order_by.return_value.first.return_value = prev

    task = MagicMock()
    task.id = "t1"

    block, prev_id, summary = ax._previous_run_context(db, task, "currentexec1")
    assert prev_id == "prevexec1234"
    assert summary is not None
    assert "Last Week" in summary["headings"]
    assert any("$3.8M" in m for m in summary["metrics"])
    # The injected block references the structured sections, not a raw dump.
    assert "Sections from the previous run" in block
    assert "Key figures from the previous run" in block


def test_previous_run_context_returns_empty_when_no_prior_run():
    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
    task = MagicMock()
    task.id = "t1"

    block, prev_id, summary = ax._previous_run_context(db, task, "currentexec1")
    assert block == ""
    assert prev_id is None
    assert summary is None
