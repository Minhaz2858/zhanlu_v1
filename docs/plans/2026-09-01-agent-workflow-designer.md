# Agent Workflow Designer — Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Ship an n8n-style canvas workflow designer where users build AI agent workflows, publish them, and chat with them from the main chatbot "+" dialog.

**Architecture:** Workflow agents are normal `AgentApp` rows (`flow_mode=true`, graph JSON in `flow`). A new Python executor (`workflow_executor.py`) runs the DAG: topological sort, per-node handlers registered in a dispatch table, streaming step events. Frontend uses `@xyflow/react` for the canvas. Chat integration rides the existing `?agent=` binding + v3 streaming endpoint.

**Tech Stack:** Backend — Python/FastAPI/SQLAlchemy (existing). Frontend — React/Vite/Tailwind + @xyflow/react (new dep, MIT). Tests — pytest (backend), vitest + Playwright (frontend).

**Design doc:** `docs/superpowers/specs/2026-09-01-agent-workflow-designer-design.md` (approved).

---

## Phase 0 — Backend (executor + router + migration + tests)

### Task 0.1: Migration — workflow_runs + workflow_run_steps

**Objective:** Two new tables to record workflow executions and per-node step results.

**Files:**
- Create: `backend/alembic/versions/083_workflow_runs.py`

**Step 1:** Create migration file modeled on `backend/alembic/versions/082_chat_shares.py`:

```python
"""workflow_runs + workflow_run_steps

Revision ID: 083
Revises: 082
"""
from alembic import op
import sqlalchemy as sa

revision = "083"
down_revision = "082"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("agent_app_id", sa.String(36), nullable=False, index=True),
        sa.Column("trigger", sa.String(50), nullable=False, server_default="chat"),
        sa.Column("status", sa.String(20), nullable=False, server_default="running"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_by_id", sa.String(36), nullable=True),
    )
    op.create_table(
        "workflow_run_steps",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False, index=True),
        sa.Column("node_id", sa.String(64), nullable=False),
        sa.Column("node_type", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("output_json", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )

def downgrade() -> None:
    op.drop_table("workflow_run_steps")
    op.drop_table("workflow_runs")
```

**Step 2:** Check the actual head revision first: `ls backend/alembic/versions/ | tail -3` — adjust `down_revision` to the real head (not necessarily "082").

**Step 3:** Create matching SQLAlchemy models:

**Files:**
- Create: `backend/app/models/workflow_run.py`
- Modify: `backend/app/models/__init__.py` (export `WorkflowRun`, `WorkflowRunStep`)

```python
from datetime import datetime
from sqlalchemy import JSON, Column, DateTime, Integer, String, Text
from app.models.base import Base

class WorkflowRun(Base):
    __tablename__ = "workflow_runs"
    id = Column(String(36), primary_key=True)
    agent_app_id = Column(String(36), nullable=False, index=True)
    trigger = Column(String(50), nullable=False, server_default="chat")
    status = Column(String(20), nullable=False, server_default="running")
    error = Column(Text, nullable=True)
    started_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    created_by_id = Column(String(36), nullable=True)

class WorkflowRunStep(Base):
    __tablename__ = "workflow_run_steps"
    id = Column(String(36), primary_key=True)
    run_id = Column(String(36), nullable=False, index=True)
    node_id = Column(String(64), nullable=False)
    node_type = Column(String(50), nullable=False)
    status = Column(String(20), nullable=False, server_default="pending")
    latency_ms = Column(Integer, nullable=True)
    output_json = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
```

**Step 4:** Run migration: `docker compose -p zhanlu_sep_1_n8n exec backend alembic upgrade head` (from repo root) — Expected: `Running upgrade 082 -> 083`.

**Step 5:** Verify: `docker exec zhanlu-postgres psql -U zhanlu -d zhanlu -c "\d workflow_runs"` shows the table.

**Step 6:** Commit: `git add backend/alembic/versions/083_workflow_runs.py backend/app/models/workflow_run.py backend/app/models/__init__.py && git commit -m "feat(workflows): workflow_runs + workflow_run_steps tables"`

### Task 0.2: Flow validation service

**Objective:** `validate_flow(flow)` returns structured errors; used by the save/validate endpoints AND the executor pre-check.

**Files:**
- Create: `backend/app/services/workflow_validation.py`

**Step 1: Write failing test**

- Create: `backend/tests/test_workflow_validation.py`

