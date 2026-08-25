"""Event-loop offload regression tests for ``_create_artifact_tool``.

Root cause observed in production (2026-08-19): ``_render_pptx`` /
``_render_docx`` / ``_render_pdf``, ``convert_to_preview``,
``generate_thumbnail`` and ``_create_sidecar_preview`` are long synchronous
calls (up to 120s for PDF rendering). Invoked directly inside the async
``_create_artifact_tool`` they block the event loop, so the SSE heartbeat in
``agents.py`` cannot emit its 5s keep-alive pings and the browser/proxy drops
the stream — the user sees "Sorry, the connection was interrupted."

The contract: every blocking call must go through ``asyncio.to_thread`` so the
event loop stays free to pump heartbeats, and the file-format render must be
bounded by ``ARTIFACT_RENDER_TIMEOUT_S`` with a graceful failure instead of a
hanging connection.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _legacy_renderer_flags(monkeypatch):
    """Pin the PPT pipeline flags OFF for this module.

    These tests exercise the LEGACY catalog renderer's offload contract
    (``pptx_export.render`` must go through ``asyncio.to_thread``).  The
    deployment env files now enable ``PPT_CREATE_ARTIFACT_PIPELINE_ENABLED``
    and ``PPT_DECK_DATA_GROUNDING_ENABLED``, which would otherwise route the
    pptx tests through the professional pipeline (``render_pptx_deck``) and
    never call the legacy renderer.  The pipeline path's own offload contract
    is covered by ``test_create_artifact_pipeline.py``.
    """
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


def _make_spy():
    """Record every callable handed to asyncio.to_thread, then run it for real
    so the surrounding flow still executes."""
    calls = []
    real_to_thread = asyncio.to_thread

    def spy(fn, *args, **kwargs):
        calls.append(fn)
        return real_to_thread(fn, *args, **kwargs)

    return calls, spy


def _base_patches(calls, spy):
    """Patches shared by every test (single list, entered once)."""
    # NOTE: use new=spy (plain-function patch), NOT side_effect=spy. MagicMock
    # wraps coroutine-returning calls in a generator-coroutine (created in
    # unittest/mock.py) that never drives the inner asyncio.to_thread coroutine
    # when wrapped in asyncio.wait/ensure_future — the thread body never runs.
    return [
        patch("asyncio.to_thread", new=spy),
        patch("app.services.artifacts.artifact_service.ArtifactService"),
        patch(
            "app.services.tool_handlers.artifact_tool._enrich_payload_from_sibling_html_report",
            side_effect=lambda **kw: kw.get("payload"),
        ),
        patch("app.services.tool_handlers.artifact_tool.log_deck_event_fire_and_forget"),
    ]


@pytest.mark.asyncio
async def test_pptx_render_preview_sidecar_are_offloaded_to_thread():
    """Blocking PPTX render, preview conversion and sidecar preview must be
    handed to asyncio.to_thread, never awaited directly on the event loop."""
    from app.services.tool_handlers.artifact_tool import _create_artifact_tool

    calls, spy = _make_spy()
    mime = (
        "application/vnd.openxmlformats-officedocument"
        ".presentationml.presentation"
    )
    patches = _base_patches(calls, spy)

    with patches[0] as _t, patches[1] as MockService, patches[2], patches[3], patch(
        "app.services.artifacts.exporters.pptx_export.render",
        return_value=(b"fake-pptx", mime, "pptx"),
    ) as m_render, patch(
        "app.services.artifacts.preview_builder.convert_to_preview",
        return_value=None,
    ) as m_preview, patch(
        "app.services.tool_handlers.artifact_tool._create_sidecar_preview",
        return_value=None,
    ) as m_sidecar:
        MockService.return_value = _fake_service()

        result = await _create_artifact_tool(
            args={
                "type": "pptx",
                "title": "Sales Overview July",
                "payload": {
                    "summary": "July 2026 sales",
                    "kpis": [{"label": "Revenue", "value": "37.1M"}],
                },
            },
            db=object(),
            user_id="u1",
            context={"conversation_id": "c1"},
        )

    assert result["success"] is True, result
    # Before the fix these are awaited directly: asyncio.to_thread is never
    # called with them, so this assertion fails (RED).
    assert any(fn is m_render for fn in calls), (
        "pptx render must be offloaded via asyncio.to_thread"
    )
    assert any(fn is m_preview for fn in calls), (
        "convert_to_preview must be offloaded via asyncio.to_thread"
    )
    assert any(fn is m_sidecar for fn in calls), (
        "_create_sidecar_preview must be offloaded via asyncio.to_thread"
    )


@pytest.mark.asyncio
async def test_pdf_thumbnail_chain_is_offloaded_to_thread():
    """PDF render + thumbnail generation must both go through
    asyncio.to_thread."""
    from app.services.tool_handlers.artifact_tool import _create_artifact_tool

    calls, spy = _make_spy()
    patches = _base_patches(calls, spy)

    with patches[0] as _t, patches[1] as MockService, patches[2], patches[3], patch(
        "app.services.artifacts.exporters.pdf_export.render",
        return_value=(b"fake-pdf", "application/pdf", "pdf"),
    ) as m_render, patch(
        "app.services.artifacts.preview_builder.convert_to_preview",
        return_value=(b"preview-pdf", "preview.pdf", "application/pdf"),
    ) as m_preview, patch(
        "app.services.artifacts.preview_builder.generate_thumbnail",
        return_value=b"thumb-png",
    ) as m_thumb, patch(
        "app.services.tool_handlers.artifact_tool._create_sidecar_preview",
        return_value=None,
    ):
        MockService.return_value = _fake_service()

        result = await _create_artifact_tool(
            args={
                "type": "pdf",
                "title": "Sales Overview July",
                "payload": {"summary": "July 2026 sales"},
            },
            db=object(),
            user_id="u1",
            context={"conversation_id": "c1"},
        )

    assert result["success"] is True, result
    assert any(fn is m_render for fn in calls), (
        "pdf render must be offloaded via asyncio.to_thread"
    )
    assert any(fn is m_thumb for fn in calls), (
        "generate_thumbnail must be offloaded via asyncio.to_thread"
    )


@pytest.mark.asyncio
async def test_render_timeout_fails_gracefully():
    """When a file-format render exceeds the ceiling, _create_artifact_tool
    must return a failure naming the timeout instead of hanging the stream."""
    from app.services.tool_handlers.artifact_tool import _create_artifact_tool

    calls, spy = _make_spy()
    patches = _base_patches(calls, spy)

    def _hang(*_args, **_kwargs):
        raise asyncio.TimeoutError("simulated render stall")

    with patches[0] as _t, patches[1] as MockService, patches[2], patches[3], patch(
        "app.services.artifacts.exporters.pptx_export.render",
        side_effect=_hang,
    ), patch(
        "app.services.artifacts.preview_builder.convert_to_preview",
        return_value=None,
    ), patch(
        "app.services.tool_handlers.artifact_tool._create_sidecar_preview",
        return_value=None,
    ):
        MockService.return_value = _fake_service()

        result = await _create_artifact_tool(
            args={
                "type": "pptx",
                "title": "Sales Overview July",
                "payload": {"summary": "July 2026 sales"},
            },
            db=object(),
            user_id="u1",
            context={"conversation_id": "c1"},
        )

    assert result["success"] is False
    assert "timed out" in result["error"], result
