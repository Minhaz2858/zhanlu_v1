"""Tests for app.services.result_block_processor.

The post-processor turns hallucinated [[RESULT]] artifact ids into real
artifacts so the frontend never sees a 404 on /api/artifacts/{id}.

Tested invariants:
- find_result_blocks parses a well-formed block and skips malformed ones.
- fulfill_result_blocks invokes the artifact tool for renderable file
  blocks and rewrites the id in the assistant text.
- Non-file blocks (agent / automation) are left untouched.
- A failed artifact creation does not corrupt the text.
"""

import json
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

# Provide a stub for the lazy import inside result_block_processor so the
# tests don't need the real artifact tool chain.
_STUB_RESULT = {
    "success": True,
    "artifact_id": "real-artifact-uuid-1",
    "file_url": "/api/artifacts/real-artifact-uuid-1/download",
    "preview_url": "/api/artifacts/real-artifact-uuid-1/preview",
    "title": "Sales_Report.docx",
    "type": "docx",
}


def _install_artifact_stub(monkeypatch, *, success=True, return_value=None):
    """Patch the lazy import path the processor uses."""
    from app.services import generation_orchestrator

    async def _stub(args, db=None, context=None):
        if return_value is not None:
            return return_value
        if success:
            return dict(_STUB_RESULT)
        return {"success": False, "error": "stub failure"}

    monkeypatch.setattr(
        generation_orchestrator,
        "_create_artifact_tool",
        _stub,
    )


def test_find_result_blocks_parses_well_formed():
    from app.services.result_block_processor import find_result_blocks

    text = (
        "I'll create a report.\n"
        "[[RESULT]]\n"
        + json.dumps(
            {
                "type": "file",
                "id": "fake-id",
                "name": "Sales_Report.docx",
                "fields": {"file_type": "docx", "content": "hi"},
                "draft": False,
            }
        )
        + "\n[[END]]\n"
    )
    blocks = find_result_blocks(text)
    assert len(blocks) == 1
    assert blocks[0].type == "file"
    assert blocks[0].name == "Sales_Report.docx"
    assert blocks[0].is_renderable_file is True


def test_find_result_blocks_skips_malformed():
    from app.services.result_block_processor import find_result_blocks

    text = "[[RESULT]] not-json [[END]]\n[[RESULT]]{}\n[[END]]"
    blocks = find_result_blocks(text)
    # The empty JSON has no "type", so it's skipped.
    assert blocks == []


def test_strip_result_blocks_removes_marker():
    from app.services.result_block_processor import strip_result_blocks

    text = "before\n[[RESULT]]\n{}\n[[END]]\nafter"
    cleaned = strip_result_blocks(text)
    assert "[[RESULT]]" not in cleaned
    assert "before" in cleaned and "after" in cleaned


@pytest.mark.asyncio
async def test_fulfill_result_blocks_creates_artifact_and_rewrites(monkeypatch):
    from app.services import result_block_processor

    _install_artifact_stub(monkeypatch, success=True)

    text = (
        "I'll create a sales report.\n"
        "[[RESULT]]\n"
        + json.dumps(
            {
                "type": "file",
                "id": "hallucinated-uuid",
                "name": "Sales_Report.docx",
                "fields": {
                    "file_type": "docx",
                    "content": "# Report\nTotal: $1",
                },
                "draft": False,
            }
        )
        + "\n[[END]]\n"
    )

    db = MagicMock()
    context = {"conversation_id": "c1"}
    rewritten, created = await result_block_processor.fulfill_result_blocks(text, db, context)

    assert len(created) == 1
    # The hallucinated id must be replaced with the real one.
    assert "real-artifact-uuid-1" in rewritten
    assert "hallucinated-uuid" not in rewritten
    # The block structure is preserved (still has [[RESULT]] ... [[END]]).
    assert "[[RESULT]]" in rewritten
    assert "[[END]]" in rewritten


@pytest.mark.asyncio
async def test_fulfill_result_blocks_leaves_non_file_blocks_alone(monkeypatch):
    from app.services import result_block_processor

    _install_artifact_stub(monkeypatch, success=True)

    text = (
        "[[RESULT]]\n"
        + json.dumps(
            {
                "type": "agent",
                "id": "agent-1",
                "name": "Helper",
                "fields": {"purpose": "automation"},
                "draft": False,
            }
        )
        + "\n[[END]]"
    )
    db = MagicMock()
    rewritten, created = await result_block_processor.fulfill_result_blocks(text, db, {})
    # Agent blocks are NOT routed to create_artifact — id stays the same.
    assert created == []
    assert "agent-1" in rewritten
    assert "real-artifact-uuid-1" not in rewritten


@pytest.mark.asyncio
async def test_fulfill_result_blocks_handles_failure_gracefully(monkeypatch):
    from app.services import result_block_processor

    _install_artifact_stub(monkeypatch, success=False)

    text = (
        "[[RESULT]]\n"
        + json.dumps(
            {
                "type": "file",
                "id": "hallucinated-uuid",
                "name": "Sales_Report.docx",
                "fields": {"file_type": "docx", "content": "body"},
                "draft": False,
            }
        )
        + "\n[[END]]"
    )
    db = MagicMock()
    rewritten, created = await result_block_processor.fulfill_result_blocks(text, db, {})
    # Failure is logged; text is left untouched (user still sees the result).
    assert created == []
    # The hallucinated id is preserved; frontend will still 404, but the
    # user can see what was attempted and we logged the failure.
    assert "hallucinated-uuid" in rewritten
