"""Integration test for the deck-edit tool pipeline.

Unlike ``test_deck_edit_tool.py`` (which mocks ``ArtifactService`` with a
``MagicMock`` and exercises a single edit at a time), this test drives three
*consecutive* edits through the real handler against a *stateful* in-memory
``ArtifactService`` that actually persists each edit's ``deck_plan`` and
auto-increments the version number — exactly what the production
``ArtifactService`` does.  This verifies the acceptance flow:

    generate deck (version 1)
      -> edit_slide   (version 2, new thumbnail)
      -> update_chart (version 3, new thumbnail, sees the edited title)
      -> add_slide    (version 4, new thumbnail, sees all prior edits)

The render and thumbnail backends are mocked (they need LibreOffice / the
full render pipeline, which are covered by ``test_pptx_rich_deck.py``), but the
``DeckPlan`` model, the mutation layer, the guard rails and the versioning
behaviour are all real.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

import app.services.tool_handlers.deck_edit_tool  # noqa: F401  (registers tools)
from app.services.synexia.contracts import DeckPlan
from app.services.tool_registry import registry


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


def _ctx(**overrides) -> dict:
    base = {
        "conversation_id": "conv-1",
        "agent_app_id": "agent-1",
        "agent_name": "my_agent",
        "conversation_metadata": {},
        "chat_session_id": "sess-1",
        "endpoint": "https://llm.example/v1",
        "org_id": "org-1",
        "app_id": "app-1",
    }
    base.update(overrides)
    return base


class StatefulArtifactService:
    """Minimal in-memory ArtifactService with real version auto-increment."""

    def __init__(self, artifact_id: str, initial_source_json: dict):
        self.artifact = SimpleNamespace(
            id=artifact_id,
            title="Q3 Sales Review",
            org_id="org-1",
            app_id="app-1",
            conversation_id="conv-1",
            created_by_agent_id="agent-1",
            status="preview_ready",
            current_version_id="v1",
        )
        self.current_version = SimpleNamespace(
            id="v1", version_number=1, source_json=initial_source_json, status="built"
        )
        self.versions = [self.current_version]
        self.blobs: list[tuple] = []

    # -- read ----------------------------------------------------------------

    def get_artifact(self, artifact_id: str):
        return self.artifact if artifact_id == str(self.artifact.id) else None

    def get_current_version(self, artifact_id: str):
        return self.current_version

    def get_versions(self, artifact_id: str):
        return sorted(self.versions, key=lambda v: v.version_number, reverse=True)

    # -- write ---------------------------------------------------------------

    def create_version(self, artifact_id, changelog=None, source_json=None, produced_by_skill=None):
        next_num = self.current_version.version_number + 1
        v = SimpleNamespace(
            id=f"v{next_num}",
            version_number=next_num,
            source_json=source_json,
            status="building",
            changelog=changelog,
        )
        self.versions.append(v)
        self.current_version = v
        self.artifact.current_version_id = v.id
        return v

    def store_blob(self, version_id, blob_type, file_name, mime_type, data):
        self.blobs.append((version_id, blob_type, file_name, data))

    def mark_version_built(self, version_id, validation_report=None):
        for v in self.versions:
            if v.id == version_id:
                v.status = "preview_ready"


async def _call(tool, args, service):
    return await registry.get_handler(tool)(args, None, "user-1", context=_ctx())


@pytest.mark.asyncio
async def test_generate_then_three_consecutive_edits():
    # "generate" a deck: a real DeckPlan persisted as version 1.
    initial_plan = DeckPlan.model_validate(DECK_PLAN_DICT)
    service = StatefulArtifactService("artifact-1", {"deck_plan": initial_plan.model_dump(mode="json")})

    with (
        patch("app.services.tool_handlers.deck_edit_tool.ArtifactService", return_value=service),
        patch(
            "app.services.tool_handlers.deck_edit_tool.render_pptx_from_plan_sync",
            return_value=(b"pptx-bytes", {"repairs": [], "audit": []}),
        ) as mock_render,
        patch(
            "app.services.tool_handlers.deck_edit_tool.render_page_thumbnails",
            return_value=[b"thumb-1", b"thumb-2", b"thumb-3", b"thumb-4", b"thumb-5"],
        ) as mock_thumbnails,
    ):
        # Edit 1: rename slide 1 -> version 2
        r1 = await _call(
            "edit_slide",
            {"artifact_id": "artifact-1", "slide_index": 1, "changes": {"title": "Edited Revenue"}},
            service,
        )
        # Edit 2: change the chart on slide 1 -> version 3 (must see the edited title)
        r2 = await _call(
            "update_chart",
            {"artifact_id": "artifact-1", "slide_index": 1, "chart": {"chart_type": "line"}},
            service,
        )
        # Edit 3: add a slide before the closing -> version 4
        r3 = await _call(
            "add_slide",
            {"artifact_id": "artifact-1", "slide": {"layout": "data_table", "title": "Region Table"}},
            service,
        )

    # Each edit succeeded and produced a strictly increasing version number.
    assert r1["success"] is True and r1["version_number"] == 2
    assert r2["success"] is True and r2["version_number"] == 3
    assert r3["success"] is True and r3["version_number"] == 4

    # Version history now holds 4 versions (1 original + 3 edits).
    history = service.get_versions("artifact-1")
    assert [v.version_number for v in history] == [4, 3, 2, 1]

    # The final persisted deck_plan reflects all three edits cumulatively.
    final_plan = DeckPlan.model_validate(service.current_version.source_json["deck_plan"])
    assert final_plan.slides[1].title == "Edited Revenue"
    assert final_plan.slides[1].chart_spec.chart_type == "line"
    assert len(final_plan.slides) == 5
    assert final_plan.slides[0].layout == "cover"
    assert final_plan.slides[-1].layout == "closing"
    assert final_plan.slides[-2].layout == "data_table"

    # Each edit re-rendered the *latest* plan (3 render passes total).
    assert mock_render.call_count == 3
    # Edit 2's render received the plan with the edited title from edit 1.
    plan_at_edit2 = mock_render.call_args_list[1].args[0]
    assert plan_at_edit2.slides[1].title == "Edited Revenue"

    # Each edit produced fresh thumbnails, stored under its own version.
    assert mock_thumbnails.call_count == 3
    thumb_versions = {blob[0] for blob in service.blobs if blob[1] == "thumbnail"}
    assert thumb_versions == {"v2", "v3", "v4"}

    # Each edit stored an "original" blob under its own version.
    original_versions = {blob[0] for blob in service.blobs if blob[1] == "original"}
    assert original_versions == {"v2", "v3", "v4"}
