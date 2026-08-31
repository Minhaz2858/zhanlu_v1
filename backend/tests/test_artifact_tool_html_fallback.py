"""
Regression tests for create_artifact(type="html") payload handling.

Root cause observed in production (2026-08-31): the agent system prompt
never documented the HTML payload contract, so the LLM called
create_artifact(type="html") with a report-card payload (summary/kpis/
sections) instead of payload={"html_content": "..."}. The tool hard-failed
with "html_content is required for html type" on every "give me in html
file" request — the reliability wrapper retried the same broken shape twice
then gave up, and the user saw "could not be generated within this turn's
tool budget" / "create_artifact failed".

Fix: (1) the prompt now documents the html contract (agent_prompts.py
FILE-FORMAT INTENT block); (2) the tool degrades gracefully — when
html_content is absent but report-card fields exist, it renders a
professional self-contained HTML report from them (the same
_render_sidecar_html renderer the inline preview uses).
"""

import pytest
from unittest.mock import MagicMock, patch

from app.services.synexia.contracts import ReportCardPayload


@pytest.fixture
def _legacy_renderer_flags(monkeypatch):
    """Pin the PPT pipeline flags OFF for this module (not exercised here)."""
    from app.config import settings

    monkeypatch.setattr(settings, "PPT_DECK_PLANNER_ENABLED", False)
    monkeypatch.setattr(settings, "PPT_CREATE_ARTIFACT_PIPELINE_ENABLED", False)
    monkeypatch.setattr(settings, "PPT_DECK_DATA_GROUNDING_ENABLED", False)


def _fake_service():
    service = MagicMock()
    service.create_artifact.return_value = MagicMock(id="art-1")
    version = MagicMock(id="ver-1", version_number=1)
    service.create_version.return_value = version
    return service


def _call_tool(args):
    """Run _create_artifact_tool with a mocked ArtifactService."""
    from app.services.tool_handlers.artifact_tool import _create_artifact_tool

    with patch("app.services.artifacts.artifact_service.ArtifactService") as MockService:
        MockService.return_value = _fake_service()
        import asyncio

        return asyncio.get_event_loop().run_until_complete(
            _create_artifact_tool(
                args=args,
                db=object(),
                user_id="u1",
                context={"conversation_id": "c1"},
            )
        )


@pytest.mark.asyncio
async def test_html_with_html_content_preserves_exact_markup():
    """html_content payload → success, rendered bytes are the exact markup."""
    from app.services.tool_handlers.artifact_tool import _create_artifact_tool

    markup = "<!doctype html><html><body><h1>Supply Chain</h1></body></html>"
    with patch("app.services.artifacts.artifact_service.ArtifactService") as MockService:
        MockService.return_value = _fake_service()
        with patch(
            "app.services.tool_handlers.artifact_tool._render_sidecar_html"
        ) as m_sidecar:
            result = await _create_artifact_tool(
                args={
                    "type": "html",
                    "title": "SC Report",
                    "payload": {"html_content": markup},
                },
                db=object(),
                user_id="u1",
                context={"conversation_id": "c1"},
            )

    assert result["success"] is True, result
    # The provided markup must be used as-is; the sidecar renderer must NOT
    # have been engaged.
    m_sidecar.assert_not_called()


@pytest.mark.asyncio
async def test_html_with_reportcard_payload_renders_sidecar_html():
    """REGRESSION: report-card payload (no html_content) must succeed by
    rendering the sidecar HTML report instead of hard-failing."""
    from app.services.tool_handlers.artifact_tool import _create_artifact_tool

    with patch("app.services.artifacts.artifact_service.ArtifactService") as MockService:
        MockService.return_value = _fake_service()
        with patch(
            "app.services.tool_handlers.artifact_tool._render_sidecar_html",
            return_value=b"<html>RENDERED-FROM-REPORTCARD</html>",
        ) as m_sidecar:
            result = await _create_artifact_tool(
                args={
                    "type": "html",
                    "title": "Supply Chain Report — August 2026 (30-Day)",
                    "payload": {
                        "summary": "August 2026 contracted volume totaled 39,769 tons.",
                        "kpis": [
                            {"label": "Contracted Volume", "value": "39,769 t"},
                            {"label": "Delivered Volume", "value": "24,706 t"},
                        ],
                        "source": "aipdp_data_warehouse_prod",
                    },
                },
                db=object(),
                user_id="u1",
                context={"conversation_id": "c1"},
            )

    assert result["success"] is True, result
    m_sidecar.assert_called_once()
    rcp = m_sidecar.call_args[0][0]
    assert isinstance(rcp, ReportCardPayload)
    assert "39,769 tons" in rcp.summary
    assert len(rcp.kpis) == 2


@pytest.mark.asyncio
async def test_html_with_no_content_and_no_reportcard_fields_fails_helpfully():
    """A truly empty payload still fails, but with a message that tells the
    LLM exactly which shape to use (old message was a dead end)."""
    from app.services.tool_handlers.artifact_tool import _create_artifact_tool

    with patch("app.services.artifacts.artifact_service.ArtifactService") as MockService:
        MockService.return_value = _fake_service()
        result = await _create_artifact_tool(
            args={
                "type": "html",
                "title": "Empty",
                "payload": {"title": "x"},  # passes GUARD 1, but no content fields
            },
            db=object(),
            user_id="u1",
            context={"conversation_id": "c1"},
        )

    assert result["success"] is False
    assert "html_content is required" in result["error"]
    assert "report-card fields" in result["error"]


@pytest.mark.asyncio
async def test_html_render_failure_path_unchanged_when_service_fails():
    """If ArtifactService.create_version fails, the version is marked failed
    and the error surfaces (existing contract preserved)."""
    from app.services.tool_handlers.artifact_tool import _create_artifact_tool

    service = MagicMock()
    service.create_artifact.return_value = MagicMock(id="art-2")
    service.create_version.return_value = None  # version creation fails

    with patch("app.services.artifacts.artifact_service.ArtifactService") as MockService:
        MockService.return_value = service
        result = await _create_artifact_tool(
            args={
                "type": "html",
                "title": "SC",
                "payload": {"html_content": "<html><body>ok</body></html>"},
            },
            db=object(),
            user_id="u1",
            context={"conversation_id": "c1"},
        )

    assert result["success"] is False
    assert "Failed to create artifact version" in result["error"]