```python
from app.services.workflow_validation import validate_flow

def test_valid_linear_flow():
    flow = {
        "version": 1,
        "nodes": [
            {"id": "n1", "type": "trigger_chat", "position": {"x": 0, "y": 0}, "data": {"label": "Chat"}},
            {"id": "n2", "type": "llm", "position": {"x": 1, "y": 0}, "data": {"label": "LLM", "config": {"prompt": "x"}}},
            {"id": "n3", "type": "output", "position": {"x": 2, "y": 0}, "data": {"label": "Out"}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2"},
            {"id": "e2", "source": "n2", "target": "n3"},
        ],
    }
    errors = validate_flow(flow)
    assert errors == []

def test_cycle_detected():
    flow = {
        "version": 1,
        "nodes": [
            {"id": "n1", "type": "trigger_chat", "position": {"x": 0, "y": 0}, "data": {"label": "A"}},
            {"id": "n2", "type": "condition", "position": {"x": 1, "y": 0}, "data": {"label": "B", "config": {"branches": []}}},
            {"id": "n3", "type": "output", "position": {"x": 2, "y": 0}, "data": {"label": "C"}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2"},
            {"id": "e2", "source": "n2", "target": "n3"},
            {"id": "e3", "source": "n3", "target": "n2"},
        ],
    }
    errors = validate_flow(flow)
    assert any("cycle" in e.lower() for e in errors)

def test_dangling_edge_rejected():
    flow = {"version": 1, "nodes": [{"id": "n1", "type": "trigger_chat", "position": {"x": 0, "y": 0}, "data": {"label": "A"}}],
            "edges": [{"id": "e1", "source": "n1", "target": "n_missing"}]}
    assert any("dangling" in e.lower() for e in validate_flow(flow))

def test_missing_required_config():
    flow = {"version": 1, "nodes": [
        {"id": "n1", "type": "trigger_chat", "position": {"x": 0, "y": 0}, "data": {"label": "A"}},
        {"id": "n2", "type": "llm", "position": {"x": 1, "y": 0}, "data": {"label": "B", "config": {}}},
        {"id": "n3", "type": "output", "position": {"x": 2, "y": 0}, "data": {"label": "C"}},
    ], "edges": []}
    assert any("llm" in e.lower() and "prompt" in e.lower() for e in validate_flow(flow))

def test_no_trigger_and_no_output():
    flow = {"version": 1, "nodes": [
        {"id": "n2", "type": "llm", "position": {"x": 1, "y": 0}, "data": {"label": "B", "config": {"prompt": "x"}}},
    ], "edges": []}
    errs = validate_flow(flow)
    assert any("trigger" in e.lower() for e in errs)
    assert any("output" in e.lower() for e in errs)
```

**Step 2:** Run: `cd backend && python -m pytest tests/test_workflow_validation.py -v` — Expected: FAIL (module not found).

**Step 3:** Implement `backend/app/services/workflow_validation.py`:

```python
"""Flow graph validation. Pure functions — no DB, no side effects."""
from typing import Any

NODE_TYPES = {"trigger_chat", "llm", "agent", "tool", "kb_search", "condition", "output"}

# config fields that MUST be present (non-empty) per node type
REQUIRED_CONFIG = {
    "llm": ["prompt"],
    "agent": ["agent_id"],
    "tool": ["tool_id"],
    "kb_search": ["kb_id"],
    "condition": ["branches"],
    "trigger_chat": [],
    "output": [],
}


def validate_flow(flow: dict[str, Any]) -> list[str]:
    """Return a list of human-readable errors (empty == valid)."""
    errors: list[str] = []
    if not isinstance(flow, dict) or "nodes" not in flow or "edges" not in flow:
        return ["flow must contain 'nodes' and 'edges'"]
    nodes = flow.get("nodes") or []
    edges = flow.get("edges") or []
    node_ids = set()
    for n in nodes:
        nid = n.get("id")
        if not nid:
            errors.append("node missing id")
            continue
        node_ids.add(nid)
        ntype = n.get("type")
        if ntype not in NODE_TYPES:
            errors.append(f"node {nid}: unknown type {ntype!r}")
            continue
        config = (n.get("data") or {}).get("config") or {}
        for field in REQUIRED_CONFIG.get(ntype, []):
            if not config.get(field):
                errors.append(f"node {nid} ({ntype}): missing required config '{field}'")
    # dangling edges
    for e in edges:
        if e.get("source") not in node_ids or e.get("target") not in node_ids:
            errors.append(f"edge {e.get('id')}: dangling (source/target not found)")
    # cycle detection (Kahn)
    adj = {nid: [] for nid in node_ids}
    indeg = {nid: 0 for nid in node_ids}
    for e in edges:
        if e["source"] in adj and e["target"] in adj:
            adj[e["source"]].append(e["target"])
            indeg[e["target"]] += 1
    queue = [nid for nid, d in indeg.items() if d == 0]
    seen = 0
    while queue:
        cur = queue.pop()
        seen += 1
        for nxt in adj[cur]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    if seen != len(node_ids):
        errors.append("cycle detected in workflow graph")
    # structural requirements
    types = {n.get("id"): n.get("type") for n in nodes}
    if sum(1 for t in types.values() if t == "trigger_chat") != 1:
        errors.append("workflow must have exactly one trigger_chat node")
    if "output" not in types.values():
        errors.append("workflow must have at least one output node")
    return errors
```

