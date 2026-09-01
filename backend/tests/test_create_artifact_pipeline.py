"""Tests for routing ``create_artifact(type="pptx")`` through the deck
pipeline (plan items C + F).

Locks in:

* When ``PPT_DECK_PLANNER_ENABLED`` AND ``PPT_CREATE_ARTIFACT_PIPELINE_ENABLED``
  are both on, ``_create_artifact_tool`` renders the pptx through
  ``ExportService.render_pptx_deck`` (offloaded to a worker thread, keeping the
  SSE heartbeat alive), registers the exact bytes as a cached ``format_export``
  blob (so ``GET /download?format=pptx`` returns the SAME deck instead of
  re-rendering — LLM nondeterminism), and persists ``report_card_payload`` +
  ``owner_user_id`` on the artifact metadata so future re-renders reconstruct
  the enriched content (C + F).
* When the flags are off, the legacy catalog renderer is used unchanged
  (``test_artifact_tool_offload.py`` still asserts the legacy path).
* ``execution_id`` from the FSM tool context reaches ``create_artifact`` so the
  deck can be grounded in that execution's real query rows.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.config import settings

_PPTX_MIME = (
    "application/vnd.openxmlformats-officedocument"
    ".presentationml.presentation"
)


def _make_db():
    engine = sa.create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _fake_artifact():
    art = MagicMock(id="art-1")
    art.metadata_json = None
    art.org_id = "default-org"
    art.app_id = "default-app"
    return art


def _fake_service():
    service = MagicMock()
    service.create_artifact.return_value = _fake_artifact()
    version = MagicMock(id="ver-1", version_number=1)
    service.create_version.return_value = version
    return service


def _make_spy():
    """Record every callable handed to asyncio.to_thread, run it for real."""
    calls = []
    real_to_thread = asyncio.to_thread

    def spy(fn, *args, **kwargs):
        calls.append(fn)
        return real_to_thread(fn, *args, **kwargs)

    return calls, spy


def _base_patches(calls, spy):
    return [
        patch("asyncio.to_thread", new=spy),
        patch("app.services.artifacts.artifact_service.ArtifactService"),
        patch(
            "app.services.tool_handlers.artifact_tool._enrich_payload_from_sibling_html_report",
            side_effect=lambda **kw: kw.get("payload"),
        ),
        patch("app.services.tool_handlers.artifact_tool.log_deck_event_fire_and_forget"),
    ]


_PPTX_ARGS = {
    "type": "pptx",
    "title": "Sales Overview July",
    "payload": {
        "summary": "July 2026 sales",
        "kpis": [{"label": "Revenue", "value": "37.1M"}],
    },
}


def _run_with_flags(coro, monkey_calls, spy, service=None, *,
                    planner=True, create_artifact_flag=True):
    """Enter the shared patches + run the tool with the given flags on."""
    from app.services.tool_handlers.artifact_tool import _create_artifact_tool
    from app.services.artifacts.exporters.service import ExportService

    patches = _base_patches(monkey_calls, spy)
    settings.PPT_DECK_PLANNER_ENABLED = planner
    settings.PPT_CREATE_ARTIFACT_PIPELINE_ENABLED = create_artifact_flag
    return settings, patches, _create_artifact_tool, ExportService


@pytest.mark.asyncio
async def test_pipeline_route_renders_via_render_pptx_deck_offloaded():
    """With both pipeline flags on, the pptx render goes through
    ``render_pptx_deck`` via ``asyncio.to_thread`` (never awaited directly)."""
    from app.services.tool_handlers.artifact_tool import _create_artifact_tool
    from app.services.artifacts.exporters.service import ExportService

    monkey_calls, spy = _make_spy()
    recorded = {}

    def _fake_render_deck(payload_, ctx_, rows_, **kwargs):
        recorded["rows"] = rows_
        recorded["user_id"] = kwargs.get("user_id")
        recorded["artifact_id"] = getattr(kwargs.get("artifact"), "id", None)
        return b"fake-deck", _PPTX_MIME, "pptx"

    settings.PPT_DECK_PLANNER_ENABLED = True
    settings.PPT_CREATE_ARTIFACT_PIPELINE_ENABLED = True
    try:
        with _base_patches(monkey_calls, spy)[0] as _t, \
             _base_patches(monkey_calls, spy)[1] as MockService, \
             _base_patches(monkey_calls, spy)[2], \
             _base_patches(monkey_calls, spy)[3], \
             patch.object(ExportService, "render_pptx_deck",
                          side_effect=_fake_render_deck) as m_render, \
             patch.object(ExportService, "_resolve_brand_tokens",
                          return_value=(None, None)), \
             patch.object(ExportService, "_attach_format_blob") as m_attach, \
             patch("app.services.artifacts.preview_builder.convert_to_preview",
                   return_value=None), \
             patch("app.services.tool_handlers.artifact_tool._create_sidecar_preview",
                   return_value=None):
            MockService.return_value = _fake_service()

            result = await _create_artifact_tool(
                args=dict(_PPTX_ARGS),
                db=_make_db(),
                user_id="u1",
                context={"conversation_id": "c1", "execution_id": "exec-9"},
            )
    finally:
        settings.PPT_DECK_PLANNER_ENABLED = False
        settings.PPT_CREATE_ARTIFACT_PIPELINE_ENABLED = False

    assert result["success"] is True, result
    # render_pptx_deck must be offloaded to a worker thread (heartbeat).
    assert any(fn is m_render for fn in monkey_calls), (
        "render_pptx_deck must be offloaded via asyncio.to_thread"
    )
    assert recorded["user_id"] == "u1"
    assert recorded["artifact_id"] == "art-1"


@pytest.mark.asyncio
async def test_pipeline_route_registers_format_export_blob_and_metadata():
    """The tool registers the exact bytes as a cached format_export blob and
    persists report_card_payload + owner_user_id for byte-consistent downloads
    and re-personalization."""
    from app.services.tool_handlers.artifact_tool import _create_artifact_tool
    from app.services.artifacts.exporters.service import ExportService

    monkey_calls, spy = _make_spy()
    recorded = {}

    def _fake_attach(*, artifact, version, format, file_name,
                     mime_type, data, theme_fingerprint=None):
        recorded["attach_format"] = format
        recorded["attach_data"] = data
        recorded["attach_fp"] = theme_fingerprint
        return MagicMock(id="blob-1")

    artifact = _fake_artifact()
    version = MagicMock(id="ver-1", version_number=1)
    settings.PPT_DECK_PLANNER_ENABLED = True
    settings.PPT_CREATE_ARTIFACT_PIPELINE_ENABLED = True
    try:
        with _base_patches(monkey_calls, spy)[0] as _t, \
             _base_patches(monkey_calls, spy)[1] as MockService, \
             _base_patches(monkey_calls, spy)[2], \
             _base_patches(monkey_calls, spy)[3], \
             patch.object(ExportService, "render_pptx_deck",
                          return_value=(b"fake-deck", _PPTX_MIME, "pptx")), \
             patch.object(ExportService, "_resolve_brand_tokens",
                          return_value=(None, None)), \
             patch.object(ExportService, "_attach_format_blob",
                          side_effect=_fake_attach), \
             patch("app.services.artifacts.preview_builder.convert_to_preview",
                   return_value=None), \
             patch("app.services.tool_handlers.artifact_tool._create_sidecar_preview",
                   return_value=None):
            svc = _fake_service()
            svc.create_artifact.return_value = artifact
            svc.create_version.return_value = version
            MockService.return_value = svc

            result = await _create_artifact_tool(
                args=dict(_PPTX_ARGS),
                db=_make_db(),
                user_id="u1",
                context={"conversation_id": "c1", "execution_id": "exec-9"},
            )
    finally:
        settings.PPT_DECK_PLANNER_ENABLED = False
        settings.PPT_CREATE_ARTIFACT_PIPELINE_ENABLED = False

    assert result["success"] is True, result
    # format_export blob registered with the rendered bytes.
    assert recorded["attach_format"] == "pptx"
    assert recorded["attach_data"] == b"fake-deck"
    assert recorded["attach_fp"] is None
    # Metadata persisted for re-render / re-personalization.
    meta = artifact.metadata_json or {}
    assert meta.get("report_card_payload"), "report_card_payload not persisted"
    assert meta.get("owner_user_id") == "u1"
    # create_artifact received the execution_id for grounding.
    assert svc.create_artifact.call_args[1].get("execution_id") == "exec-9"


@pytest.mark.asyncio
async def test_pipeline_route_legacy_when_flags_off():
    """With the pipeline flags off, the legacy catalog renderer is used and
    render_pptx_deck / format blob registration do NOT happen."""
    from app.services.tool_handlers.artifact_tool import _create_artifact_tool
    from app.services.artifacts.exporters.service import ExportService

    monkey_calls, spy = _make_spy()
    settings.PPT_DECK_PLANNER_ENABLED = False
    settings.PPT_CREATE_ARTIFACT_PIPELINE_ENABLED = False
    try:
        with _base_patches(monkey_calls, spy)[0] as _t, \
             _base_patches(monkey_calls, spy)[1] as MockService, \
             _base_patches(monkey_calls, spy)[2], \
             _base_patches(monkey_calls, spy)[3], \
             patch("app.services.artifacts.exporters.pptx_export.render",
                   return_value=(b"legacy-pptx", _PPTX_MIME, "pptx")) as m_legacy, \
             patch.object(ExportService, "render_pptx_deck") as m_render, \
             patch.object(ExportService, "_attach_format_blob") as m_attach, \
             patch("app.services.artifacts.preview_builder.convert_to_preview",
                   return_value=None), \
             patch("app.services.tool_handlers.artifact_tool._create_sidecar_preview",
                   return_value=None):
            MockService.return_value = _fake_service()

            result = await _create_artifact_tool(
                args=dict(_PPTX_ARGS),
                db=_make_db(),
                user_id="u1",
                context={"conversation_id": "c1"},
            )
    finally:
        settings.PPT_DECK_PLANNER_ENABLED = False
        settings.PPT_CREATE_ARTIFACT_PIPELINE_ENABLED = False

    assert result["success"] is True, result
    assert any(fn is m_legacy for fn in monkey_calls), (
        "legacy pptx render must be offloaded via asyncio.to_thread"
    )
    m_render.assert_not_called()
    m_attach.assert_not_called()
