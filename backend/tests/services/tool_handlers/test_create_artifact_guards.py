import asyncio
from unittest.mock import MagicMock, patch

import pytest

from app.services.tool_handlers._errors import ToolExecutionError
from app.services.tool_handlers.artifact_tool import _create_artifact_tool


def _fake_service():
    service = MagicMock()
    service.create_artifact.return_value = MagicMock(id="art-1")
    service.create_version.return_value = MagicMock(id="ver-1", version_number=1)
    return service


def _make_spy():
    """Record every callable handed to asyncio.to_thread, then run it for real
    so the surrounding flow still executes (same pattern as the offload tests)."""
    calls = []
    real_to_thread = asyncio.to_thread

    def spy(fn, *args, **kwargs):
        calls.append(fn)
        return real_to_thread(fn, *args, **kwargs)

    return calls, spy


@pytest.mark.asyncio
async def test_guard_1_raises_when_no_execution_id_and_no_data():
    db = MagicMock()
    args = {"type": "docx", "title": "Test", "payload": {}}
    with pytest.raises(ToolExecutionError):
        await _create_artifact_tool(args=args, db=db, user_id="u1")


@pytest.mark.asyncio
async def test_guard_1_raises_when_payload_missing_entirely():
    db = MagicMock()
    args = {"type": "docx", "title": "Test"}
    with pytest.raises(ToolExecutionError):
        await _create_artifact_tool(args=args, db=db, user_id="u1")


@pytest.mark.asyncio
async def test_guard_1_passes_with_summary_payload():
    """Regression: summary-only payloads (legacy) must NOT be blocked."""
    calls, spy = _make_spy()
    mime = (
        "application/vnd.openxmlformats-officedocument"
        ".wordprocessingml.document"
    )
    patches = [
        patch("asyncio.to_thread", new=spy),
        patch("app.services.artifacts.artifact_service.ArtifactService"),
        patch(
            "app.services.tool_handlers.artifact_tool._enrich_payload_from_sibling_html_report",
            side_effect=lambda **kw: kw.get("payload"),
        ),
        patch("app.services.tool_handlers.artifact_tool.log_deck_event_fire_and_forget"),
    ]
    with patches[0] as _t, patches[1] as MockService, patches[2], patches[3], patch(
        "app.services.artifacts.exporters.docx_export.render",
        return_value=(b"fake-docx", mime, "docx"),
    ), patch(
        "app.services.artifacts.preview_builder.convert_to_preview",
        return_value=None,
    ), patch(
        "app.services.tool_handlers.artifact_tool._create_sidecar_preview",
        return_value=None,
    ):
        MockService.return_value = _fake_service()
        db = MagicMock()
        args = {
            "type": "docx",
            "title": "Test",
            "payload": {"summary": "July 2026 sales"},
        }
        result = await _create_artifact_tool(args=args, db=db, user_id="u1")
    assert isinstance(result, dict)
    assert result["success"] is True, result


@pytest.mark.asyncio
async def test_xlsx_artifact_type_accepted():
    """Regression (2026-08-29): the prompt documents XLSX as a supported
    artifact type (payload={'sheets': [...]}) and the exporter + model both
    support it, but the tool allowlist rejected 'xlsx' — forcing the agent
    to flail (sandbox / execute_code / fabricated completions) whenever a
    user asked for Excel. The allowlist now includes xlsx."""
    calls, spy = _make_spy()
    patches = [
        patch("asyncio.to_thread", new=spy),
        patch("app.services.artifacts.artifact_service.ArtifactService"),
        patch(
            "app.services.tool_handlers.artifact_tool._enrich_payload_from_sibling_html_report",
            side_effect=lambda **kw: kw.get("payload"),
        ),
        patch("app.services.tool_handlers.artifact_tool.log_deck_event_fire_and_forget"),
    ]
    with patches[0] as _t, patches[1] as MockService, patches[2], patches[3], patch(
        "app.services.artifacts.exporters.xlsx_export.render",
        return_value=(
            b"fake-xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".xlsx",
        ),
    ), patch(
        "app.services.artifacts.preview_builder.convert_to_preview",
        return_value=None,
    ), patch(
        "app.services.tool_handlers.artifact_tool._create_sidecar_preview",
        return_value=None,
    ):
        MockService.return_value = _fake_service()
        db = MagicMock()
        args = {
            "type": "xlsx",
            "title": "Top 10 Customers",
            "payload": {
                "sheets": [
                    {
                        "title": "Customers",
                        "rows": [
                            {"name": "ACME", "revenue": 1000},
                            {"name": "Globex", "revenue": 900},
                        ],
                    },
                ],
            },
        }
        result = await _create_artifact_tool(args=args, db=db, user_id="u1")
    assert isinstance(result, dict)
    assert result["success"] is True, result


# --- GUARD 2: execution resolution -------------------------------------------