**Step 4:** Run tests — Expected: 6 passed. Fix any drift.

**Step 5:** Commit: `git commit -am "feat(workflows): flow validation service"`

### Task 0.3: Executor core — topological runner

**Objective:** `WorkflowExecutor` that topologically sorts the graph and runs nodes through a handler dispatch table, collecting per-node results.

**Files:**
- Create: `backend/app/services/workflow_executor.py`

**Step 1: Write failing test** — `backend/tests/test_workflow_executor.py`:

```python
import pytest
from app.services.workflow_executor import execute_flow_graph

class FakeContext:
    """Minimal ctx double: node handlers read inputs from upstream outputs."""
    def __init__(self, trigger_input):
        self.trigger_input = trigger_input
        self.outputs = {}
        self.step_events = []

def fake_llm_handler(ctx, node):
    upstream = ctx.outputs.get("n1", {})
    return {"text": f"llm({upstream.get('text', '')})"}

def fake_output_handler(ctx, node):
    upstream = ctx.outputs.get("n2", {})
    return {"text": upstream.get("text", "")}

NODE_HANDLERS = {"llm": fake_llm_handler, "output": fake_output_handler}

def test_executes_in_topological_order():
    flow = {"nodes": [
        {"id": "n1", "type": "trigger_chat", "position": {"x": 0, "y": 0}, "data": {"label": "T", "config": {}}},
        {"id": "n2", "type": "llm", "position": {"x": 1, "y": 0}, "data": {"label": "L", "config": {"prompt": "x"}}},
        {"id": "n3", "type": "output", "position": {"x": 2, "y": 0}, "data": {"label": "O", "config": {}}},
    ], "edges": [
        {"id": "e1", "source": "n1", "target": "n2"},
        {"id": "e2", "source": "n2", "target": "n3"},
    ]}
    ctx = FakeContext("hello")
    result = execute_flow_graph(flow, ctx, NODE_HANDLERS)
    assert result["output"] == "llm(hello)"

def test_node_failure_routed_to_output():
    flow = {"nodes": [
        {"id": "n1", "type": "trigger_chat", "position": {"x": 0, "y": 0}, "data": {"label": "T", "config": {}}},
        {"id": "n2", "type": "llm", "position": {"x": 1, "y": 0}, "data": {"label": "L", "config": {"prompt": "x"}}},
        {"id": "n3", "type": "output", "position": {"x": 2, "y": 0}, "data": {"label": "O", "config": {}}},
    ], "edges": [
        {"id": "e1", "source": "n1", "target": "n2"},
        {"id": "e2", "source": "n2", "target": "n3"},
    ]}
    def boom(ctx, node):
        raise RuntimeError("kaboom")
    handlers = {"llm": boom, "output": fake_output_handler}
    ctx = FakeContext("hello")
    result = execute_flow_graph(flow, ctx, handlers)
    assert result["status"] == "failed"
    assert "kaboom" in result["error"]
```

**Step 2:** Run — Expected: FAIL.

**Step 3:** Implement `execute_flow_graph` in `workflow_executor.py`:

