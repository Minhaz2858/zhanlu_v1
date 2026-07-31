"""Phase 2 schema: runtime ALTER TABLE ensure adds the new columns."""
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from sqlalchemy import inspect as sa_inspect

from app.database import engine
from app.models.automation_execution import AutomationExecution
from app.models.automation_task import AutomationTask
from app.services import automation_dispatcher as disp


def test_model_columns_exist():
    assert hasattr(AutomationExecution, "tool_calls")
    assert hasattr(AutomationExecution, "tool_failures")
    assert hasattr(AutomationExecution, "verification_status")
    assert hasattr(AutomationTask, "verify_outputs")
    assert hasattr(AutomationTask, "last_alert_at")


def test_ensure_schema_adds_columns():
    disp._ensure_schema()
    cols_exec = {c["name"] for c in sa_inspect(engine).get_columns("automation_executions")}
    assert {"tool_calls", "tool_failures", "verification_status"} <= cols_exec
    cols_task = {c["name"] for c in sa_inspect(engine).get_columns("automation_tasks")}
    assert {"verify_outputs", "last_alert_at"} <= cols_task