@pytest.mark.asyncio
async def test_payload_only_call_never_resolves_last_execution():
    """Regression (2026-08-29): a NEW analysis carrying its own payload must
    NEVER be silently merged with the session's last cached execution (a
    PREVIOUS topic's data — the 'same file for a new request' bug class,
    conv 945c7cf2). The stale session-state fallback was removed: with no
    source_execution_id the payload is authoritative and NO cached
    DataExecution is resolved."""
    calls, spy = _make_spy()
    mime = (
        "application/vnd.openxmlformats-officedocument"
        ".wordprocessingml.document"
    )
    patches = [
        patch("asyncio.to_thread", new=spy),
        patch("app.services.artifacts.artifact_service.ArtifactService"),
        patch(
            "app.services.tool_handlers.artifact_tool._enrich_payload_from_sibling_html_report",
            side_effect=lambda **kw: kw.get("payload"),
        ),
        patch("app.services.tool_handlers.artifact_tool.log_deck_event_fire_and_forget"),
    ]
    with patches[0] as _t, patches[1] as MockService, patches[2], patches[3], patch(
        "app.services.artifacts.exporters.docx_export.render",
        return_value=(b"fake-docx", mime, "docx"),
    ), patch(
        "app.services.artifacts.preview_builder.convert_to_preview",
        return_value=None,
    ), patch(
        "app.services.tool_handlers.artifact_tool._create_sidecar_preview",
        return_value=None,
    ), patch(
        # If the stale fallback were still wired, this would be called with
        # the session's last execution id and blow up here.
        "app.services.tool_handlers.artifact_tool.DataExecutionService.get_by_id",
        side_effect=AssertionError(
            "payload-only call must not resolve the session's last execution"
        ),
    ):
        MockService.return_value = _fake_service()
        db = MagicMock()
        args = {
            "type": "docx",
            "title": "Top 10 Customers",
            "payload": {"kpis": [{"label": "Customers", "value": 10}]},
        }
        result = await _create_artifact_tool(
            args=args, db=db, user_id="u1",
            context={"conversation_id": "conv-with-stale-execution"},
        )
    assert isinstance(result, dict)
    assert result["success"] is True, result


@pytest.mark.asyncio
async def test_guard_2_raises_when_execution_id_not_found():
    db = MagicMock()
    args = {"type": "docx", "title": "Test", "source_execution_id": "evt_nope"}
    with patch(
        "app.services.tool_handlers.artifact_tool.DataExecutionService"
    ) as mock_svc:
        mock_svc.get_by_id.return_value = None
        with pytest.raises(ToolExecutionError):
            await _create_artifact_tool(args=args, db=db, user_id="u1")


@pytest.mark.asyncio
async def test_guard_2_raises_when_execution_belongs_to_different_session():
    db = MagicMock()
    fake = MagicMock(session_id="other-session")
    fake.is_expired.return_value = False
    args = {"type": "docx", "title": "Test", "source_execution_id": "evt_abc"}
    with patch(
        "app.services.tool_handlers.artifact_tool.DataExecutionService"
    ) as mock_svc:
        mock_svc.get_by_id.return_value = fake
        with pytest.raises(ToolExecutionError):
            await _create_artifact_tool(
                args=args,
                db=db,
                user_id="u1",
                context={"conversation_id": "conv-1"},
            )


@pytest.mark.asyncio
async def test_guard_2_happy_path_drives_to_completion():
    calls, spy = _make_spy()
    mime = (
        "application/vnd.openxmlformats-officedocument"
        ".wordprocessingml.document"
    )
    fake = MagicMock(session_id="conv-1", tool_name="ask_data_agent")
    fake.is_expired.return_value = False
    fake.result = {
        "rows": [[100]],
        "columns": ["value"],
        "summary": "cached summary",
    }
    patches = [
        patch("asyncio.to_thread", new=spy),
        patch("app.services.artifacts.artifact_service.ArtifactService"),
        patch(
            "app.services.tool_handlers.artifact_tool._enrich_payload_from_sibling_html_report",
            side_effect=lambda **kw: kw.get("payload"),
        ),
        patch("app.services.tool_handlers.artifact_tool.log_deck_event_fire_and_forget"),
        patch("app.services.tool_handlers.artifact_tool.DataExecutionService"),
    ]
    with patches[0] as _t, patches[1] as MockService, patches[2], patches[3], patches[4] as MockSvc, patch(
        "app.services.artifacts.exporters.docx_export.render",
        return_value=(b"fake-docx", mime, "docx"),
    ), patch(
        "app.services.artifacts.preview_builder.convert_to_preview",
        return_value=None,
    ), patch(
        "app.services.tool_handlers.artifact_tool._create_sidecar_preview",
        return_value=None,
    ):
        MockService.return_value = _fake_service()
        MockSvc.get_by_id.return_value = fake
        db = MagicMock()
        args = {
            "type": "docx",
            "title": "Re-export",
            "payload": {"summary": "July 2026 sales"},
            "source_execution_id": "evt_abc",
        }
        result = await _create_artifact_tool(
            args=args,
            db=db,
            user_id="u1",
            context={"conversation_id": "conv-1"},
        )
    assert isinstance(result, dict)
    assert result["success"] is True, result