```python
"""Workflow DAG runner.

Pure runner: takes a flow graph + a handler dispatch table + a context
object, executes nodes in topological order, and returns the final output.
Node handlers are registered by the caller (real handlers live in
workflow_nodes.py). The runner is testable with fake handlers.
"""
from typing import Any, Callable


def _topo_order(nodes: list[dict], edges: list[dict]) -> list[dict]:
    node_ids = [n["id"] for n in nodes]
    adj = {nid: [] for nid in node_ids}
    indeg = {nid: 0 for nid in node_ids}
    for e in edges:
        if e["source"] in adj and e["target"] in adj:
            adj[e["source"]].append(e["target"])
            indeg[e["target"]] += 1
    queue = [nid for nid in node_ids if indeg[nid] == 0]
    ordered = []
    while queue:
        cur = queue.pop(0)
        ordered.append(next(n for n in nodes if n["id"] == cur))
        for nxt in adj[cur]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    return ordered


def execute_flow_graph(flow: dict, ctx: Any,
                       handlers: dict[str, Callable]) -> dict[str, Any]:
    """Run a flow graph.

    ctx must expose: .trigger_input, .outputs (dict node_id -> output),
    .step_events (list, appendable), and .record_step(node_id, status, ...)
    Real contexts add DB/LLM/KB wiring; fakes just record.
    """
    nodes = flow.get("nodes") or []
    edges = flow.get("edges") or []
    ordered = _topo_order(nodes, edges)
    final_output = None
    for node in ordered:
        nid = node["id"]
        ntype = node["type"]
        handler = handlers.get(ntype)
        if handler is None:
            err = f"node {nid}: no handler for type {ntype!r}"
            ctx.record_step(nid, "failed", error=err)
            return {"status": "failed", "error": err, "output": None}
        try:
            if hasattr(ctx, "record_step"):
                ctx.record_step(nid, "running")
            out = handler(ctx, node)
            ctx.outputs[nid] = out
            if hasattr(ctx, "record_step"):
                ctx.record_step(nid, "completed", output=out)
            if ntype == "output":
                final_output = out.get("text") or out.get("json") or str(out)
        except Exception as exc:  # noqa: BLE001
            err = f"node {nid} ({ntype}) failed: {exc}"
            if hasattr(ctx, "record_step"):
                ctx.record_step(nid, "failed", error=err)
            return {"status": "failed", "error": err, "output": None}
    return {"status": "completed", "error": None, "output": final_output}
```

**Step 4:** Run tests — Expected: 2 passed.

**Step 5:** Commit: `git commit -am "feat(workflows): DAG runner with topological execution"`

### Task 0.4: Real node handlers

**Objective:** Implement the 7 MVP handlers. Each handler is a function `(ctx, node) -> dict`.

**Files:**
- Create: `backend/app/services/workflow_nodes.py`

**Step 1: Write failing tests** — `backend/tests/test_workflow_nodes.py`. For LLM/agent/tool/kb handlers, inject fake dependencies via the context (ctx carries `llm_complete`, `run_agent`, `run_tool`, `kb_search` callables) so tests need no DB/network:

```python
import pytest
from app.services.workflow_nodes import handle_llm, handle_condition, handle_trigger, handle_output

class FakeCtx:
    def __init__(self, **deps):
        self.deps = deps
        self.outputs = {}
        self.trigger_input = "hello"
    def llm_complete(self, prompt, model=None, **kw):
        return self.deps.get("llm_text", "llm-ok")
    def kb_search(self, kb_id, query, top_k=5):
        return self.deps.get("kb_results", [{"text": "doc1"}])

def _node(nid, ntype, config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0},
            "data": {"label": nid, "config": config}}

def test_trigger_passes_message():
    ctx = FakeCtx()
    out = handle_trigger(ctx, _node("n1", "trigger_chat", {}))
    assert out == {"text": "hello"}

def test_llm_uses_prompt_template_and_upstream():
    ctx = FakeCtx(llm_text="llm-ok")
    ctx.outputs["n1"] = {"text": "the question"}
    node = _node("n2", "llm", {"prompt": "Answer: {{inputs.n1.text}}"})
    out = handle_llm(ctx, node)
    assert out["text"] == "llm-ok"

def test_condition_routes_by_upstream_value():
    ctx = FakeCtx()
    ctx.outputs["n1"] = {"text": "5"}
    node = _node("n3", "condition", {"branches": [
        {"op": "gt", "value": "3", "on_true": "pass"},
        {"op": "default", "on_true": "fail"},
    ]})
    out = handle_condition(ctx, node)
    assert out == {"branch": "pass"}

def test_output_formats_markdown():
    ctx = FakeCtx()
    ctx.outputs["n1"] = {"text": "# Title"}
    out = handle_output(ctx, _node("n4", "output", {"format": "markdown"}))
    assert out["text"] == "# Title"
```

