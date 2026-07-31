"""Regression: task creator user missing -> clean failure, no NoneType crash."""
import os, sys
import pytest

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


class _FakeDBNoUser:
    def get(self, model, key):
        return None  # creator user does not resolve


class _FakeTask:
    id = "task-1"
    name = "T"
    created_by_id = "ghost-user"
    session_id = None
    org_id = "default-org"
    app_id = "default-app"
    project_id = None


class _FakeAgent:
    id = "agent-1"
    name = "runtime"


def test_run_agent_in_conversation_raises_creator_missing():
    from app.services import automation_executor as ax
    with pytest.raises(ax._TaskCreatorMissingError):
        ax._run_agent_in_conversation(
            _FakeTask(), _FakeAgent(), "do things", "exec-1",
            db_override=_FakeDBNoUser(),
        )
