"""Workflow node type registry — the single source of truth for the canvas.

Modeled on n8n's INodeTypeDescription / INodeProperties schema. Every node
type declares:

- ``inputs`` / ``outputs``: typed connection ports (NodeConnectionType).
  Ports are either the plain type string ('main') or a dict with an explicit
  label, e.g. {"type": "ai_llm", "label": "Model"}. A node with BOTH a main
  and a supplemental port of the same direction exports two handles; the
  supplemental one is a "provider" port used to wire n8n-style AI
  dependencies (model / tool / KB) into an Agent node.
- ``properties``: the parameter schema that drives the auto-generated
  settings form (displayName/name/type/default/placeholder/required/options).

This registry is used by:
  1. GET /apps/{app_id}/workflow-node-types (frontend palette + form)
  2. validate_flow() (required config comes from the schema)
  3. the workflow runtime (executes nodes, resolves defaults)
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Connection types (mirrors n8n's NodeConnectionType subset we use)
# ---------------------------------------------------------------------------
CONNECTION_MAIN = "main"          # data-flow chain
CONNECTION_AI_LLM = "ai_llm"      # language model provider
CONNECTION_AI_TOOL = "ai_tool"    # tool / skill provider
CONNECTION_AI_KB = "ai_kb"        # knowledge base provider

# Supplemental (non-main) ports render as secondary handles in a distinct color
SUPPLEMENTAL_CONNECTION_TYPES = {CONNECTION_AI_LLM, CONNECTION_AI_TOOL, CONNECTION_AI_KB}

CONNECTION_TYPE_META = {
    CONNECTION_MAIN: {"label": "Main", "color": "#8b5cf6"},
    CONNECTION_AI_LLM: {"label": "Model", "color": "#10b981"},
    CONNECTION_AI_TOOL: {"label": "Tool", "color": "#f59e0b"},
    CONNECTION_AI_KB: {"label": "Knowledge Base", "color": "#06b6d4"},
}

# ---------------------------------------------------------------------------
# Property schema types (subset of n8n's NodePropertyTypes)
# ---------------------------------------------------------------------------
PROPERTY_TYPES = {
    "string", "number", "boolean", "options", "multiOptions",
    "json", "notice", "collection",
}


def port(type_: str, label: str | None = None) -> dict[str, str]:
    """Build a connection port descriptor."""
    return {"type": type_} if label is None else {"type": type_, "label": label}


def prop(
    display_name: str,
    name: str,
    type_: str,
    default: Any = None,
    *,
    placeholder: str | None = None,
    description: str | None = None,
    required: bool = False,
    options: list[dict] | None = None,
    item_label: str | None = None,
    load_options_method: str | None = None,
) -> dict[str, Any]:
    """Build a node property descriptor (n8n INodeProperties subset)."""
    p: dict[str, Any] = {
        "displayName": display_name,
        "name": name,
        "type": type_,
        "default": default,
    }
    if placeholder is not None:
        p["placeholder"] = placeholder
    if description is not None:
        p["description"] = description
    if required:
        p["required"] = True
    if options is not None:
        p["options"] = options
    if item_label is not None:
        p["itemLabel"] = item_label
    if load_options_method is not None:
        p["loadOptionsMethod"] = load_options_method
    return p


def notice(text: str) -> dict[str, Any]:
    return {"displayName": text, "name": "_notice", "type": "notice", "default": ""}


# ---------------------------------------------------------------------------
# Node type registry
# ---------------------------------------------------------------------------
NODE_TYPES: dict[str, dict[str, Any]] = {
    "trigger_chat": {
        "name": "trigger_chat",
        "displayName": "Chat Trigger",
        "category": "Triggers",
        "icon": "⚡",
        "color": "#8b5cf6",
        "description": "Fires when a user chats with this workflow",
        "description_zh": "当用户与该工作流对话时触发",
        "inputs": [],
        "outputs": ["main"],
        "properties": [
            notice("Fires on every user message. The message text is available as {{inputs.<node>.text}}. When run from the main chatbot, the recent conversation (last 10 turns) is available as {{inputs.<node>.history}}."),
        ],
    },
    "trigger_manual": {
        "name": "trigger_manual",
        "displayName": "Manual Trigger",
        "category": "Triggers",
        "icon": "▶",
        "color": "#8b5cf6",
        "description": "Fires when you run the workflow from the canvas (Test Run)",
        "description_zh": "从画布手动运行工作流时触发（测试运行）",
        "inputs": [],
        "outputs": ["main"],
        "properties": [
            notice("Fires when you click Run in the Test Run panel. The simulated input you type becomes {{inputs.<node>.text}}."),
        ],
    },
    "trigger_schedule": {
        "name": "trigger_schedule",
        "displayName": "Schedule Trigger",
        "category": "Triggers",
        "icon": "⏰",
        "color": "#8b5cf6",
        "description": "Fires on a cron schedule (published workflows only)",
        "description_zh": "按 cron 表达式定时触发（仅已发布工作流）",
        "inputs": [],
        "outputs": ["main"],
        "properties": [
            prop("Cron expression", "cron", "string", default="0 9 * * *", required=True,
                 placeholder="0 9 * * *",
                 description="5-field cron (minute hour day month weekday). The workflow must be published for the scheduler to fire it."),
            prop("Input", "input", "string", default="",
                 placeholder="Scheduled run",
                 description="Static input text passed to the workflow on each scheduled run."),
        ],
    },
    "trigger_webhook": {
        "name": "trigger_webhook",
        "displayName": "Webhook Trigger",
        "category": "Triggers",
        "icon": "🔗",
        "color": "#8b5cf6",
        "description": "Fires when an HTTP request hits your webhook URL",
        "description_zh": "当 HTTP 请求访问您的 Webhook 地址时触发",
        "inputs": [],
        "outputs": ["main"],
        "properties": [
            notice("POST /api/apps/{app_id}/workflows/{workflow_id}/webhook/{token} — the workflow's final output is returned as the HTTP response."),
            prop("Token", "token", "string", default="", required=True,
                 placeholder="generate-a-long-secret-token",
                 description="Secret token appended to the webhook URL. Keep it private — anyone with the URL can trigger the workflow."),
            prop("Method", "method", "options", default="POST",
                 options=[{"name": "POST", "value": "POST"}, {"name": "GET", "value": "GET"}]),
            prop("Input field", "input_field", "string", default="input",
                 description="JSON body field used as the workflow input (falls back to 'text'/'message', then the raw body)."),
        ],
    },
    "llm": {
        "name": "llm",
        "displayName": "LLM",
        "category": "AI",
        "icon": "◈",
        "color": "#10b981",
        "description": "Call the LLM with a prompt (supports {{inputs.<node>.<field>}})",
        "description_zh": "调用大模型（支持 {{inputs.<节点>.<字段>}} 模板）",
        "inputs": ["main"],
        # main = chain step; ai_llm = provider port for Agent nodes
        "outputs": ["main", port(CONNECTION_AI_LLM, "Model")],
        "properties": [
            prop("Prompt", "prompt", "string",
                 default="", required=True, placeholder="You are... {{inputs.n1.text}}",
                 description="Prompt template. Use {{inputs.<node>.<field>}} to reference upstream output."),
            prop("Model", "model", "string", default="automatic",
                 placeholder="automatic / deepseek-chat / ..."),
            prop("Temperature", "temperature", "number", default=0.3),
        ],
    },
    "agent": {
        "name": "agent",
        "displayName": "Agent",
        "category": "AI",
        "icon": "🤖",
        "color": "#f59e0b",
        "description": "Invoke an existing agent as a sub-agent",
        "description_zh": "调用已创建的智能体作为子智能体",
        "inputs": ["main", port(CONNECTION_AI_LLM, "Model"), port(CONNECTION_AI_TOOL, "Tool"), port(CONNECTION_AI_KB, "Knowledge Base")],
        "outputs": ["main"],
        "properties": [
            prop("Agent", "agent_id", "options", default="", required=True,
                 description="The agent to invoke. Loaded from your agents.",
                 options=[], load_options_method="listAgents"),
            prop("Input template", "input_template", "string", default="",
                 placeholder="{{inputs.n1.text}}",
                 description="Optional template for the sub-agent input. Defaults to the upstream text."),
        ],
    },
    "tool": {
        "name": "tool",
        "displayName": "Tool",
        "category": "AI",
        "icon": "🔧",
        "color": "#f59e0b",
        "description": "Run a Toolkit skill",
        "description_zh": "运行工具包技能",
        "inputs": ["main"],
        "outputs": ["main", port(CONNECTION_AI_TOOL, "Tool")],
        "properties": [
            prop("Tool", "tool_id", "options", default="", required=True,
                 description="The tool to run. Loaded from your toolkit.",
                 options=[], load_options_method="listTools"),
            prop("Input template", "input_template", "string", default="",
                 placeholder="{{inputs.n1.text}}"),
        ],
    },
    "kb_search": {
        "name": "kb_search",
        "displayName": "KB Search",
        "category": "AI",
        "icon": "📚",
        "color": "#06b6d4",
        "description": "Retrieve chunks from a knowledge base",
        "description_zh": "从知识库检索内容",
        "inputs": ["main"],
        "outputs": ["main", port(CONNECTION_AI_KB, "Knowledge Base")],
        "properties": [
            prop("Knowledge Base", "kb_id", "options", default="", required=True,
                 description="The knowledge base to search. Loaded from your connectors.",
                 options=[], load_options_method="listKnowledgeBases"),
            prop("Query template", "query", "string", default="",
                 placeholder="{{inputs.n1.text}}",
                 description="Template for the search query."),
            prop("Top K", "top_k", "number", default=5),
        ],
    },
    "db_query": {
        "name": "db_query",
        "displayName": "Database Query",
        "category": "Data",
        "icon": "🗄️",
        "color": "#3b82f6",
        "description": "Run SQL (or a natural-language question) against a connected database",
        "description_zh": "对已连接的数据库执行 SQL（或自然语言查询）",
        "inputs": ["main"],
        "outputs": ["main"],
        "properties": [
            prop("Connector", "kb_id", "options", default="", required=True,
                 description="The database connector to query. Loaded from your connectors (database-type only).",
                 options=[], load_options_method="listDatabases"),
            prop("Mode", "mode", "options", default="sql",
                 options=[
                     {"name": "SQL", "value": "sql"},
                     {"name": "Natural language", "value": "nl"},
                 ],
                 description="SQL runs the statement directly. Natural language translates the question with the connector's schema context."),
            prop("SQL", "sql", "string", default="",
                 placeholder="SELECT * FROM orders WHERE status = 'open' LIMIT 10",
                 description="Read-only SQL statement. Supports {{inputs.<node>.<field>}} templates."),
            prop("Question", "question", "string", default="",
                 placeholder="{{inputs.n1.text}} — e.g. total revenue by month",
                 description="Natural-language question about the connected database."),
            prop("Limit", "limit", "number", default=50,
                 description="Maximum rows returned."),
        ],
    },
    "mcp_tool": {
        "name": "mcp_tool",
        "displayName": "MCP Tool",
        "category": "Data",
        "icon": "🔌",
        "color": "#8b5cf6",
        "description": "Call a tool on a connected MCP server (Feishu, Drive, GitHub, …)",
        "description_zh": "调用已连接 MCP 服务器上的工具（飞书、云盘、GitHub 等）",
        "inputs": ["main"],
        "outputs": ["main"],
        "properties": [
            prop("MCP Server", "server_id", "options", default="", required=True,
                 description="The connected MCP server to call. Loaded from your MCP connections.",
                 options=[], load_options_method="listMcpServers"),
            prop("Tool", "tool_name", "options", default="", required=True,
                 description="Tool exposed by the selected MCP server.",
                 options=[], load_options_method="listMcpTools"),
            prop("Arguments (JSON)", "arguments", "json", default="",
                 placeholder='{"input": "{{inputs.n1.text}}"}',
                 description="JSON arguments passed to the tool. Supports {{inputs.<node>.<field>}} templates. Defaults to {\"input\": <upstream text>}."),
        ],
    },
    "http_request": {
        "name": "http_request",
        "displayName": "HTTP Request",
        "category": "Data",
        "icon": "🌐",
        "color": "#0ea5e9",
        "description": "Call a REST API",
        "description_zh": "调用 REST API",
        "inputs": ["main"],
        "outputs": ["main"],
        "properties": [
            prop("Method", "method", "options", default="GET",
                 options=[{"name": n, "value": n} for n in ("GET", "POST", "PUT", "PATCH", "DELETE")]),
            prop("URL", "url", "string", default="", required=True,
                 placeholder="https://api.example.com/items?q={{inputs.n1.items[0].text}}"),
            prop("Headers", "headers", "collection", default=[],
                 options=[prop("Key", "key", "string"), prop("Value", "value", "string")],
                 item_label="Header"),
            prop("Body (JSON)", "body", "json", default="",
                 placeholder='{"q": "{{inputs.n1.text}}"}'),
            prop("Auth", "auth_type", "options", default="none",
                 options=[{"name": "None", "value": "none"}, {"name": "Bearer token", "value": "bearer"}]),
            prop("Bearer token", "bearer_token", "string", default="",
                 placeholder="{{inputs.n1.text}}"),
            prop("Timeout (s)", "timeout", "number", default=15),
            prop("Response format", "response_format", "options", default="auto",
                 options=[{"name": "Auto", "value": "auto"}, {"name": "JSON", "value": "json"}, {"name": "Text", "value": "text"}]),
        ],
    },
    "code": {
        "name": "code",
        "displayName": "Code",
        "category": "Data",
        "icon": "⌨️",
        "color": "#10b981",
        "description": "Run Python to transform the input item",
        "description_zh": "运行 Python 转换输入数据",
        "inputs": ["main"],
        "outputs": ["main"],
        "properties": [
            prop("Language", "language", "options", default="python",
                 options=[{"name": "Python", "value": "python"}]),
            prop("Code", "code", "string", default="", required=True,
                 placeholder='output = {"name": input.get("name"), "total": input.get("price", 0) * 2}'),
            prop("Timeout (s)", "timeout", "number", default=10),
        ],
    },
    "set_fields": {
        "name": "set_fields",
        "displayName": "Set Fields",
        "category": "Data",
        "icon": "✏️",
        "color": "#f59e0b",
        "description": "Add or overwrite fields on each item",
        "description_zh": "为每个数据项添加或覆盖字段",
        "inputs": ["main"],
        "outputs": ["main"],
        "properties": [
            prop("Fields", "fields", "collection", default=[],
                 options=[prop("Name", "name", "string"), prop("Value (template)", "value", "string")],
                 item_label="Field"),
        ],
    },
    "split_out": {
        "name": "split_out",
        "displayName": "Split Out",
        "category": "Data",
        "icon": "✂️",
        "color": "#f59e0b",
        "description": "Expand an array field into one item per element",
        "description_zh": "将数组字段拆分为多条数据",
        "inputs": ["main"],
        "outputs": ["main"],
        "properties": [
            prop("Field", "field", "string", default="items",
                 placeholder="{{inputs.n1.items[0].tags}}"),
            prop("Item field name", "item_field", "string", default="item"),
        ],
    },
    "remove_duplicates": {
        "name": "remove_duplicates",
        "displayName": "Remove Duplicates",
        "category": "Data",
        "icon": "🧹",
        "color": "#f59e0b",
        "description": "Remove items with duplicate values for a key",
        "description_zh": "按字段去重",
        "inputs": ["main"],
        "outputs": ["main"],
        "properties": [
            prop("Key field", "field", "string", default="id"),
        ],
    },
    "merge": {
        "name": "merge",
        "displayName": "Merge",
        "category": "Data",
        "icon": "🔗",
        "color": "#f59e0b",
        "description": "Combine items from multiple inputs into one list",
        "description_zh": "将多个输入的数据项合并为一个列表",
        "inputs": ["main", "main"],
        "outputs": ["main"],
        "properties": [
            notice("Appends every item from ALL connected inputs, in order. Use it to combine parallel branches into one list before a final LLM or Output node. References upstream items as {{inputs.<node>.items}}."),
        ],
    },
    "condition": {
        "name": "condition",
        "displayName": "Condition",
        "category": "Logic",
        "icon": "🔀",
        "color": "#f43f5e",
        "description": "Route to the true or false branch based on upstream output",
        "description_zh": "根据上游输出路由到真/假分支",
        "inputs": ["main"],
        # True branch = port 0 (source-main), False branch = port 1 (source-main-1)
        "outputs": [port("main", "True"), port("main", "False")],
        "properties": [
            prop("Field", "field", "string", default="", required=True,
                 placeholder="{{inputs.n1.text}}",
                 description="Value to evaluate (usually an upstream reference)."),
            prop("Operator", "op", "options", default="eq",
                 options=[
                     {"name": "equals", "value": "eq"},
                     {"name": "not equals", "value": "ne"},
                     {"name": "greater than", "value": "gt"},
                     {"name": "less than", "value": "lt"},
                     {"name": "contains", "value": "contains"},
                     {"name": "is empty", "value": "empty"},
                 ]),
            prop("Value", "value", "string", default="",
                 placeholder="Comparison value (leave empty for is-empty)"),
            prop("On true → outcome label", "on_true", "string", default="true",
                 description="Label shown on the matching outgoing edge."),
            prop("On false → outcome label", "on_false", "string", default="false"),
        ],
    },
    "switch": {
        "name": "switch",
        "displayName": "Switch",
        "category": "Logic",
        "icon": "🔀",
        "color": "#f43f5e",
        "description": "Route to the first matching case (n8n Switch) — up to 3 cases + default",
        "description_zh": "路由到第一个匹配的分支（n8n Switch）— 最多 3 个条件 + 默认",
        "inputs": ["main"],
        # Case ports 0..2 (source-main, source-main-1, source-main-2), default = port 3
        "outputs": [
            port("main", "Case 1"), port("main", "Case 2"),
            port("main", "Case 3"), port("main", "Default"),
        ],
        "properties": [
            prop("Cases", "cases", "collection", default=[],
                 item_label="Case",
                 description="Evaluate in order; the first matching case routes the flow.",
                 options=[
                     prop("Field", "field", "string", default="", required=True,
                          placeholder="{{inputs.n1.text}}"),
                     prop("Operator", "op", "options", default="eq",
                          options=[
                              {"name": "equals", "value": "eq"},
                              {"name": "not equals", "value": "ne"},
                              {"name": "greater than", "value": "gt"},
                              {"name": "less than", "value": "lt"},
                              {"name": "contains", "value": "contains"},
                              {"name": "is empty", "value": "empty"},
                          ]),
                     prop("Value", "value", "string", default=""),
                 ]),
            prop("Default outcome label", "fallback_label", "string", default="default",
                 description="Label shown on the default outgoing edge (no case matched)."),
        ],
    },
    "output": {
        "name": "output",
        "displayName": "Output",
        "category": "Output",
        "icon": "➤",
        "color": "#64748b",
        "description": "Final answer returned to the user",
        "description_zh": "返回给用户的最终答案",
        "inputs": ["main"],
        "outputs": [],
        "properties": [
            prop("Format", "format", "options", default="markdown",
                 options=[
                     {"name": "markdown", "value": "markdown"},
                     {"name": "text", "value": "text"},
                     {"name": "json", "value": "json"},
                 ]),
        ],
    },
}


def get_node_type(node_type: str) -> dict[str, Any] | None:
    return NODE_TYPES.get(node_type)


def is_trigger_type(node_type: str) -> bool:
    """Any node that starts a workflow (n8n: trigger nodes)."""
    return node_type in NODE_TYPES and node_type.startswith("trigger_")


def is_router_type(node_type: str) -> bool:
    """Nodes whose output port selection determines which branch executes."""
    return node_type in ("condition", "switch")


# ---------------------------------------------------------------------------
# Universal error-handling properties (n8n "On Error" settings)
# ---------------------------------------------------------------------------
# Appended to EVERY node type's property list by list_node_types() — no
# per-node registry edits needed. The executor reads these from
# node.data.config (missing keys fall back to defaults).
ERROR_HANDLING_PROPS = [
    prop("Retry on fail", "retryOnFail", "number", default=0,
         description="Number of times to retry the node after a failure before it is considered failed. 0 = no retry."),
    prop("Continue on fail", "continueOnFail", "boolean", default=False,
         description="When enabled, a failed node emits an error item and the workflow continues to the next node instead of aborting (n8n 'Continue On Fail')."),
]


def list_node_types() -> list[dict[str, Any]]:
    """Registry as a JSON-serializable list (ordered by category), with the
    universal error-handling properties (retryOnFail / continueOnFail)
    appended to every node type — n8n's per-node 'On Error' settings.

    Returns COPIES: the shared NODE_TYPES registry is never mutated.
    """
    order = {"Triggers": 0, "AI": 1, "Logic": 2, "Data": 3, "Output": 4}
    out = []
    for t in sorted(
        NODE_TYPES.values(),
        key=lambda t: (order.get(t["category"], 9), t["displayName"]),
    ):
        entry = dict(t)
        entry["properties"] = list(t["properties"]) + list(ERROR_HANDLING_PROPS)
        out.append(entry)
    return out


def required_config_fields(node_type: str) -> list[str]:
    """Names of required (non-empty) config fields — drives validate_flow."""
    t = get_node_type(node_type)
    if not t:
        return []
    return [p["name"] for p in t["properties"]
            if p.get("required") and p["type"] in ("string", "options", "number")]