**Step 2:** Run — Expected: FAIL.

**Step 3:** Implement `workflow_nodes.py` (template rendering, condition eval, real call wiring):

```python
"""MVP node handlers. Each handler: (ctx, node) -> dict output.

ctx must expose the dependency callables the handler needs:
  - ctx.trigger_input            (str) chat message for trigger_chat
  - ctx.llm_complete(prompt, ...) (str) LLM completion
  - ctx.run_agent(agent_id, input_text) -> dict  (sub-agent invocation)
  - ctx.run_tool(tool_id, input_text) -> dict
  - ctx.kb_search(kb_id, query, top_k) -> list[dict]
  - ctx.outputs                   dict node_id -> output (upstream results)
The real ExecutionContext (workflow_executor.py Task 0.5) binds these to
the existing services; tests inject fakes.
"""
import re
from typing import Any


def _render(template: str, ctx: Any) -> str:
    """{{inputs.<nodeId>.<field>}} substitution — minimal, safe."""
    def repl(m):
        path = m.group(1).strip()
        parts = [p for p in path.split(".") if p]
        if len(parts) >= 3 and parts[0] == "inputs":
            node_id, field = parts[1], parts[2]
            val = ctx.outputs.get(node_id, {}).get(field, "")
            return str(val)
        return m.group(0)
    return re.sub(r"\{\{\s*([^}]+)\s*\}\}", repl, template)


def handle_trigger(ctx, node):
    return {"text": ctx.trigger_input}


def handle_llm(ctx, node):
    config = node["data"].get("config") or {}
    prompt = _render(config.get("prompt", ""), ctx)
    text = ctx.llm_complete(prompt, model=config.get("model") or "automatic")
    return {"text": text}


def handle_agent(ctx, node):
    config = node["data"].get("config") or {}
    agent_id = config["agent_id"]
    input_text = _render(config.get("input_template", "{{inputs}}"), ctx)
    result = ctx.run_agent(agent_id, input_text)
    return {"text": str(result.get("text", result))}


def handle_tool(ctx, node):
    config = node["data"].get("config") or {}
    tool_id = config["tool_id"]
    input_text = _render(config.get("input_template", "{{inputs}}"), ctx)
    result = ctx.run_tool(tool_id, input_text)
    return {"text": str(result.get("text", result))}


def handle_kb_search(ctx, node):
    config = node["data"].get("config") or {}
    query = _render(config.get("query", "{{inputs}}"), ctx)
    docs = ctx.kb_search(config["kb_id"], query, top_k=int(config.get("top_k", 5)))
    return {"text": "\n\n".join(d.get("text", "") for d in docs), "docs": docs}


def _eval_op(value: str, op: str, target: str) -> bool:
    try:
        if op == "gt": return float(value) > float(target)
        if op == "lt": return float(value) < float(target)
        if op == "eq": return value == target
        if op == "contains": return target in value
    except (TypeError, ValueError):
        return False
    return False


def handle_condition(ctx, node):
    config = node["data"].get("config") or {}
    upstream = next(iter(ctx.outputs.values()), {})
    value = str(upstream.get("text", ""))
    for branch in config.get("branches", []):
        if branch.get("op") == "default" or _eval_op(value, branch.get("op", "eq"), str(branch.get("value", ""))):
            return {"branch": branch.get("on_true", "pass")}
    return {"branch": "fail"}


def handle_output(ctx, node):
    config = node["data"].get("config") or {}
    fmt = config.get("format", "markdown")
    upstream = next(iter(ctx.outputs.values()), {})
    text = upstream.get("text", "")
    if fmt == "json":
        import json as _json
        text = _json.dumps(upstream, ensure_ascii=False)
    return {"text": text, "format": fmt}


NODE_HANDLERS = {
    "trigger_chat": handle_trigger,
    "llm": handle_llm,
    "agent": handle_agent,
    "tool": handle_tool,
    "kb_search": handle_kb_search,
    "condition": handle_condition,
    "output": handle_output,
}
```

**Step 4:** Run tests — Expected: 4 passed.

**Step 5:** Commit: `git commit -am "feat(workflows): 7 MVP node handlers"`

