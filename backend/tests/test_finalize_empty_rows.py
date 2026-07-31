"""
Gap A: a data question that returns 0 rows must still produce the
requested file/artifact — never a dead end.

Covers:
  1. build_no_data_payload() produces a valid, JSON-serializable
     ReportCardPayload whose single KPI doubles as the sandbox data row.
  2. finalize_into_artifact() with a no-data payload persists the
     Artifact + HTML blob + MessageArtifact link even when the file
     export backend is unavailable (best-effort, non-fatal).
  3. When the sandbox export succeeds, file_exports carries the requested
     format so the chat loop attaches the export artifact_id.
"""

import json
import sys, os
from uuid import uuid4

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.database import SessionLocal, engine, Base
from app.models.artifact import Artifact, MessageArtifact
from app.routers.agents import _should_finalize_no_data
from app.services.synexia.finalize import (
    build_no_data_payload,
    finalize_into_artifact,
)


# ---------------------------------------------------------------------------
# _should_finalize_no_data gate (code-review Important #1)
# ---------------------------------------------------------------------------


def test_gate_true_only_for_ran_and_empty():
    assert _should_finalize_no_data(
        {"success": True, "rows": [], "sql": "SELECT 1"}, "pptx",
    ) is True


def test_gate_false_when_rows_none_no_query_ran():
    # rows=None means the data agent answered conversationally without
    # ever querying — a "0 rows" narrative would be misleading.
    assert _should_finalize_no_data(
        {"success": True, "rows": None, "answer": "I cannot access that"}, "pptx",
    ) is False
    assert _should_finalize_no_data(
        {"success": True, "answer": "clarification please"}, "docx",
    ) is False  # no rows key at all


def test_gate_false_when_rows_present():
    assert _should_finalize_no_data(
        {"success": True, "rows": [{"a": 1}]}, "pptx",
    ) is False


def test_gate_false_when_failed_or_no_doc_intent():
    assert _should_finalize_no_data(
        {"success": False, "rows": []}, "pptx",
    ) is False
    assert _should_finalize_no_data(
        {"success": True, "rows": []}, None,
    ) is False
    assert _should_finalize_no_data("not a dict", "pptx") is False


@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


# ---------------------------------------------------------------------------
# build_no_data_payload
# ---------------------------------------------------------------------------


def test_no_data_payload_shape_and_serializable():
    p = build_no_data_payload(
        user_message="create a pptx of quarterly sales",
        source="sales_db",
        sql="SELECT * FROM sales WHERE region = 'nowhere'",
    )
    dumped = p.model_dump()
    json.dumps(dumped)  # must be JSON-serializable (stored on tool result)

    assert dumped["kpis"] == [{"label": "Rows returned", "value": "0", "delta": None, "caption": None}]
    assert any("0 rows" in i["text"] for i in dumped["insights"])
    assert any("SELECT * FROM sales" in i["text"] for i in dumped["insights"])
    assert dumped["chart"] is None
    assert dumped["title"]
    assert dumped["summary"]


def test_no_data_payload_without_sql():
    p = build_no_data_payload(user_message="", source=None, sql=None)
    dumped = p.model_dump()
    json.dumps(dumped)
    assert dumped["title"]  # falls back to a default title
    assert not any("SQL" in i["text"] for i in dumped["insights"])


# ---------------------------------------------------------------------------
# finalize_into_artifact with a no-data payload
# ---------------------------------------------------------------------------


def test_finalize_no_data_persists_artifact_and_link(db, monkeypatch):
    # Keep the export side-effects out of the test: eager render is a no-op
    # and the sandbox is unavailable (returns failure) — finalize must still
    # persist the html_report artifact and link it to the message.
    monkeypatch.setattr(
        "app.services.synexia.finalize.ExportService",
        lambda _db: type("ES", (), {"eager_render_default": lambda *a, **k: None})(),
    )
    monkeypatch.setattr(
        "app.services.tool_handlers.sandbox_tool.run_sandbox_skill_sync",
        lambda *a, **k: {"success": False, "error": "no sandbox in test"},
    )

    mid, cid = str(uuid4()), str(uuid4())
    payload = build_no_data_payload(
        user_message="create a pptx of quarterly sales",
        source="sales_db",
        sql="SELECT 1",
    )

    artifact, file_exports = finalize_into_artifact(
        db,
        conversation_id=cid,
        agent_name="test-agent",
        user_message="create a pptx of quarterly sales",
        source="sales_db",
        sql="SELECT 1",
        payload=payload,
        message_id=mid,
    )

    assert artifact is not None
    assert file_exports == {}  # sandbox unavailable — non-fatal

    row = db.query(Artifact).filter(Artifact.id == artifact.id).one()
    assert row.artifact_type == "html_report"
    assert row.metadata_json["report_card_payload"]["kpis"][0]["value"] == "0"

    links = db.query(MessageArtifact).filter(
        MessageArtifact.message_id == mid,
        MessageArtifact.artifact_id == artifact.id,
    ).all()
    assert len(links) == 1


def test_finalize_no_data_file_export_populates_file_exports(db, monkeypatch):
    monkeypatch.setattr(
        "app.services.synexia.finalize.ExportService",
        lambda _db: type("ES", (), {"eager_render_default": lambda *a, **k: None})(),
    )
    export_id = str(uuid4())
    monkeypatch.setattr(
        "app.services.tool_handlers.sandbox_tool.run_sandbox_skill_sync",
        lambda *a, **k: {
            "success": True,
            "artifact_id": export_id,
            "preview_url": f"/api/artifacts/{export_id}/preview",
            "download_url": f"/api/artifacts/{export_id}/download",
            "job_id": "job-1",
        },
    )

    mid, cid = str(uuid4()), str(uuid4())
    payload = build_no_data_payload(
        user_message="create a pptx of quarterly sales",
        source="sales_db",
        sql="SELECT 1",
    )

    artifact, file_exports = finalize_into_artifact(
        db,
        conversation_id=cid,
        agent_name="test-agent",
        user_message="create a pptx of quarterly sales",
        source="sales_db",
        sql="SELECT 1",
        payload=payload,
        message_id=mid,
    )

    assert artifact is not None
    assert "pptx" in file_exports
    entry = file_exports["pptx"]
    assert entry["artifact_id"] == export_id
    assert entry["preview_url"].endswith("/preview")
    # user_signal mutated in-place so the frontend picks the export surface
    assert payload.user_signal != "default"
