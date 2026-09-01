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

import json
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
        run_id: str | None = None,
        trigger: str = "chat",
        history: list | None = None,
    ):
        self.db = db
        self.org_id = org_id
        self.app_id = app_id
        self.agent = agent
        self.trigger_input = trigger_input
        self.trigger = trigger
        self.trigger_history = history or []
        self.created_by_id = created_by_id
        self.on_event = on_event or (lambda ev: None)
        self.outputs: dict[str, Any] = {}
        self.run_id = run_id or str(uuid.uuid4())
        self.run = WorkflowRun(
            id=self.run_id,
            agent_app_id=agent.id,
            trigger=trigger,
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

    def finish_run(self, status: str, error: str | None = None, output: Any = None,
                   extra: dict | None = None) -> None:
        self.run.status = status
        self.run.error = error
        self.run.finished_at = datetime.now(timezone.utc)
        self.db.commit()
        ev = {"type": "run_finished", "run_id": self.run_id,
              "status": status, "error": error, "output": output}
        if extra:
            ev.update(extra)
        self.on_event(ev)

    # ---- dependency bindings ----

    def llm_complete(self, prompt: str, model: str | None = None) -> str:
        return _sync_text_completion(prompt, model)

    def run_agent(self, agent_id: str, input_text: str, model: str | None = None,
                  extra_context: str | None = None) -> dict:
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
        if extra_context:
            system = f"{system}\n\n{extra_context}"
        text = self.llm_complete(f"{system}\n\n{input_text}", model=model or agent.model)
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

    def db_query(self, kb_id: str, query: str, mode: str = "sql", limit: int = 50) -> dict:
        """Run SQL (or translate a NL question) against a database connector.

        NL mode builds a schema summary from the connector's own metadata,
        has the LLM produce the SQL, then executes it — so both modes go
        through the same dialect-aware, read-only execute() path.
        """
        from app.models.knowledge_base import KnowledgeBase
        from app.services.db.connector_factory import get_connector

        kb = self.db.query(KnowledgeBase).filter(
            KnowledgeBase.id == kb_id, KnowledgeBase.is_deleted.is_(False),
        ).first()
        if kb is None:
            raise RuntimeError(f"database connector {kb_id!r} not found")
        if not kb.db_type:
            raise RuntimeError(f"connector {kb.name!r} is not a database connector")

        with get_connector(kb) as conn:
            sql = query.strip()
            if mode == "nl":
                sql = self._translate_to_sql(conn, query)
            rows = conn.execute(sql, max_rows=max(limit, 1), timeout_s=30)

        if not rows:
            text = "0 rows returned."
        else:
            text = "\n".join(
                json.dumps(r, ensure_ascii=False, default=str) for r in rows
            )
        return {"text": text, "rows": rows}

    def _translate_to_sql(self, conn, question: str) -> str:
        """NL → SQL using the connector's own schema (no hardcoded tables)."""
        tables = conn.list_tables()[:25]
        schema_parts = []
        for t in tables:
            try:
                cols = conn.describe_table(t)
            except Exception:  # noqa: BLE001
                continue
            schema_parts.append(f"{t}(" + ", ".join(c["name"] for c in cols[:40]) + ")")
        schema = "\n".join(schema_parts) or "(no tables accessible)"
        prompt = (
            "You translate natural-language questions into READ-ONLY SQL "
            f"for this database schema:\n{schema}\n\n"
            f"Question: {question}\n\n"
            "Return ONLY the SQL statement (no markdown fences, no explanation). "
            "Use LIMIT 100. Never write UPDATE/INSERT/DELETE/DDL."
        )
        sql = self.llm_complete(prompt).strip()
        sql = sql.removeprefix("```sql").removeprefix("```").removesuffix("```").strip()
        if not sql.lower().lstrip().startswith("select"):
            raise RuntimeError(f"NL query did not produce a SELECT: {sql[:120]!r}")
        return sql

    def mcp_call(self, server_id: str, tool_name: str, arguments: dict) -> dict:
        """Invoke a tool on a connected MCP server (stdio JSON-RPC)."""
        from app.models.mcp_server import McpServer
        from app.services.tool_handlers.mcp_proxy import call_mcp_tool

        server = self.db.query(McpServer).filter(
            McpServer.id == server_id, McpServer.is_deleted.is_(False),
        ).first()
        if server is None:
            raise RuntimeError(f"MCP server {server_id!r} not found")
        return call_mcp_tool(server.server_url, tool_name, arguments)

    def run_code(self, code: str, item: dict, timeout: int = 10) -> dict:
        """Run user Python in a restricted sandbox (thread + timeout).

        Available names: json, math, len, str, int, float, bool, list, dict,
        range, abs, min, max, sum, round, isinstance, print, and `input`
        (the upstream item dict). NO __import__ (import statements fail),
        no open/eval/exec, no filesystem/network. Script must assign `output`.
        """
        import threading

        safe_builtins = {
            "len": len, "str": str, "int": int, "float": float,
            "bool": bool, "list": list, "dict": dict, "range": range,
            "enumerate": enumerate, "zip": zip, "abs": abs, "min": min,
            "max": max, "sum": sum, "round": round, "isinstance": isinstance,
            "Exception": Exception, "ValueError": ValueError,
            "TypeError": TypeError, "KeyError": KeyError,
            "True": True, "False": False, "None": None, "print": print,
        }
        restricted = {
            "__builtins__": safe_builtins,
            "json": json,
            "math": __import__("math"),
            "input": item,
        }
        box: dict = {}
        def _run():
            try:
                exec(compile(code, "<code>", "exec"), restricted, box)
            except Exception as exc:  # noqa: BLE001
                box["_error"] = repr(exc)
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            return {"success": False, "text": f"Code timed out after {timeout}s"}
        if "_error" in box:
            return {"success": False, "text": f"Code error: {box['_error']}"}
        if "output" not in box:
            return {"success": False, "text": "Code must assign an `output` variable"}
        out = box["output"]
        try:
            json.dumps(out)
        except TypeError:
            return {"success": False, "text": "output is not JSON-serializable"}
        if isinstance(out, dict):
            return {"success": True, "text": json.dumps(out, ensure_ascii=False), "items": [out]}
        if isinstance(out, list):
            return {"success": True, "text": json.dumps(out, ensure_ascii=False), "items": out}
        return {"success": True, "text": str(out), "items": [{"value": out}]}

    def http_request(
        self,
        method: str,
        url: str,
        headers: list | None = None,
        body: str | None = None,
        timeout: int = 15,
        auth_type: str = "none",
        bearer_token: str = "",
        response_format: str = "auto",
    ) -> dict:
        """Call a REST API with httpx (sync — executor already runs in a worker)."""
        import httpx

        hdrs = {h["key"]: str(h["value"]) for h in (headers or []) if h.get("key")}
        if auth_type == "bearer" and bearer_token:
            hdrs["Authorization"] = f"Bearer {bearer_token}"
        data = None
        if method != "GET" and body and body.strip():
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                data = body
        try:
            resp = httpx.request(
                method, url, headers=hdrs,
                json=data if isinstance(data, dict) else None,
                content=data if isinstance(data, str) else None,
                timeout=timeout, follow_redirects=True,
            )
        except Exception as exc:  # noqa: BLE001
            return {"text": f"HTTP error: {exc}", "items": [], "status": 0, "success": False}
        text = resp.text[:1_000_000]
        items = None
        if response_format != "text" and resp.headers.get("content-type", "").startswith("application/json"):
            try:
                parsed = json.loads(text)
                items = parsed if isinstance(parsed, list) else [parsed]
            except json.JSONDecodeError:
                items = None
        if items is None:
            items = [{"body": text, "status": resp.status_code}]
        ok = 200 <= resp.status_code < 300
        return {"text": text, "items": items, "status": resp.status_code, "success": ok}


def _flow_dict(flow: Any) -> dict:
    """agent_apps.flow is a JSON column typed list|None — our format is a dict."""
    return flow if isinstance(flow, dict) else {}


def _run_error_workflow(db, org_id: str, app_id: str, agent: AgentApp,
                        result: dict, ctx: WorkflowExecutionContext,
                        created_by_id: str | None = None) -> None:
    """Execute the workflow's configured error workflow (n8n Error Workflow).

    The error workflow receives a JSON input describing the failure:
    {error, workflow_id, workflow_name, run_id, node_id, node_type, trigger}.
    Runs one level deep only — is_error_run=True blocks recursion, so a
    failing error workflow never spawns another error workflow.
    """
    import json as _json

    flow = _flow_dict(agent.flow)
    error_wf_id = (flow.get("settings") or {}).get("on_error_workflow_id")
    if not error_wf_id:
        return
    error_wf = db.query(AgentApp).filter(
        AgentApp.id == error_wf_id,
        AgentApp.flow_mode.is_(True),
        AgentApp.is_deleted.is_(False),
    ).first()
    if error_wf is None:
        return
    payload = _json.dumps({
        "error": result.get("error"),
        "workflow_id": agent.id,
        "workflow_name": agent.name,
        "run_id": ctx.run_id,
        "node_id": result.get("failed_node_id"),
        "node_type": result.get("failed_node_type"),
        "trigger": ctx.trigger,
    }, ensure_ascii=False)
    try:
        sub = run_workflow_agent(
            db, org_id, app_id, error_wf, payload,
            created_by_id=created_by_id,
            on_event=ctx.on_event,
            trigger="error",
            is_error_run=True,
        )
        result["error_workflow_run_id"] = sub.get("run_id")
        result["error_workflow_status"] = sub.get("status")
        if sub.get("error"):
            result["error_workflow_error"] = sub["error"]
    except Exception as exc:  # noqa: BLE001
        result["error_workflow_error"] = str(exc)


def run_workflow_agent(
    db,
    org_id: str,
    app_id: str,
    agent: AgentApp,
    trigger_input: str,
    created_by_id: str | None = None,
    on_event: Callable[[dict], None] | None = None,
    run_id: str | None = None,
    trigger: str = "chat",
    is_error_run: bool = False,
    history: list | None = None,
) -> dict[str, Any]:
    """Validate + execute a flow_mode agent. Returns {status, error, output}.

    On failure, if the workflow defines an error workflow
    (flow.settings.on_error_workflow_id), it is executed with the failure
    context as its input BEFORE the main run is marked finished — the live
    stream stays open so subscribers see the error workflow execute. Error
    runs never recurse (is_error_run guard, one level deep).
    """
    flow = _flow_dict(agent.flow)
    errors = validate_flow(flow)
    if errors:
        return {"status": "failed", "error": "; ".join(errors), "output": None}
    ctx = WorkflowExecutionContext(
        db, org_id, app_id, agent, trigger_input,
        created_by_id=created_by_id, on_event=on_event, run_id=run_id,
        trigger=trigger, history=history,
    )
    try:
        result = execute_flow_graph(flow, ctx, NODE_HANDLERS)
        if result["status"] == "failed" and not is_error_run:
            _run_error_workflow(db, org_id, app_id, agent, result, ctx,
                                created_by_id=created_by_id)
        extra = {}
        if result.get("error_workflow_run_id"):
            extra = {
                "error_workflow_run_id": result["error_workflow_run_id"],
                "error_workflow_status": result["error_workflow_status"],
            }
            if result.get("error_workflow_error"):
                extra["error_workflow_error"] = result["error_workflow_error"]
        ctx.finish_run(result["status"], error=result["error"],
                       output=result["output"], extra=extra)
        result["run_id"] = ctx.run_id
        return result
    except Exception as exc:  # noqa: BLE001
        ctx.finish_run("failed", error=str(exc))
        return {"status": "failed", "error": str(exc), "output": None, "run_id": ctx.run_id}