### Task 0.5: ExecutionContext — bind handlers to real services

**Objective:** `WorkflowExecutionContext` implements the ctx contract against real services: LLM via `app.services.llm_service`, sub-agents via the agent invocation pipeline, tools via `agent_loop.tool_executor`, KB via the KB search service. It persists `WorkflowRun` + `WorkflowRunStep` rows and appends streaming events to a callback.

**Files:**
- Create: `backend/app/services/workflow_runtime.py` (or extend `workflow_executor.py`)

**Step 1:** Read the real service signatures first (adjust bindings):
- `grep -n "def " backend/app/services/llm_service.py | head` — find the completion entry (e.g. `complete(prompt, model=...)`)
- `grep -n "def " backend/app/services/agent_loop/tool_executor.py | head`
- `grep -rn "def search\|def query" backend/app/services/*.py | grep -i kb | head`
- For sub-agent invocation: `grep -n "def run_agent\|async def run_agent" backend/app/services/agent_invocations.py`

**Step 2: Write failing test** — `backend/tests/test_workflow_runtime.py` with monkeypatched service calls; assert run + step rows are persisted and the callback receives events in order.

**Step 3:** Implement the context. Core shape:

```python
class WorkflowExecutionContext:
    def __init__(self, db, org_id, app_id, agent, trigger_input,
                 created_by_id=None, on_event=None):
        self.db = db
        self.org_id = org_id
        self.app_id = app_id
        self.agent = agent
        self.trigger_input = trigger_input
        self.created_by_id = created_by_id
        self.on_event = on_event or (lambda ev: None)
        self.outputs = {}
        self.run_id = uuid4hex()
        self.run = WorkflowRun(id=self.run_id, agent_app_id=agent.id, trigger="chat",
                               status="running", created_by_id=created_by_id,
                               started_at=datetime.utcnow())
        db.add(self.run); db.commit()

    def record_step(self, node_id, status, output=None, error=None):
        step = WorkflowRunStep(id=uuid4hex(), run_id=self.run_id, node_id=node_id,
                               node_type="", status=status, output_json=output,
                               error=error, started_at=..., finished_at=...)
        self.db.add(step); self.db.commit()
        self.on_event({"type": "step", "run_id": self.run_id, "node_id": node_id,
                       "status": status, "output": output, "error": error})

    def llm_complete(self, prompt, model=None): ...   # llm_service
    def run_agent(self, agent_id, input_text): ...    # agent invocation pipeline
    def run_tool(self, tool_id, input_text): ...      # tool_executor
    def kb_search(self, kb_id, query, top_k=5): ...   # KB service
```

**Step 4:** Run tests — Expected: pass.

**Step 5:** Commit: `git commit -am "feat(workflows): execution context bound to real services"`

### Task 0.6: Workflows router

**Objective:** REST endpoints under `/api/apps/{app_id}/workflows` (auth-protected like `app/routers/agents.py`).

**Files:**
- Create: `backend/app/routers/workflows.py`
- Modify: `backend/main.py` (import + include_router)

**Step 1: Write failing tests** — `backend/tests/routers/test_workflows_router.py` (use the same TestClient + auth fixture pattern as `backend/tests/routers/` peers; read one first: `ls backend/tests/routers/` and copy the auth header pattern).

Endpoints (contract):

```text
GET    /api/apps/{app_id}/workflows                  → list flow_mode agents (id, name, description, status, updated_date)
GET    /api/apps/{app_id}/workflows/{id}             → agent incl. flow graph
PUT    /api/apps/{app_id}/workflows/{id}             → save graph (validates; 422 with errors if invalid)
POST   /api/apps/{app_id}/workflows/{id}/validate    → {valid: bool, errors: [...]}
POST   /api/apps/{app_id}/workflows/{id}/run         → test run; body {input: str}; returns {run_id, status, output}
GET    /api/apps/{app_id}/workflows/{id}/runs        → run history (latest 50)
POST   /api/apps/{app_id}/workflows/{id}/publish     → status draft → published (validates first)
```

**Step 2:** Implement router:

```python
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.agent_app import AgentApp
from app.routers.auth import get_current_user  # verify actual helper name in auth.py
from app.services.workflow_validation import validate_flow
from app.services.workflow_runtime import run_workflow_agent  # helper: validate→execute→persist

router = APIRouter(prefix="/api/apps/{app_id}/workflows", tags=["workflows"])

@router.get("")
def list_workflows(app_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return db.query(AgentApp).filter(AgentApp.app_id == app_id,
                                     AgentApp.flow_mode.is_(True),
                                     AgentApp.is_deleted.is_(False)).all()

# ... get / save (validate before commit) / validate / run / runs / publish
```

