"""Workflow runtime — ExecutionContext + run orchestrator.

ExecutionContext implements the ctx contract required by workflow_nodes
against real Zhanlu services:

  - llm_complete  -> sync text completion via llm_service provider plumbing
  - run_agent     -> MVP: target AgentApp's prompt sections + input, one
                     LLM completion (NO sub-agent tool calling yet — the
                     full agent loop integration is a documented follow-up)
  - run_tool      -> MVP: Tool.skill_md is used as system context and the
                     LLM follows it (skills are instructions; the agent-loop
                     sandbox execution is a documented follow-up)
  - kb_search     -> rag_retriever.retrieve against the KB's collection

run_workflow_agent() orchestrates: validate -> persist WorkflowRun ->
execute_flow_graph -> persist per-node WorkflowRunStep -> final status.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from app.database import SessionLocal  # noqa: F401  (kept for callers)
from app.models.agent_app import AgentApp
from app.models.workflow_run import WorkflowRun, WorkflowRunStep
from app.services import llm_service
from app.services.workflow_executor import execute_flow_graph
from app.services.workflow_nodes import NODE_HANDLERS
from app.services.workflow_validation import validate_flow


def _sync_text_completion(prompt: str, model: str | None = None) -> str:
    """Minimal sync text completion through the configured LLM providers.

    Mirrors llm_service.chat_completion_json_sync but returns free text.
    """
    model = model or llm_service.get_model()
    body = {
        "prompt": prompt,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
    }
    payload, _, _ = llm_service.build_llm_payload(body, model)
    for provider in llm_service.get_llm_providers():
        request_payload = dict(payload)
        request_payload["model"] = provider.model
        try:
            with httpx.Client(timeout=60.0) as client:
                resp = client.post(
                    llm_service._provider_chat_url(provider),
                    headers=llm_service._provider_headers(provider),
                    json=request_payload,
                )
                resp.raise_for_status()
            data = resp.json()
            choices = data.get("choices") or []
            if choices:
                content = (choices[0].get("message") or {}).get("content")
                if content:
                    return content
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:  # noqa: BLE001
            continue
    raise RuntimeError("llm_complete: all providers failed")


class WorkflowExecutionContext:
    """Binds node-handler dependencies to real services and persists runs."""

    def __init__(
        self,
        db,
        org_id: str,
        app_id: str,
        agent: AgentApp,
        trigger_input: str,
        created_by_id: str | None = None,
        on_event: Callable[[dict], None] | None = None,
    ):
        self.db = db
        self.org_id = org_id
        self.app_id = app_id
        self.agent = agent
        self.trigger_input = trigger_input
        self.created_by_id = created_by_id
        self.on_event = on_event or (lambda ev: None)
        self.outputs: dict[str, Any] = {}
        self.run_id = str(uuid.uuid4())
        self.run = WorkflowRun(
            id=self.run_id,
            agent_app_id=agent.id,
            trigger="chat",
            status="running",
            created_by_id=created_by_id,
            org_id=org_id,
            app_id=app_id,
            started_at=datetime.now(timezone.utc),
        )
        db.add(self.run)
        db.commit()
        self.on_event({"type": "run_started", "run_id": self.run_id})

    # ---- persistence / streaming ----

    def record_step(self, node_id: str, status: str, output: dict | None = None,
                    error: str | None = None) -> None:
        node_type = ""
        for n in (self.agent.flow or {}).get("nodes", []) if isinstance(self.agent.flow, dict) else []:
            if n.get("id") == node_id:
                node_type = n.get("type", "")
                break
        step = WorkflowRunStep(
            run_id=self.run_id,
            node_id=node_id,
            node_type=node_type,
            status=status,
            output_json=output,
            error=error,
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc) if status in ("completed", "failed") else None,
        )
        self.db.add(step)
        self.db.commit()
        self.on_event({
            "type": "step",
            "run_id": self.run_id,
            "node_id": node_id,
            "node_type": node_type,
            "status": status,
            "output": output,
            "error": error,
        })

    def finish_run(self, status: str, error: str | None = None, output: Any = None) -> None:
        self.run.status = status
        self.run.error = error
        self.run.finished_at = datetime.now(timezone.utc)
        self.db.commit()
        self.on_event({"type": "run_finished", "run_id": self.run_id,
                       "status": status, "error": error, "output": output})

    # ---- dependency bindings ----

    def llm_complete(self, prompt: str, model: str | None = None) -> str:
        return _sync_text_completion(prompt, model)

    def run_agent(self, agent_id: str, input_text: str) -> dict:
        agent = self.db.query(AgentApp).filter(
            AgentApp.id == agent_id,
            AgentApp.is_deleted.is_(False),
        ).first()
        if agent is None:
            raise RuntimeError(f"sub-agent {agent_id!r} not found")
        parts = [p for p in [
            agent.prompt_identity, agent.prompt_boundary,
            agent.prompt_reasoning, agent.prompt_tools, agent.prompt_output,
        ] if p]
        system = "\n\n".join(parts) or "You are a helpful assistant."
        text = self.llm_complete(f"{system}\n\n{input_text}", model=agent.model)
        return {"text": text}

    def run_tool(self, tool_id: str, input_text: str) -> dict:
        from app.models.tool import Tool
        tool = self.db.query(Tool).filter(
            Tool.id == tool_id, Tool.is_deleted.is_(False),
        ).first()
        if tool is None:
            raise RuntimeError(f"tool {tool_id!r} not found")
        if not tool.skill_md:
            return {"text": f"[tool {tool.name} has no instructions]"}
        # MVP: LLM follows the skill instructions. Sandbox execution of
        # custom tools is a documented follow-up.
        text = self.llm_complete(
            f"Follow these tool instructions exactly:\n{tool.skill_md}\n\nInput:\n{input_text}"
        )
        return {"text": text}

    def kb_search(self, kb_id: str, query: str, top_k: int = 5) -> list[dict]:
        from app.models.knowledge_base import KnowledgeBase
        from app.services.rag import rag_retriever
        kb = self.db.query(KnowledgeBase).filter(
            KnowledgeBase.id == kb_id, KnowledgeBase.is_deleted.is_(False),
        ).first()
        collection = getattr(kb, "collection", None) or rag_retriever.DEFAULT_COLLECTION
        chunks = rag_retriever.retrieve(query, n_results=top_k, collection=collection)
        return [{"text": c.text, "source": c.source, "score": c.score} for c in chunks]


def _flow_dict(flow: Any) -> dict:
    """agent_apps.flow is a JSON column typed list|None — our format is a dict."""
    return flow if isinstance(flow, dict) else {}


def run_workflow_agent(
    db,
    org_id: str,
    app_id: str,
    agent: AgentApp,
    trigger_input: str,
    created_by_id: str | None = None,
    on_event: Callable[[dict], None] | None = None,
) -> dict[str, Any]:
    """Validate + execute a flow_mode agent. Returns {status, error, output}."""
    flow = _flow_dict(agent.flow)
    errors = validate_flow(flow)
    if errors:
        return {"status": "failed", "error": "; ".join(errors), "output": None}
    ctx = WorkflowExecutionContext(
        db, org_id, app_id, agent, trigger_input,
        created_by_id=created_by_id, on_event=on_event,
    )
    try:
        result = execute_flow_graph(flow, ctx, NODE_HANDLERS)
        ctx.finish_run(result["status"], error=result["error"], output=result["output"])
        result["run_id"] = ctx.run_id
        return result
    except Exception as exc:  # noqa: BLE001
        ctx.finish_run("failed", error=str(exc))
        return {"status": "failed", "error": str(exc), "output": None, "run_id": ctx.run_id}
