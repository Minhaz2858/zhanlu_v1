"""E2E tests for create_artifact: cached DataExecution -> payload mapping
(Task 11: ``_payload_from_execution`` integration + 0-row warning).

Locks in:

* ``cache_data_execution`` -> ``DataExecutionService.get_by_id`` ->
  ``_payload_from_execution`` round-trips a cached ask_data_agent result
  into a ReportCard-style payload (summary + kpis).
* ``_create_artifact_tool`` enriches its payload from the cached
  DataExecution resolved by GUARD 2, and appends a "Data Quality Note"
  section when the cached result returned 0 rows.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.database import SessionLocal, engine
from app.models.data_execution import DataExecution
from app.models.session_state import SessionState
from app.services.data_execution.cache import cache_data_execution
from app.services.data_execution.execution_service import DataExecutionService
from app.services.tool_handlers._payload_from_execution import _payload_from_execution

_DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument"
    ".wordprocessingml.document"
)


def _ensure_table():
    DataExecution.__table__.create(bind=engine, checkfirst=True)
    SessionState.__table__.create(bind=engine, checkfirst=True)


@pytest.mark.asyncio
async def test_full_flow_cache_then_map():
    _ensure_table()
    db = SessionLocal()
    try:
        exec_id = await cache_data_execution(
            db=db, session_id="test-e2e-flow",
            tool_name="ask_data_agent",
            args={"query": "test"},
            result={"rows": [[100, 200]], "columns": ["a", "b"], "summary": "E2E"},
            summary_text="E2E summary", org_id="org1", app_id="default-app",
        )
        assert exec_id is not None
        execution = DataExecutionService.get_by_id(db, exec_id)
        assert execution is not None
        payload = _payload_from_execution(execution)
        assert payload["summary"] == "E2E"
        assert len(payload["kpis"]) == 2
    finally:
        db.rollback()
        db.close()


@pytest.mark.asyncio
async def test_guard2_execution_with_zero_rows_appends_warning():
    _ensure_table()
    from app.services.tool_handlers.artifact_tool import _create_artifact_tool
    db = SessionLocal()
    try:
        exec_id = await cache_data_execution(
            db=db, session_id="test-e2e-zerorow",
            tool_name="ask_data_agent",
            args={"query": "empty"},
            result={"rows": [], "columns": [], "summary": ""},
            summary_text=None, org_id="org1", app_id="default-app",
        )
        with patch("app.services.artifacts.artifact_service.ArtifactService") as MockService, patch(
            "app.services.artifacts.exporters.docx_export.render",
            return_value=(b"fake-docx", _DOCX_MIME, "docx"),
        ), patch(
            "app.services.artifacts.preview_builder.convert_to_preview",
            return_value=None,
        ), patch(
            "app.services.tool_handlers.artifact_tool._create_sidecar_preview",
            return_value=None,
        ):
            MockService.return_value = MagicMock()
            result = await _create_artifact_tool(
                args={"type": "docx", "title": "Empty Report",
                      "source_execution_id": exec_id},
                db=db, user_id="u1",
                context={"conversation_id": "test-e2e-zerorow",
                         "agent_app_id": "a1", "execution_id": exec_id,
                         "org_id": "org1", "app_id": "default-app"},
            )
        assert result.get("success") is True
        payload = result["payload"]
        assert any(
            s.get("title") == "Data Quality Note" for s in payload.get("sections", [])
        )
    finally:
        db.rollback()
        db.close()