**Step 3:** Register in `main.py`:

```python
from app.routers.workflows import router as workflows_router
app.include_router(workflows_router)
```

**Step 4:** Run tests — Expected: pass. Verify manually:
`curl -s http://localhost:5002/healthz` still ok; login → list workflows returns `[]`.

**Step 5:** Commit: `git commit -am "feat(workflows): workflows router (list/get/save/validate/run/runs/publish)"`

---

## Phase 1 — Frontend canvas

### Task 1.1: Install xyflow + workflow API client

**Files:**
- Modify: `frontend/package.json` (dep `@xyflow/react`)
- Create: `frontend/src/api/workflows.js`

**Steps:**
1. `cd frontend && npm install @xyflow/react --registry https://registry.npmjs.org`
2. Create `api/workflows.js` mirroring `api/dashboards.js` patterns (authFetch-based):
   - `listWorkflows()` → GET `/api/apps/{app}/workflows`
   - `getWorkflow(id)`, `saveWorkflow(id, graph)`, `validateWorkflow(id)`,
     `runWorkflow(id, input)`, `listWorkflowRuns(id)`, `publishWorkflow(id)`
3. Commit.

### Task 1.2: My Space — Agent Workflow tab

**Files:**
- Modify: `frontend/src/pages/MySpace.jsx`

**Steps:**
1. Add tab `{ key: 'workflow', label: t.mySpace.tabs.workflow || 'Agent Workflow', icon: Workflow, entity: 'AgentApp' }` to the `TABS` array (Workflow icon already imported in Sidebar — add import in MySpace).
2. In the tab's load branch, query `listWorkflows()` (API client) and render a card grid: name, description, status chip (draft/published), updated date, "Open in Canvas" → `/my-space/workflow/:id`, "New Workflow" → POST AgentApp with `flow_mode: true, flow: {version:1, nodes:[], edges:[]}` then navigate to canvas.
3. Reuse the existing card/list styling from the agent tab section of MySpace (copy the agent card grid classes).
4. Verify in browser: tab appears, empty state shows, New Workflow creates and navigates.
5. Commit.

### Task 1.3: Canvas page — static layout

**Files:**
- Create: `frontend/src/pages/WorkflowCanvas.jsx`
- Create: `frontend/src/components/workflow/WorkflowCanvas.jsx` (the xyflow wrapper)
- Create: `frontend/src/components/workflow/NodePalette.jsx`
- Create: `frontend/src/components/workflow/NodeSettingsDrawer.jsx`
- Create: `frontend/src/components/workflow/workflowNodeTypes.jsx`
- Modify: `frontend/src/App.jsx` (route `/my-space/workflow/:id`)

**Steps:**
1. Add route in App.jsx desktopRoutes: `<Route path="/my-space/workflow/:id" element={<WorkflowCanvas />} />` (before the `:type` catch-all).
2. `workflowNodeTypes.jsx`: define the 7 custom node components with n8n-style headers (colored per group: Triggers=green, AI=violet, Logic=amber, Output=blue), source/target handles (`<Handle type="target" position={Position.Left} />`, source right), label + config summary line.
3. `WorkflowCanvas.jsx`: `<ReactFlow nodes={nodes} edges={edges} onNodesChange onEdgesChange onConnect nodeTypes={workflowNodeTypes} fitView miniMap />` — convert the agent's `flow` JSON to xyflow nodes/edges on load, convert back on save.
4. `NodePalette.jsx`: left rail listing the 7 node types grouped, drag-to-canvas via `useReactFlow().screenToFlowPosition` + `onDrop` (copy the standard xyflow drag-drop example), click-to-add.
5. `NodeSettingsDrawer.jsx`: right drawer; when a node is selected, render the per-type config form (llm: prompt textarea + model select + temperature; agent: AgentApp picker; tool: tool picker; kb: KB picker + top_k; condition: branch rows builder; output: format select).
6. Top bar in `WorkflowCanvas.jsx` page: name (inline editable), status chip, Validate / Save / Publish / Run buttons wired to `api/workflows.js`; validation errors shown as red toast + per-node red border.
7. Verify in browser: palette → drag nodes → connect → configure → save → reload keeps graph.
8. Commit.

