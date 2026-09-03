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

import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from app.database import SessionLocal  # noqa: F401  (kept for callers)
from app.models.agent_app import AgentApp
from app.models.workflow_run import WorkflowRun, WorkflowRunStep
from app.services import llm_service
from app.services.workflow_executor import execute_flow_graph

logger = logging.getLogger(__name__)


def _resolve_entity(db, model, value: str, scope_col=None, scope_value=None):
    """Resolve a config identifier to a live row — by id first, then by name.

    The node forms historically stored the option's NAME into config fields
    that the runtime resolves by ID (agent_id/tool_id/kb_id/server_id), so a
    flow saved from the UI could carry a name and fail with "<x> not found".
    New flows store ids (frontend fixed); this fallback keeps already-saved
    flows working. Name lookups require a unique live match — ambiguous names
    still raise so we never silently pick the wrong entity.

    ``scope_col``/``scope_value`` (optional): when both are provided, the NAME
    lookup is additionally filtered to rows owned by ``scope_value`` (e.g.
    ``model.created_by_id == user.id``) so a display name never resolves to an
    entity the current user doesn't own. Id lookups are never scoped — ids are
    globally unique primary keys. Existing runtime call sites (run_agent/
    run_tool/kb_search/db_query/mcp_call) do not pass scope params and keep
    the original unscoped behaviour.
    """
    if not value:
        return None
    row = db.query(model).filter(
        model.id == value, model.is_deleted.is_(False),
    ).first()
    if row is not None:
        return row
    if hasattr(model, "name"):
        q = db.query(model).filter(
            model.name == value, model.is_deleted.is_(False),
        )
        if scope_col is not None and scope_value is not None:
            q = q.filter(scope_col == scope_value)
        matches = q.all()
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise RuntimeError(
                f"identifier {value!r} is ambiguous ({len(matches)} rows with that name)"
            )
    return None


# Config keys in node data.config that reference entities by id at runtime.
# workflow_builder may emit display NAMEs instead of ids (the prompt says the
# system resolves names), so before persisting we walk the graph and swap
# names for the canonical row id — same id-first/unique-name semantics the
# runtime itself uses via _resolve_entity.
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def resolve_entity_references(flow: dict, db, user=None) -> list[str]:
    """Walk node configs and resolve display-NAME references to ids in place.

    Handles kb_id/agent_id/tool_id/server_id config keys (the entity types the
    runtime resolves by id). Uses _resolve_entity (id-first, unique-name
    fallback, raises on ambiguity). Already-id values (UUID-shaped) and
    unresolvable names are left untouched — the runtime reports not-found at
    execution time. Returns a list of resolution notes; notes prefixed with
    "ERROR: " indicate a fatal ambiguity the caller should surface as a
    validation failure instead of persisting a wrong graph.

    ``user`` (optional): when provided and not an admin, NAME lookups are
    scoped to entities the user owns (``created_by_id == user.id``) so a name
    matching only ANOTHER user's entity does not resolve — it is left
    untouched, consistent with unknown names. All entity models handled here
    (AgentApp, Tool, KnowledgeBase, McpServer) inherit ``created_by_id`` from
    TimestampedBase; a model without a usable scope column falls back to the
    unscoped lookup.
    """
    from app.models.agent_app import AgentApp
    from app.models.knowledge_base import KnowledgeBase
    from app.models.mcp_server import McpServer
    from app.models.tool import Tool

    key_model = {
        "kb_id": KnowledgeBase,
        "agent_id": AgentApp,
        "tool_id": Tool,
        "server_id": McpServer,
    }
    user_id = getattr(user, "id", None)
    is_admin = getattr(user, "role", None) == "admin"
    scope_value = user_id if (user_id and not is_admin) else None
    notes: list[str] = []
    for node in flow.get("nodes") or []:
        node_id = node.get("id")
        config = (node.get("data") or {}).get("config") or {}
        for key, model in key_model.items():
            value = config.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            value = value.strip()
            if _UUID_RE.match(value):
                continue  # already an id — runtime resolves it directly
            # Scope name→id resolution to the user's own entities so a
            # display name never resolves to a row the user doesn't own.
            scope_col = getattr(model, "created_by_id", None) if scope_value else None
            try:
                row = _resolve_entity(
                    db, model, value,
                    scope_col=scope_col, scope_value=scope_value,
                )
            except RuntimeError as exc:  # ambiguity — never pick one silently
                notes.append(f"ERROR: node {node_id}: {exc}")
                continue
            if row is not None and row.id != value:
                config[key] = row.id
                notes.append(
                    f"node {node_id}: resolved {key} {value!r} -> {row.id}"
                )
    return notes


