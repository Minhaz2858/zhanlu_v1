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
    agent, node, config, slot = due[0]
    assert agent.id == "wf1"
    assert node["type"] == "trigger_schedule"
    assert config["cron"] == "* * * * *"
    assert slot == datetime(2026, 9, 1, 12, 1, 0, tzinfo=timezone.utc)


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

# ---------------------------------------------------------------------------
# R6 Phase 5 — occurrence claim adoption (cross-process exactly-once)
# ---------------------------------------------------------------------------

def test_due_includes_representative_slot():
    """The 4th tuple element is the representative slot — the occurrence_key
    source (same-slot ticks across processes dedupe on it)."""
    agents = [_agent("wf1", nodes=[_schedule_node("* * * * *")])]
    due = find_due_scheduled_workflows(FakeDB(agents), now=T)
    agent, node, config, slot = due[0]
    assert slot == datetime(2026, 9, 1, 12, 1, 0, tzinfo=timezone.utc)


def test_tick_claims_occurrence_before_firing(monkeypatch):
    """tick claims via claim_occurrence(occurrence_cls=WorkflowOccurrence)
    BEFORE spawning the run thread — the UNIQUE (agent_app_id, node_id,
    occurrence_key) index arbitrates across processes."""
    import app.services.workflow_scheduler as ws

    agents = [_agent("wf1", nodes=[_schedule_node("* * * * *")])]
    monkeypatch.setattr(ws, "find_due_scheduled_workflows",
                        lambda db, **kw: [(agents[0],
                                           agents[0].flow["nodes"][0],
                                           {"cron": "* * * * *",
                                            "input": "hi"},
                                           datetime(2026, 9, 1, 12, 1, 0,
                                                    tzinfo=timezone.utc))])
    agents_ns = SimpleNamespace(
        id="wf1", status="published", is_deleted=False, org_id="org-1",
        app_id="app-1", created_by_id="u1",
        flow={"nodes": [_schedule_node("* * * * *")], "edges": []})

    class _FakeAgentQuery:
        def filter(self, *a, **kw):
            return self

        def first(self):
            return agents_ns

    calls = {"claim": [], "run": [], "complete": []}

    class _Occ:
        id = "occ-1"
        status = "claimed"

    def fake_claim(db, **kw):
        calls["claim"].append(kw)
        return _Occ()

    monkeypatch.setattr(ws, "_claim_occurrence", fake_claim)
    monkeypatch.setattr(ws, "_session_factory", lambda: SimpleNamespace(
        close=lambda: None, query=lambda m: _FakeAgentQuery()))
    monkeypatch.setattr(ws, "_run_workflow_agent",
                        lambda *a, **kw: calls["run"].append((a, kw)))
    monkeypatch.setattr(ws, "_complete_occurrence",
                        lambda db, occ, status: calls["complete"].append(
                            status))

    sched = ws.WorkflowScheduler(poll_interval=1)
    fired = sched.tick()
    assert fired == 1
    claim = calls["claim"][0]
    assert claim["occurrence_cls"] is ws.WorkflowOccurrence
    assert claim["subject_fields"] == {"agent_app_id": "wf1",
                                       "node_id": "s1"}
    assert claim["occurrence_key"] == "scheduled:2026-09-01T12:01:00+00:00"
    assert len(calls["run"]) == 1
    assert calls["complete"] == ["completed"]


def test_tick_skips_when_claim_lost(monkeypatch):
    """Claim returned None (another process won the slot) ⇒ never fires."""
    import app.services.workflow_scheduler as ws

    agents = [_agent("wf1", nodes=[_schedule_node("* * * * *")])]
    monkeypatch.setattr(ws, "find_due_scheduled_workflows",
                        lambda db, **kw: [(agents[0],
                                           agents[0].flow["nodes"][0],
                                           {"cron": "* * * * *"},
                                           datetime(2026, 9, 1, 12, 1, 0,
                                                    tzinfo=timezone.utc))])
    calls = {"run": 0}
    monkeypatch.setattr(ws, "_claim_occurrence", lambda db, **kw: None)
    monkeypatch.setattr(ws, "_session_factory", lambda: SimpleNamespace(
        close=lambda: None))
    monkeypatch.setattr(ws, "_run_workflow_agent",
                        lambda *a, **kw: calls.__setitem__("run",
                                                           calls["run"] + 1))
    monkeypatch.setattr(ws, "_complete_occurrence", lambda *a, **kw: None)

    sched = ws.WorkflowScheduler(poll_interval=1)
    assert sched.tick() == 0
    assert calls["run"] == 0  # exactly-once: the winner's process fires


def test_tick_run_failure_completes_failed_terminal(monkeypatch):
    import app.services.workflow_scheduler as ws

    agents = [_agent("wf1", nodes=[_schedule_node("* * * * *")])]
    monkeypatch.setattr(ws, "find_due_scheduled_workflows",
                        lambda db, **kw: [(agents[0],
                                           agents[0].flow["nodes"][0],
                                           {"cron": "* * * * *"},
                                           datetime(2026, 9, 1, 12, 1, 0,
                                                    tzinfo=timezone.utc))])
    statuses = []

    class _Occ:
        id = "occ-1"
        status = "claimed"

    monkeypatch.setattr(ws, "_claim_occurrence",
                        lambda db, **kw: _Occ())
    monkeypatch.setattr(ws, "_session_factory", lambda: SimpleNamespace(
        close=lambda: None, query=lambda m: SimpleNamespace(
            filter=lambda *a, **kw: SimpleNamespace(
                first=lambda: SimpleNamespace(
                    id="wf1", org_id="o", app_id="a", created_by_id="u1")))))

    def _boom(*a, **kw):
        raise RuntimeError("harness exploded")

    monkeypatch.setattr(ws, "_run_workflow_agent", _boom)
    monkeypatch.setattr(ws, "_complete_occurrence",
                        lambda db, occ, status: statuses.append(status))

    sched = ws.WorkflowScheduler(poll_interval=1)
    assert sched.tick() == 1  # the fire happened (and failed honestly)
    assert statuses == ["failed_terminal"]