### Task 1.4: Run drawer (test run)

**Files:**
- Create: `frontend/src/components/workflow/RunDrawer.jsx`
- Modify: `frontend/src/components/workflow/WorkflowCanvas.jsx`

**Steps:**
1. Bottom drawer: input box (mock trigger text) + Run button → `runWorkflow(id, {input})`.
2. Render returned run history + live steps (poll `listWorkflowRuns` every 2s while running; MVP: after run completes, show steps with status/latency/output preview).
3. Verify: run a 3-node linear flow from the drawer, see step rows.
4. Commit.

---

## Phase 2 — Chat integration

### Task 2.1: New Task dialog — Workflow Agents group

**Files:**
- Modify: `frontend/src/components/CreateResourceDialog.jsx` (read it first; it's the "+" dialog)

**Steps:**
1. Add a "Workflow Agents" group listing published `flow_mode` agents (via `api/workflows.js` list, filter `status === 'published'`).
2. Selecting one → `navigate('/?agent=' + id)` (existing binding) — same as the agent group.
3. Verify in browser: "+" button → dialog shows Workflow Agents → click → chat opens with agent chip.
4. Commit.

### Task 2.2: Chat send path — flow_mode branch

**Files:**
- Modify: `frontend/src/pages/Chat.jsx` (around the `handleSend` payload build, ~line 1327-1440)
- Modify: `backend/app/routers/agents.py` (v3 stream handler — when target agent `flow_mode=true`, call `run_workflow_agent(...)` and stream its events instead of the normal agent loop; exact integration point: find where the conversation's agent is resolved in the v3 messages/stream handler)

**Steps:**
1. Backend: in the v3 stream handler, after resolving the target AgentApp, branch: if `agent.flow_mode` → create `WorkflowExecutionContext`, `execute_flow_graph`, emit SSE events (`step` events + final message) using the existing SSE builder helpers (`app/services/agent_loop/sse_builders.py`).
2. Frontend: `handleSend` — when `effectiveAgent.flow_mode` is true, still send via the same endpoint (no frontend branch needed for the wire protocol); add a small "flow" chip on the agent chip to signal workflow mode. Ensure the existing step/event rendering surfaces the step events (extend the message card renderer with a collapsible workflow step card if the events carry `type === 'step'` and `run_id`).
3. E2E verify: publish a workflow → chat → select it in "+" → send message → streamed reply + step card.
4. Commit.

---

## Phase 3 — Polish

### Task 3.1: Validation UX + publish gate
Red border + error text on invalid nodes (already sketched in 1.3); Publish disabled with tooltip listing errors. Test: try publishing an incomplete flow → blocked.

### Task 3.2: i18n
Add `workflow.*` keys to the zh/en language bundles (`frontend/src/lib/LanguageProvider` / locale files) for tab label, palette groups, node names, drawer labels, buttons.

### Task 3.3: Undo/redo + minimap + keyboard shortcuts (Ctrl+S save, Delete node)
Use xyflow `useOnSelectionChange`/`applyEdgeChanges` history stack — small util `lib/workflowHistory.js` (undo/redo states on save points).

---

## Verification (full MVP)

1. Backend: `cd backend && python -m pytest tests/test_workflow_validation.py tests/test_workflow_executor.py tests/test_workflow_nodes.py tests/routers/test_workflows_router.py -v` — all green.
2. Migration: alembic upgrade head on a fresh DB, `\d workflow_runs` present.
3. Frontend: `cd frontend && npm run build` clean; vitest for new tests.
4. E2E (Playwright): create → canvas → nodes → connect → configure → save → publish → chat "+" → workflow agent → message → streamed answer with step card.
5. `make smoke` still green (no regressions).

## Notes / Pitfalls

- `backend/app/routers/agents.py` is a 15k-line monolith — do NOT touch its internals beyond the single flow_mode branch point in the v3 stream handler; keep the workflow code in separate files.
- Auth helper name: verify `get_current_user` vs actual in `app/routers/auth.py` before Task 0.6.
- xyflow React 19 compat: check `@xyflow/react` peer deps before install; if React 19 mismatch, pin the compatible version.
- Template rendering `{{inputs.<id>.<field>}}` is intentionally minimal — do not add Jinja.
- Editor test runs share the same executor as chat; only the trigger input differs.
