"""Regression test: sandbox auto-export args must be JSON-serializable.

Root-cause test for the 2026-08-21 production bug: finalize_into_artifact
passed raw ``InsightSpec`` pydantic objects in ``args["insights"]`` to
``run_sandbox_skill_sync``. The sandbox runner JSON-serializes args for the
Docker job config.json, raised ``Object of type InsightSpec is not JSON
serializable`` during a flush, poisoned the SQLAlchemy session, and made
the ENTIRE artifact write roll back (82 occurrences in 24h of logs).
"""

import json
from unittest.mock import MagicMock, patch

from app.services.synexia.contracts import (
    InsightSpec,
    KPISpec,
    ReportCardPayload,
)
from app.services.synexia.finalize import finalize_into_artifact


def _payload_with_insights() -> ReportCardPayload:
    return ReportCardPayload(
        title="Sales Report",
        summary="Total revenue for last 30 days.",
        kpis=[KPISpec(label="Revenue", value="12345.0")],
        insights=[
            InsightSpec(icon="trending-up", text="Top performer is C5."),
            InsightSpec(icon="alert-triangle", text="Concentration risk."),
        ],
    )


def test_sandbox_export_args_are_json_serializable():
    """args passed to run_sandbox_skill_sync must survive json.dumps even
    when the payload carries pydantic InsightSpec objects."""
    captured: dict = {}

    def _fake_run_sandbox_skill_sync(*, args, db, user_id, **kwargs):
        captured["args"] = args
        return {"success": False, "error": "stubbed"}

    db = MagicMock()
    with patch(
        "app.services.tool_handlers.sandbox_tool.run_sandbox_skill_sync",
        side_effect=_fake_run_sandbox_skill_sync,
    ), patch(
        # Skip eager-render (needs a real DB / renderer); it is non-fatal
        # anyway but keeping it silent makes failures loud.
        "app.services.synexia.finalize.ExportService",
        MagicMock(),
    ):
        finalize_into_artifact(
            db,
            conversation_id="conv-test",
            agent_name="test-agent",
            user_message="make me a DOCX sales report",
            source="erp_product_sales_details",
            sql="SELECT 1",
            payload=_payload_with_insights(),
            message_id=None,
        )

    assert "args" in captured, "file intent should have triggered sandbox call"
    # THE regression assertion: raw InsightSpec objects are not JSON-serializable.
    args = captured["args"]
    assert isinstance(args["insights"], list) and args["insights"], (
        "insights must be a non-empty list"
    )
    assert all(isinstance(i, dict) for i in args["insights"]), (
        "insights entries must be plain dicts, not pydantic objects"
    )
    json.dumps(args)  # must not raise


def test_sandbox_export_insight_dict_shape():
    """The serialized insight dicts keep icon+text fields the sandbox
    renderer depends on."""
    captured: dict = {}

    def _fake_run_sandbox_skill_sync(*, args, db, user_id, **kwargs):
        captured["args"] = args
        return {"success": False, "error": "stubbed"}

    db = MagicMock()
    with patch(
        "app.services.tool_handlers.sandbox_tool.run_sandbox_skill_sync",
        side_effect=_fake_run_sandbox_skill_sync,
    ), patch(
        "app.services.synexia.finalize.ExportService",
        MagicMock(),
    ):
        finalize_into_artifact(
            db,
            conversation_id="conv-test",
            agent_name="test-agent",
            user_message="export to docx please",
            source=None,
            sql=None,
            payload=_payload_with_insights(),
        )

    insights = captured["args"]["insights"]
    assert insights[0] == {"icon": "trending-up", "text": "Top performer is C5."}