def _kb_datasource_config(kb) -> dict | None:
    """Map a KnowledgeBase connector to the governed nl2sql adapter config.

    The nl2sql pipeline builds its adapter from a ``datasource_config``
    dict; the MySQL adapter wraps a KnowledgeBase directly (same
    MySQLConnector the workflow runtime uses), Postgres/SQLite map to
    connection params. Returns None when the connector type is
    unsupported (caller falls back to the legacy translator).
    """
    dialect = (kb.db_type or "").lower()
    if dialect.startswith("mysql"):
        return {"dialect": "mysql", "kb": kb}
    if dialect.startswith("postgres"):
        return {
            "dialect": "postgres",
            "host": kb.host or "localhost",
            "port": int(kb.port or 5432),
            "database": kb.database_name or "",
            "username": kb.username or "",
            "password": kb.password or "",
        }
    if dialect.startswith("sqlite"):
        path = kb.file_url or kb.database_name or ""
        if not path:
            return None
        return {"dialect": "sqlite", "path": path}
    return None


def save_flow_from_decision_summary(payload: dict, workflow_id: str, db, user) -> dict:
    """Validate + persist a workflow graph emitted by workflow_builder.

    Returns {"status": "flow_saved", "flow": ...},
    {"status": "flow_invalid", "errors": [...]}, or
    {"status": "flow_save_failed", "errors": [...]} when the target workflow
    row is missing / not owned (or persistence raised) — the caller emits the
    typed flow_save_failed SSE event instead of crashing the stream.

    Entity display-NAME references in node configs (kb_id/agent_id/tool_id/
    server_id) are resolved to ids in place before saving — scoped to the
    user's own entities for non-admin users; ambiguous names are treated as
    validation errors (repair loop — the agent sees them next turn and can
    disambiguate).
    """
    from fastapi import HTTPException

    from app.config import settings
    from app.routers.workflows import save_workflow, SaveBody

    name = (payload.get("name") or "Untitled Workflow").strip()
    description = (payload.get("description") or "").strip()
    flow = payload.get("flow")
    if not isinstance(flow, dict) or not isinstance(flow.get("nodes"), list):
        return {"status": "flow_invalid", "errors": ["flow.nodes must be a list"]}

    errors = validate_flow(flow)
    if errors:
        return {"status": "flow_invalid", "errors": errors}

    # The builder agent often omits edge ids — assign stable ones (e1..eN)
    # so canvas edits (delete/reconnect) have identities to work with.
    for i, edge in enumerate(flow.get("edges") or [], start=1):
        if not edge.get("id"):
            edge["id"] = f"e{i}"

    notes = resolve_entity_references(flow, db, user)
    ref_errors = [n for n in notes if n.startswith("ERROR: ")]
    if ref_errors:
        return {"status": "flow_invalid", "errors": ref_errors}

    try:
        save_workflow(
            workflow_id,
            SaveBody(name=name, description=description, flow=flow),
            settings.DEFAULT_APP_ID,
            db=db,
            user=user,
        )
    except HTTPException as exc:
        # Stale/foreign workflow: the owning row is missing, soft-deleted, or
        # belongs to another user (save_workflow raises 404). Convert to a
        # flow_save_failed-style result so the v3 stream emits the typed SSE
        # event instead of aborting mid-stream.
        logger.warning(
            "save_flow: workflow %s not savable (HTTP %s): %s",
            workflow_id, exc.status_code, exc.detail,
        )
        db.rollback()
        return {
            "status": "flow_save_failed",
            "errors": ["workflow not found or not owned"],
        }
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "save_flow: unexpected error persisting workflow %s: %s",
            workflow_id, exc,
        )
        db.rollback()
        return {
            "status": "flow_save_failed",
            "errors": [f"failed to save workflow: {exc}"],
        }
    return {"status": "flow_saved", "flow": flow}


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
    """Binds node-handler dependencies to real services and persists runs.

    ``ephemeral=True`` (single-node test / dry-run contexts): every handler
    dependency binding is available exactly as in a persisted run, but no
    WorkflowRun/WorkflowRunStep row is ever created or committed and no run
    events (run_started / step / run_finished) are emitted — record_step and
    finish_run become no-ops and ``run`` stays None (``run_id`` is still set
    to a fresh uuid4 so downstream code that only needs an identifier keeps
    working).
    """

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
        ephemeral: bool = False,
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
        self.ephemeral = ephemeral
        if ephemeral:
            # No row persistence and no run lifecycle events at all.
            self.run = None
            return
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
        if self.ephemeral:
            return
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
        if self.ephemeral or self.run is None:
            return
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
        agent = _resolve_entity(self.db, AgentApp, agent_id)
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
        tool = _resolve_entity(self.db, Tool, tool_id)
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
        kb = _resolve_entity(self.db, KnowledgeBase, kb_id)
        if kb is None:
            raise RuntimeError(f"knowledge base {kb_id!r} not found")
        collection = getattr(kb, "collection", None) or rag_retriever.DEFAULT_COLLECTION
        chunks = rag_retriever.retrieve(query, n_results=top_k, collection=collection)
        return [{"text": c.text, "source": c.source, "score": c.score} for c in chunks]

    def db_query(self, kb_id: str, query: str, mode: str = "sql", limit: int = 50) -> dict:
        """Run SQL (or translate a NL question) against a database connector.

        NL mode first routes through the GOVERNED nl2sql pipeline
        (``app.services.nl2sql.ask`` — semantic resolution, M-Schema
        context, validated + retried SQL generation, policy, audit; the
        same service the agent tool layer uses). If the pipeline can't
        run for this connector (unsupported dialect, no adapter), it
        falls back to the legacy schema-summary translator. SQL mode is
        always deterministic and never touches the translator.
        """
        from app.models.knowledge_base import KnowledgeBase
        from app.services.db.connector_factory import get_connector

        kb = _resolve_entity(self.db, KnowledgeBase, kb_id)
        if kb is None:
            raise RuntimeError(f"database connector {kb_id!r} not found")
        if not kb.db_type:
            raise RuntimeError(f"connector {kb.name!r} is not a database connector")

        if mode == "nl":
            governed = self._nl2sql_governed(kb, query, limit)
            if governed is not None:
                return governed

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

    def _nl2sql_governed(self, kb, question: str, limit: int) -> dict | None:
        """Route a workflow NL query through the governed nl2sql pipeline.

        Returns the db_query-shaped result dict, or None when the
        pipeline cannot run (unsupported dialect / adapter failure /
        generation failure) so the caller falls back to the legacy
        translator. Never raises into the served path.
        """
        try:
            from app.services.nl2sql import ask as nl2sql_ask

            config = _kb_datasource_config(kb)
            if config is None:
                return None
            # audit_events.binding_id is varchar(36) — the synthetic id
            # MUST fit or the audit flush fails and poisons the shared
            # SQLAlchemy session (workflow then dies with "transaction
            # has been rolled back"). Compact form: wf:<agent8>:<q-hash16>
            q_hash = hashlib.sha1(str(question).encode("utf-8")).hexdigest()[:16]
            binding_id = f"wf:{str(self.agent.id)[:8]}:{q_hash}"
            res = nl2sql_ask(
                question,
                binding_id=binding_id,
                db=self.db,
                datasource_config=config,
            )
            if not res.success or res.data is None:
                logger.warning(
                    "db_query nl: governed nl2sql failed (%s) — falling back",
                    res.error or "no data",
                )
                return None
            rows = [
                {col: val for col, val in zip(res.data.columns, row)}
                for row in res.data.rows
            ][:max(limit, 1)]
            if not rows:
                text = "0 rows returned."
            else:
                text = "\n".join(
                    json.dumps(r, ensure_ascii=False, default=str) for r in rows
                )
            return {"text": text, "rows": rows, "sql": res.sql, "engine": "nl2sql"}
        except Exception as exc:  # noqa: BLE001 — fallback, never crash the node
            logger.warning("db_query nl: governed nl2sql unavailable (%s) — falling back", exc)
            return None

    def _translate_to_sql(self, conn, question: str) -> str:
        """NL → SQL using the connector's own schema (no hardcoded tables)."""
        # Include the full business schema, not just the first 25 tables —
        # large ERP warehouses (60+ tables) push their sales/order views past
        # a small cap and NL questions then fail with "table not found".
        # Skip framework tables (auth_*/django_*/_ref_*) so the prompt stays
        # focused on business data and the LLM doesn't aggregate over users.
        _SYSTEM_PREFIXES = ("auth_", "django_", "_ref_", "access_", "alert_")
        tables = [
            t for t in conn.list_tables()[:100]
            if not t.lower().startswith(_SYSTEM_PREFIXES)
        ][:60]
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
            "Prefer denormalized business views (names like *_v_* / erp_v_*) "
            "for aggregations when the schema offers them. Return ONLY the SQL "
            "statement (no markdown fences, no explanation). "
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

        server = _resolve_entity(self.db, McpServer, server_id)
        if server is None:
            raise RuntimeError(f"MCP server {server_id!r} not found")
        return call_mcp_tool(server.server_url, tool_name, arguments)

    def run_code(self, code: str, item: dict, timeout: int = 10) -> dict:
        """Run user Python in the Docker sandbox (agent-sandbox pattern).

        Enqueues a ``workflow_code`` SandboxJob via the sandbox-worker and
        returns its captured result. When the sandbox path is unavailable
        (dev / Docker down) it falls back to the restricted in-process exec
        — same semantics as the sandbox runner (restricted builtins,
        ``input`` = upstream item, must assign ``output``), no isolation.
        """
        from app.services.workflow_sandbox import run_code_sandboxed

        # db may be absent on bare contexts (semantics-only tests); the
        # sandbox service falls back to the in-process restricted exec.
        return run_code_sandboxed(getattr(self, "db", None), code, item, timeout)

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
    error_wf = _resolve_entity(db, AgentApp, error_wf_id)
    if error_wf is None or not error_wf.flow_mode:
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
