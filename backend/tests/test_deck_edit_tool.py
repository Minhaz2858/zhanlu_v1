"""Tests for PHASE 2 — the ``deck_edit_tool`` handler layer.

Covers the six registered edit tools and the shared guard / render /
version / thumbnail flow:

* six tools registered in the tool registry (category ``artifact_edit``,
  ``enabled_by_default=False``)
* missing-artifact rejection
* cross-tenant rejection
* failed-artifact rejection
* missing ``deck_plan`` rejection
* happy path: mutation -> render -> new version -> original blob ->
  thumbnails -> canonical result dict
* cover/closing removal and reorder guards surface as failure results
  (no dirty version is created)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

# Importing the handler module registers the six deck-edit tools in the
# registry (needed by test_tool_registered when this file runs alone).
import app.services.tool_handlers.deck_edit_tool  # noqa: F401
from app.services.tool_registry import registry

# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------

DECK_PLAN_DICT = {
    "title": "Q3 Sales Review",
    "deck_type": "data_report",
    "theme_recommendation": "zhanlu-blue",
    "headline_style": "topic",
    "summary": "Quarterly sales summary",
    "methodology": "Data from ERP",
    "slides": [
        {"layout": "cover", "title": "Q3 Sales Review", "subtitle": "Board deck"},
        {
            "layout": "chart_with_bullets",
            "title": "Revenue Trend",
            "bullets": ["Up 8%"],
            "chart_spec": {"chart_type": "bar", "x_key": "month", "y_keys": ["revenue"], "title": "Revenue"},
            "chart_rows": [{"month": "Jan", "revenue": 100}, {"month": "Feb", "revenue": 120}],
        },
        {"layout": "insights_bullets", "title": "Key Insights", "bullets": ["A", "B"]},
        {"layout": "closing", "title": "Thank You", "subtitle": "Q&A"},
    ],
}


def _artifact(**overrides):
    base = {
        "id": 1,
        "org_id": "org-1",
        "app_id": "app-1",
        "conversation_id": "conv-1",
        "created_by_agent_id": "agent-1",
        "status": "preview_ready",
        "current_version_id": 1,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _version(source_json=None, version_number=1):
    return SimpleNamespace(
        id=10,
        version_number=version_number,
        source_json=source_json or {"deck_plan": DECK_PLAN_DICT},
        status="built",
    )


def _new_version(version_number=2):
    return SimpleNamespace(id=20, version_number=version_number)


def _ctx(**overrides):
    base = {
        "conversation_id": "conv-1",
        "agent_app_id": "agent-1",
        "agent_name": "my_agent",
        "conversation_metadata": {},
        "chat_session_id": "sess-1",
        "endpoint": "https://llm.example/v1",
        # Tenant reference the agent loop injects via data_ctx_extras; the
        # deck-edit guard compares artifact.org_id/app_id against these.
        "org_id": "org-1",
        "app_id": "app-1",
    }
    base.update(overrides)
    return base


@pytest.fixture
def mock_service():
    svc = MagicMock()
    svc.get_artifact.return_value = _artifact()
    svc.get_current_version.return_value = _version()
    svc.create_version.return_value = _new_version()
    return svc


@pytest.fixture
def mock_render():
    with patch(
        "app.services.tool_handlers.deck_edit_tool.render_pptx_from_plan_sync",
        return_value=(b"pptx-bytes", {"repairs": [], "audit": []}),
    ) as m:
        yield m


@pytest.fixture
def mock_thumbnails():
    with patch(
        "app.services.tool_handlers.deck_edit_tool.render_page_thumbnails",
        return_value=[b"thumb-1", b"thumb-2", b"thumb-3", b"thumb-4"],
    ) as m:
        yield m


def _call(tool, args, db, ctx):
    return registry.get_handler(tool)(args, db, "user-1", context=ctx)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool",
    ["edit_slide", "add_slide", "restyle_deck", "update_chart", "remove_slide", "reorder_slide"],
)
def test_tool_registered(tool):
    entry = registry.get_entry(tool)
    assert entry is not None, f"{tool} must be registered"
    assert entry.category == "artifact_edit"
    assert entry.enabled_by_default is False


# ---------------------------------------------------------------------------
# Guard rails
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_artifact_returns_failure(mock_service, mock_render, mock_thumbnails):
    mock_service.get_artifact.return_value = None
    db = MagicMock()
    with patch("app.services.tool_handlers.deck_edit_tool.ArtifactService", return_value=mock_service):
        result = await _call(
            "edit_slide", {"artifact_id": 999, "slide_index": 1, "changes": {"title": "X"}}, db, _ctx()
        )
    assert result["success"] is False
    assert "not found" in result["message"].lower() or "not found" in str(result).lower()
    mock_service.create_version.assert_not_called()


@pytest.mark.asyncio
async def test_cross_tenant_rejected(mock_service, mock_render, mock_thumbnails):
    mock_service.get_artifact.return_value = _artifact(org_id="org-OTHER", app_id="app-OTHER")
    db = MagicMock()
    with patch("app.services.tool_handlers.deck_edit_tool.ArtifactService", return_value=mock_service):
        result = await _call(
            "edit_slide", {"artifact_id": 1, "slide_index": 1, "changes": {"title": "X"}}, db, _ctx()
        )
    assert result["success"] is False
    assert "permission" in result["message"].lower() or "tenant" in result["message"].lower()
    mock_service.create_version.assert_not_called()


@pytest.mark.asyncio
async def test_failed_artifact_rejected(mock_service, mock_render, mock_thumbnails):
    mock_service.get_artifact.return_value = _artifact(status="failed")
    db = MagicMock()
    with patch("app.services.tool_handlers.deck_edit_tool.ArtifactService", return_value=mock_service):
        result = await _call(
            "edit_slide", {"artifact_id": 1, "slide_index": 1, "changes": {"title": "X"}}, db, _ctx()
        )
    assert result["success"] is False
    assert "failed" in result["message"].lower()
    mock_service.create_version.assert_not_called()


@pytest.mark.asyncio
async def test_missing_deck_plan_rejected(mock_service, mock_render, mock_thumbnails):
    mock_service.get_current_version.return_value = _version(source_json={"other": "data"})
    db = MagicMock()
    with patch("app.services.tool_handlers.deck_edit_tool.ArtifactService", return_value=mock_service):
        result = await _call(
            "edit_slide", {"artifact_id": 1, "slide_index": 1, "changes": {"title": "X"}}, db, _ctx()
        )
    assert result["success"] is False
    assert "no stored deck plan" in result["message"].lower() or "deck plan" in result["message"].lower()
    mock_service.create_version.assert_not_called()


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_slide_success(mock_service, mock_render, mock_thumbnails):
    db = MagicMock()
    with patch("app.services.tool_handlers.deck_edit_tool.ArtifactService", return_value=mock_service):
        result = await _call(
            "edit_slide",
            {"artifact_id": 1, "slide_index": 1, "changes": {"title": "Edited Title", "bullets": ["X", "Y"]}},
            db,
            _ctx(),
        )
    assert result["success"] is True
    assert result["version_number"] == 2
    assert result["artifact_id"] == 1
    # render called with a DeckPlan (mutated) + rows
    assert mock_render.call_count == 1
    _plan_arg = mock_render.call_args[0][0]
    assert _plan_arg.slides[1].title == "Edited Title"
    # version persisted with mutated plan
    _created_sj = mock_service.create_version.call_args.kwargs["source_json"]
    assert _created_sj["deck_plan"]["slides"][1]["title"] == "Edited Title"
    assert mock_service.create_version.call_args.kwargs["produced_by_skill"] == "deck_edit"
    # original blob + thumbnails stored
    mock_service.store_blob.assert_any_call(
        mock_service.create_version.return_value.id, "original",
        ANY, "application/vnd.openxmlformats-officedocument.presentationml.presentation", b"pptx-bytes",
    )
    assert mock_service.mark_version_built.called
    assert mock_thumbnails.called
    thumb_calls = [c for c in mock_service.store_blob.call_args_list if c.args[1] == "thumbnail"]
    assert len(thumb_calls) == 4
    # result carries the canonical keys
    for key in ("success", "artifact_id", "version_id", "version_number", "file_url",
                "preview_url", "download_url", "file_name", "mime_type", "file_size", "message"):
        assert key in result


@pytest.mark.asyncio
async def test_add_slide_success(mock_service, mock_render, mock_thumbnails):
    db = MagicMock()
    with patch("app.services.tool_handlers.deck_edit_tool.ArtifactService", return_value=mock_service):
        result = await _call(
            "add_slide",
            {"artifact_id": 1, "slide": {"layout": "data_table", "title": "Region Table", "bullets": ["r1"]}},
            db,
            _ctx(),
        )
    assert result["success"] is True
    _plan = mock_render.call_args[0][0]
    assert len(_plan.slides) == 5
    assert _plan.slides[-1].layout == "closing"  # added before closing
    assert _plan.slides[-2].layout == "data_table"


@pytest.mark.asyncio
async def test_restyle_deck_success(mock_service, mock_render, mock_thumbnails):
    db = MagicMock()
    with patch("app.services.tool_handlers.deck_edit_tool.ArtifactService", return_value=mock_service):
        result = await _call(
            "restyle_deck",
            {"artifact_id": 1, "theme": "neon_cyber", "headline_style": "assertion"},
            db,
            _ctx(),
        )
    assert result["success"] is True
    _plan = mock_render.call_args[0][0]
    assert _plan.theme_recommendation == "neon_cyber"
    assert _plan.headline_style == "assertion"


@pytest.mark.asyncio
async def test_update_chart_success(mock_service, mock_render, mock_thumbnails):
    db = MagicMock()
    with patch("app.services.tool_handlers.deck_edit_tool.ArtifactService", return_value=mock_service):
        result = await _call(
            "update_chart",
            {"artifact_id": 1, "slide_index": 1, "chart": {"chart_type": "line", "y_keys": ["revenue", "cost"]}},
            db,
            _ctx(),
        )
    assert result["success"] is True
    _plan = mock_render.call_args[0][0]
    assert _plan.slides[1].chart_spec.chart_type == "line"
    assert _plan.slides[1].chart_spec.y_keys == ["revenue", "cost"]


@pytest.mark.asyncio
async def test_remove_slide_success(mock_service, mock_render, mock_thumbnails):
    db = MagicMock()
    with patch("app.services.tool_handlers.deck_edit_tool.ArtifactService", return_value=mock_service):
        result = await _call(
            "remove_slide", {"artifact_id": 1, "slide_index": 1}, db, _ctx()
        )
    assert result["success"] is True
    _plan = mock_render.call_args[0][0]
    assert len(_plan.slides) == 3
    assert [s.title for s in _plan.slides] == ["Q3 Sales Review", "Key Insights", "Thank You"]


@pytest.mark.asyncio
async def test_reorder_slide_success(mock_service, mock_render, mock_thumbnails):
    db = MagicMock()
    with patch("app.services.tool_handlers.deck_edit_tool.ArtifactService", return_value=mock_service):
        result = await _call(
            "reorder_slide", {"artifact_id": 1, "from_index": 1, "to_index": 2}, db, _ctx()
        )
    assert result["success"] is True
    _plan = mock_render.call_args[0][0]
    assert [s.title for s in _plan.slides] == ["Q3 Sales Review", "Key Insights", "Revenue Trend", "Thank You"]


# ---------------------------------------------------------------------------
# Guard violations inside mutation layer -> failure, no dirty version
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_cover_rejected_no_dirty_version(mock_service, mock_render, mock_thumbnails):
    db = MagicMock()
    with patch("app.services.tool_handlers.deck_edit_tool.ArtifactService", return_value=mock_service):
        result = await _call(
            "remove_slide", {"artifact_id": 1, "slide_index": 0}, db, _ctx()
        )
    assert result["success"] is False
    assert "cover" in result["message"].lower()
    mock_service.create_version.assert_not_called()
    mock_render.assert_not_called()


@pytest.mark.asyncio
async def test_remove_closing_rejected_no_dirty_version(mock_service, mock_render, mock_thumbnails):
    db = MagicMock()
    with patch("app.services.tool_handlers.deck_edit_tool.ArtifactService", return_value=mock_service):
        result = await _call(
            "remove_slide", {"artifact_id": 1, "slide_index": 3}, db, _ctx()
        )
    assert result["success"] is False
    assert "closing" in result["message"].lower()
    mock_service.create_version.assert_not_called()
    mock_render.assert_not_called()


@pytest.mark.asyncio
async def test_reorder_cover_rejected(mock_service, mock_render, mock_thumbnails):
    db = MagicMock()
    with patch("app.services.tool_handlers.deck_edit_tool.ArtifactService", return_value=mock_service):
        result = await _call(
            "reorder_slide", {"artifact_id": 1, "from_index": 1, "to_index": 0}, db, _ctx()
        )
    assert result["success"] is False
    mock_service.create_version.assert_not_called()
    mock_render.assert_not_called()
