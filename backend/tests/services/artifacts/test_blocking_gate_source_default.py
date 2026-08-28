"""Blocking-gate source-label + empty-bytes guard tests.

CRITICAL regression lock (2026-08-28):

1a. The chat path builds the deck ExportContext WITHOUT a ``source`` label and
    the planner never sets one.  ``source_citation`` is a non-repairable FAIL
    audit rule, so with the blocking gate on the dispatcher returned ``b""``
    and ``artifact_tool`` stored a 0-byte pptx as "success".  Fix: the export
    pipeline ALWAYS defaults ``ctx.source`` (payload source → grounding KB
    marker → "company data") so the layout-engine footer renders and the
    citation audit passes.

1b. A blocked deck (``b""`` + FAIL report) must NEVER be stored as a
    successful artifact: ``_create_artifact_tool`` refuses to store empty
    bytes and returns ``success=False`` instead.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.services.artifacts.exporters._common import ExportContext
from app.services.artifacts.exporters.service import ExportService
from app.services.synexia.contracts import InsightSpec, ReportCardPayload

_PPTX_MIME = (
    "application/vnd.openxmlformats-officedocument"
    ".presentationml.presentation"
)


def _make_db():
    engine = sa.create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _fake_service():
    service = MagicMock()
    service.create_artifact.return_value = MagicMock(id="art-1", metadata_json=None)
    service.create_version.return_value = MagicMock(id="ver-1", version_number=1)
    return service


# ---------------------------------------------------------------------------
# 1a — chat-path decks ALWAYS carry a source label
# ---------------------------------------------------------------------------


def _enable_blocking_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "PPT_DECK_PLANNER_ENABLED", True)
    monkeypatch.setattr(settings, "PPT_AUDIT_ENABLED", True)
    monkeypatch.setattr(settings, "PPT_AUDIT_BLOCKING_ENABLED", True)
    monkeypatch.setattr(settings, "PPT_LLM_POLISH_ENABLED", False)
    monkeypatch.setattr(settings, "PPT_SMART_ROUTER_ENABLED", False)


def _rich_payload() -> ReportCardPayload:
    """A rich narrative payload (the chat-path shape) with NO source."""
    return ReportCardPayload(
        title="Q2 Enterprise Review",
        source="",  # the bug: the LLM/agent never fills this in
        summary="Enterprise led the quarter with record bookings and 18% revenue growth.",
        key_findings=[InsightSpec(text="Enterprise revenue grew 18% in Q2", icon="trending_up")],
    )


def test_rich_deck_without_source_renders_and_passes_citation_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rich-payload deck through render_pptx_deck with NO explicit source
    still renders non-empty bytes AND the audit source_citation passes because
    the pipeline defaulted the source label into the context."""
    _enable_blocking_gate(monkeypatch)

    svc = ExportService(_make_db())
    ctx = ExportContext(conversation_id="conv-1", user_message="build me a deck")  # no source

    data, mime, ext, audit_report = svc._render_deck_pipeline(
        _rich_payload(), ctx, [{"region": "EA", "sales": 10}],
        user_message="build me a deck",
    )

    # The deck is real pptx bytes — NOT b'' from the blocking gate.
    assert data and len(data) >= 10000
    assert ext == "pptx"
    # The context picked up a default source label.
    assert (ctx.source or "").strip(), "ctx.source was not defaulted"
    # The blocking gate did not trip: no FAIL rules.
    assert audit_report.get("status") != "FAIL", audit_report
    citation = next(
        (r for r in audit_report.get("rules", []) if r.get("id") == "source_citation"),
        None,
    )
    assert citation is not None, "source_citation rule missing from audit report"
    assert citation["level"] == "PASS", citation


def test_render_pptx_deck_wrapper_defaults_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public chat-path wrapper (artifact_tool calls this) also gets the
    defaulted label — the in-chat deck and the download are both covered."""
    _enable_blocking_gate(monkeypatch)

    svc = ExportService(_make_db())
    ctx = ExportContext(conversation_id="conv-1")

    data, mime, ext = svc.render_pptx_deck(
        _rich_payload(), ctx, [{"region": "EA", "sales": 10}],
        user_message="deck please",
    )

    assert data and len(data) >= 10000
    assert ext == "pptx"
    assert (ctx.source or "").strip()


def test_payload_source_is_preferred_over_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the payload names a real data source, that label wins over the
    generic default (provenance accuracy)."""
    _enable_blocking_gate(monkeypatch)

    payload = _rich_payload()
    payload.source = "aipdp_data_warehouse_prod"

    svc = ExportService(_make_db())
    ctx = ExportContext(conversation_id="conv-1")

    data, _mime, _ext, _report = svc._render_deck_pipeline(
        payload, ctx, [], user_message="deck please",
    )
    assert data  # still renders
    assert ctx.source == "aipdp_data_warehouse_prod"


# ---------------------------------------------------------------------------
# 1b — a blocked deck (b'' + FAIL) is NEVER stored as a success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blocked_deck_empty_bytes_not_stored() -> None:
    """When the audit gate returns b'' + FAIL, create_artifact returns
    success=False and NO blob is stored (no 0-byte artifact)."""
    from app.services.tool_handlers.artifact_tool import _create_artifact_tool

    async def _to_thread_inline(fn, *args, **kwargs):
        # Resolve the offloaded call inline (the real to_thread runs it in a
        # worker thread; for this test the patched render returns instantly).
        return fn(*args, **kwargs)

    settings.PPT_DECK_PLANNER_ENABLED = True
    settings.PPT_CREATE_ARTIFACT_PIPELINE_ENABLED = True
    try:
        with patch(
            "asyncio.to_thread",
            new=_to_thread_inline,
        ), patch(
            "app.services.artifacts.artifact_service.ArtifactService"
        ) as MockService, patch(
            "app.services.tool_handlers.artifact_tool._enrich_payload_from_sibling_html_report",
            side_effect=lambda **kw: kw.get("payload"),
        ), patch(
            "app.services.tool_handlers.artifact_tool.log_deck_event_fire_and_forget"
        ), patch.object(
            ExportService,
            "render_pptx_deck",
            return_value=(b"", _PPTX_MIME, "pptx"),  # the blocked-deck shape
        ), patch.object(
            ExportService, "_resolve_brand_tokens", return_value=(None, None)
        ), patch.object(
            ExportService, "_attach_format_blob"
        ) as m_attach, patch(
            "app.services.artifacts.preview_builder.convert_to_preview",
            return_value=None,
        ), patch(
            "app.services.tool_handlers.artifact_tool._create_sidecar_preview",
            return_value=None,
        ):
            svc = _fake_service()
            MockService.return_value = svc

            result = await _create_artifact_tool(
                args={
                    "type": "pptx",
                    "title": "Blocked Deck",
                    "payload": {"summary": "x"},
                },
                db=_make_db(),
                user_id="u1",
                context={"conversation_id": "c1"},
            )
    finally:
        settings.PPT_DECK_PLANNER_ENABLED = False
        settings.PPT_CREATE_ARTIFACT_PIPELINE_ENABLED = False

    assert result["success"] is False, result
    assert result.get("reason") == "render_empty", result
    # The 0-byte blob must never be stored / the version never marked built.
    svc.store_blob.assert_not_called()
    svc.mark_version_built.assert_not_called()
    m_attach.assert_not_called()
