"""Schedule Trigger tests — pure parts (due detection) with a fake DB."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services.workflow_scheduler import find_due_scheduled_workflows

T = datetime(2026, 9, 1, 12, 1, 5, tzinfo=timezone.utc)  # 12:01:05 UTC


class FakeQuery:
    def __init__(self, rows, filters=None):
        self.rows = rows
        self.filters = filters or []

    def filter(self, *args, **kwargs):
        return FakeQuery(self.rows, self.filters + list(args))

    def all(self):
        out = self.rows
        for cond in self.filters:
            # SQLAlchemy BinaryExpression on a model attribute — emulate the
            # common `Model.status == "published"` shape.
            left = getattr(cond, "left", None)
            right = getattr(cond, "right", None)
            name = getattr(left, "name", None)
            value = getattr(right, "value", getattr(right, "arg", None))
            if name is not None and value is not None:
                out = [r for r in out if getattr(r, name, None) == value]
        return out


class FakeDB:
    def __init__(self, agents):
        self.agents = agents

    def query(self, model):
        return FakeQuery(self.agents)


def _agent(agent_id, status="published", nodes=None):
    return SimpleNamespace(
        id=agent_id,
        status=status,
        is_deleted=False,
        flow={"version": 1, "nodes": nodes or [], "edges": []},
    )


def _schedule_node(cron, input_text=""):
    return {"id": "s1", "type": "trigger_schedule", "position": {"x": 0, "y": 0},
            "data": {"label": "Sched", "config": {"cron": cron, "input": input_text}}}


def test_due_when_cron_fires_within_poll_window():
    # "* * * * *" fires at 12:01:00 — inside the (12:00:35, 12:01:05] window.
    agents = [_agent("wf1", nodes=[_schedule_node("* * * * *")])]
    due = find_due_scheduled_workflows(FakeDB(agents), now=T)
    assert len(due) == 1
    agent, node, config = due[0]
    assert agent.id == "wf1"
    assert node["type"] == "trigger_schedule"
    assert config["cron"] == "* * * * *"


def test_not_due_when_next_fire_is_in_the_future():
    # Daily cron at 09:00 — next fire is tomorrow, not within the window.
    agents = [_agent("wf1", nodes=[_schedule_node("0 9 * * *")])]
    due = find_due_scheduled_workflows(FakeDB(agents), now=T)
    assert due == []


def test_due_catches_missed_daily_fire_within_window():
    # now = 09:00:05, since = 08:59:35 → the 09:00 fire fell inside the window.
    now = datetime(2026, 9, 1, 9, 0, 5, tzinfo=timezone.utc)
    since = now - timedelta(seconds=30)
    agents = [_agent("wf1", nodes=[_schedule_node("0 9 * * *")])]
    due = find_due_scheduled_workflows(FakeDB(agents), now=now, since=since)
    assert len(due) == 1


def test_draft_workflow_never_fires():
    agents = [_agent("wf1", status="draft", nodes=[_schedule_node("* * * * *")])]
    assert find_due_scheduled_workflows(FakeDB(agents), now=T) == []


def test_bad_cron_skipped():
    agents = [_agent("wf1", nodes=[_schedule_node("not-a-cron")])]
    assert find_due_scheduled_workflows(FakeDB(agents), now=T) == []


def test_workflow_without_schedule_trigger_ignored():
    agents = [_agent("wf1", nodes=[
        {"id": "t1", "type": "trigger_chat", "position": {"x": 0, "y": 0}, "data": {"label": "T", "config": {}}},
    ])]
    assert find_due_scheduled_workflows(FakeDB(agents), now=T) == []
