"""MVP node handlers. Each handler: (ctx, node) -> dict output.

ctx must expose the dependency callables the handler needs:
  - ctx.trigger_input            (str) chat message for trigger_chat
  - ctx.llm_complete(prompt, ...) (str) LLM completion
  - ctx.run_agent(agent_id, input_text) -> dict  (sub-agent invocation)
  - ctx.run_tool(tool_id, input_text) -> dict
  - ctx.kb_search(kb_id, query, top_k) -> list[dict]
  - ctx.outputs                   dict node_id -> output (upstream results)
The real ExecutionContext (workflow_runtime.py) binds these to the
existing services; tests inject fakes.
"""

import json
import re
from typing import Any


def _resolve_value(obj: Any, path: list[str]) -> Any:
    """Walk a dict/list by path segments: ['items', '0', 'name']."""
    cur = obj
    for seg in path:
        if isinstance(cur, list):
            if seg.isdigit() and int(seg) < len(cur):
                cur = cur[int(seg)]
            else:
                return None
        elif isinstance(cur, dict):
            cur = cur.get(seg)
        else:
            return None
    return cur


def _render(template: str, ctx: Any) -> str:
    """{{inputs.<nodeId>.<field>}} substitution — minimal, safe.

    Supports deep paths into item arrays: {{inputs.n2.items[0].name}},
    plus back-compat {{inputs.n2.text}}. Missing paths render as "".
    """

    def repl(m):
        path = m.group(1).strip()
        parts = [p for p in path.split(".") if p]
        if len(parts) >= 3 and parts[0] == "inputs":
            node_id = parts[1]
            rest = parts[2:]
            val = ctx.outputs.get(node_id, {})
            if len(rest) >= 2 and "[" in rest[0]:
                # items[0].field → ['items', '0', 'field'] (split [N])
                head, _, tail = rest[0].partition("[")
                if head == "items":
                    idx = tail.rstrip("]")
                    segments = ["items", idx] + rest[1:]
                    val = _resolve_value(val, segments)
                    return "" if val is None else str(val)
            val = _resolve_value(val, rest)
            return "" if val is None else str(val)
        return m.group(0)

    return re.sub(r"\{\{\s*([^}]+)\s*\}\}", repl, template)


def handle_trigger(ctx, node):
    return {"text": ctx.trigger_input, "trigger": getattr(ctx, "trigger", "chat")}


def handle_llm(ctx, node):
    config = node["data"].get("config") or {}
    prompt = _render(config.get("prompt", ""), ctx)
    text = ctx.llm_complete(prompt, model=config.get("model") or "automatic")
    return {"text": text}


def _node_config(ctx: Any, node_id: str) -> dict:
    node = (getattr(ctx, "node_by_id", {}) or {}).get(node_id) or {}
    return (node.get("data") or {}).get("config") or {}


def handle_agent(ctx, node):
    config = node["data"].get("config") or {}
    agent_id = config["agent_id"]
    input_text = _render(config.get("input_template", "{{inputs}}"), ctx)

    # n8n-style provider wiring: supplemental connections into this node
    # resolve the sub-agent's model / tools / knowledge base.
    supp = (getattr(ctx, "supplemental_upstreams", {}) or {}).get(node["id"], {})
    kwargs: dict[str, Any] = {}

    # ai_llm → upstream LLM node supplies the model (+ temperature)
    llm_srcs = supp.get("ai_llm") or []
    if llm_srcs:
        provider_cfg = _node_config(ctx, llm_srcs[0])
        kwargs["model"] = provider_cfg.get("model") or "automatic"

    # ai_tool → upstream Tool nodes are listed as available tools
    tool_srcs = supp.get("ai_tool") or []
    if tool_srcs:
        tool_lines = []
        for src in tool_srcs:
            tool_cfg = _node_config(ctx, src)
            tool_lines.append(f"- {tool_cfg.get('tool_id', src)}")
        kwargs["extra_context"] = (
            "You have access to the following tools (invoke by name when the "
            "user's request needs them):\n" + "\n".join(tool_lines)
        )

    # ai_kb → upstream KB Search nodes provide retrieval context
    kb_srcs = supp.get("ai_kb") or []
    if kb_srcs:
        chunks = []
        for src in kb_srcs:
            out = ctx.outputs.get(src, {})
            docs = out.get("docs") or []
            chunks.extend(d.get("text", "") for d in docs)
        if chunks:
            context = "\n\n".join(chunks)
            kwargs["extra_context"] = (
                (kwargs.get("extra_context", "") + "\n\n" if kwargs.get("extra_context") else "")
                + "Relevant knowledge base context:\n" + context
            )

    result = ctx.run_agent(agent_id, input_text, **kwargs)
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


def handle_db_query(ctx, node):
    config = node["data"].get("config") or {}
    kb_id = config["kb_id"]
    mode = config.get("mode", "sql")
    limit = int(config.get("limit", 50))
    if mode == "nl":
        query = _render(config.get("question", ""), ctx)
    else:
        query = _render(config.get("sql", ""), ctx)
    result = ctx.db_query(kb_id, query, mode=mode, limit=limit)
    rows = result.get("rows") or []
    return {
        "text": result.get("text", ""),
        "rows": rows,
        "count": len(rows),
        "mode": mode,
    }


def handle_mcp_tool(ctx, node):
    config = node["data"].get("config") or {}
    server_id = config["server_id"]
    tool_name = config["tool_name"]
    upstream = next(iter(ctx.current_upstream.values()), {})
    arguments: dict | None = None
    raw_args = _render(config.get("arguments", ""), ctx).strip()
    if raw_args:
        try:
            parsed = json.loads(raw_args)
            arguments = parsed if isinstance(parsed, dict) else {"value": parsed}
        except json.JSONDecodeError:
            arguments = {"input": raw_args}
    else:
        arguments = {"input": upstream.get("text", "")}
    result = ctx.mcp_call(server_id, tool_name, arguments)
    if result.get("success"):
        return {"text": result.get("result", ""), "success": True, "tool": tool_name}
    return {"text": result.get("error", "MCP tool call failed"), "success": False, "tool": tool_name}


def _eval_op(value: str, op: str, target: str) -> bool:
    try:
        if op == "gt":
            return float(value) > float(target)
        if op == "lt":
            return float(value) < float(target)
        if op == "eq":
            return value == target
        if op == "ne":
            return value != target
        if op == "contains":
            return target in value
        if op == "empty":
            return not value.strip()
    except (TypeError, ValueError):
        return False
    return False


def handle_condition(ctx, node):
    config = node["data"].get("config") or {}
    upstream = next(iter(ctx.current_upstream.values()), {})
    value = str(upstream.get("text", ""))
    # Legacy branch-array form (pre-schema canvas) — keep round-tripping old flows.
    # Legacy routing always follows the single (port 0) edge.
    branches = config.get("branches")
    if isinstance(branches, list) and branches:
        for branch in branches:
            if branch.get("op") == "default" or _eval_op(
                value, branch.get("op", "eq"), str(branch.get("value", ""))
            ):
                return {"text": value, "branch": branch.get("on_true", "pass"), "_route": [0]}
        return {"text": value, "branch": "fail", "_route": [0]}
    # Schema form: field/op/value + outcome labels. True = port 0, False = port 1.
    field = config.get("field", "")
    if field:
        rendered = _render(field, ctx)
        if rendered:
            value = rendered
    op = config.get("op", "eq")
    ok = _eval_op(value, op, str(config.get("value", ""))) if op != "empty" else _eval_op(value, "empty", "")
    return {
        "text": value,
        "branch": config.get("on_true", "true") if ok else config.get("on_false", "false"),
        "_route": [0] if ok else [1],
    }


# Switch: case output ports 0..2, default output port 3 (matches the registry).
SWITCH_CASE_PORTS = 3
SWITCH_DEFAULT_PORT = 3


def handle_switch(ctx, node):
    config = node["data"].get("config") or {}
    upstream = next(iter(ctx.current_upstream.values()), {})
    value = str(upstream.get("text", ""))
    cases = config.get("cases") or []
    for i, case in enumerate(cases[:SWITCH_CASE_PORTS]):
        if not isinstance(case, dict):
            continue
        field = str(case.get("field", ""))
        rendered = _render(field, ctx) if field else value
        if rendered:
            value_to_test = rendered
        else:
            value_to_test = value
        op = case.get("op", "eq")
        ok = _eval_op(value_to_test, op, str(case.get("value", ""))) if op != "empty" else _eval_op(value_to_test, "empty", "")
        if ok:
            return {
                "text": value_to_test,
                "case": i,
                "branch": f"case_{i + 1}",
                "_route": [i],
            }
    return {
        "text": value,
        "case": "default",
        "branch": config.get("fallback_label", "default"),
        "_route": [SWITCH_DEFAULT_PORT],
    }


def handle_output(ctx, node):
    config = node["data"].get("config") or {}
    fmt = config.get("format", "markdown")
    upstream = next(iter(ctx.current_upstream.values()), {})
    text = upstream.get("text", "")
    if fmt == "json":
        text = json.dumps(upstream, ensure_ascii=False)
    return {"text": text, "format": fmt}


NODE_HANDLERS = {
    "trigger_chat": handle_trigger,
    "trigger_manual": handle_trigger,
    "trigger_schedule": handle_trigger,
    "trigger_webhook": handle_trigger,
    "llm": handle_llm,
    "agent": handle_agent,
    "tool": handle_tool,
    "kb_search": handle_kb_search,
    "db_query": handle_db_query,
    "mcp_tool": handle_mcp_tool,
    "condition": handle_condition,
    "switch": handle_switch,
    "output": handle_output,
}
