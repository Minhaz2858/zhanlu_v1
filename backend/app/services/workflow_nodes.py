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


def first_item(ctx: Any, node_id: str) -> dict:
    """First upstream item for a node, defaulting to the text output."""
    out = ctx.outputs.get(node_id, {})
    items = out.get("items")
    if items:
        return items[0] if isinstance(items[0], dict) else {"value": items[0]}
    return {"text": out.get("text", "")}


def all_items(ctx: Any, node_id: str) -> list[dict]:
    """All upstream items for a node, defaulting to a single text item."""
    out = ctx.outputs.get(node_id, {})
    items = out.get("items")
    if items:
        return [i if isinstance(i, dict) else {"value": i} for i in items]
    return [{"text": out.get("text", "")}]


def _upstream_id(ctx: Any, node: dict) -> str:
    """First node id in current_upstream (fallback: first output)."""
    if getattr(ctx, "current_upstream", None):
        return next(iter(ctx.current_upstream), "")
    return next(iter(ctx.outputs), "")


def _json_summary(items: list[dict]) -> str:
    return json.dumps(items[:3], ensure_ascii=False) if items else "0 items"


def handle_trigger(ctx, node):
    text = ctx.trigger_input
    out = {"text": text, "trigger": getattr(ctx, "trigger", "chat"), "items": [{"text": text}]}
    # Chat-trigger history (option B): when the chat path injected prior
    # turns via ctx.trigger_history, expose a rendered transcript so
    # workflow authors can reference {{inputs.<node>.history}}.
    history = getattr(ctx, "trigger_history", None)
    if history:
        lines = []
        for m in history[-10:]:
            role = "User" if m.get("role") == "user" else "Assistant"
            content = str(m.get("content", ""))
            if content:
                lines.append(f"{role}: {content}")
        if lines:
            out["history"] = "\n".join(lines)
    return out


def handle_llm(ctx, node):
    config = node["data"].get("config") or {}
    prompt = _render(config.get("prompt", ""), ctx)
    text = ctx.llm_complete(prompt, model=config.get("model") or "automatic")
    return {"text": text, "items": [{"text": text}]}


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
    text = str(result.get("text", result))
    return {"text": text, "items": [{"text": text}]}


def handle_tool(ctx, node):
    config = node["data"].get("config") or {}
    tool_id = config["tool_id"]
    input_text = _render(config.get("input_template", "{{inputs}}"), ctx)
    result = ctx.run_tool(tool_id, input_text)
    text = str(result.get("text", result))
    return {"text": text, "items": [{"text": text}]}


def handle_kb_search(ctx, node):
    config = node["data"].get("config") or {}
    query = _render(config.get("query", "{{inputs}}"), ctx)
    docs = ctx.kb_search(config["kb_id"], query, top_k=int(config.get("top_k", 5)))
    return {"text": "\n\n".join(d.get("text", "") for d in docs), "docs": docs, "items": docs}


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
        "items": rows,
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
        text = result.get("result", "")
        return {"text": text, "success": True, "tool": tool_name, "items": [{"text": text}]}
    text = result.get("error", "MCP tool call failed")
    return {"text": text, "success": False, "tool": tool_name, "items": [{"text": text}]}


def handle_http_request(ctx, node):
    config = node["data"].get("config") or {}
    url = _render(config.get("url", ""), ctx)
    headers = []
    for h in config.get("headers") or []:
        headers.append({"key": _render(str(h.get("key", "")), ctx), "value": _render(str(h.get("value", "")), ctx)})
    body = _render(config.get("body", ""), ctx)
    bearer = _render(config.get("bearer_token", ""), ctx)
    result = ctx.http_request(
        config.get("method", "GET"), url, headers=headers, body=body,
        timeout=int(config.get("timeout", 15)),
        auth_type=config.get("auth_type", "none"), bearer_token=bearer,
        response_format=config.get("response_format", "auto"),
    )
    return {
        "text": result.get("text", ""),
        "items": result.get("items") or [],
        "status": result.get("status", 0),
        "success": result.get("success", False),
    }


def handle_code(ctx, node):
    config = node["data"].get("config") or {}
    code = _render(config.get("code", ""), ctx)
    upstream_id = _upstream_id(ctx, node)
    items_in = all_items(ctx, upstream_id) if upstream_id else [{"text": ""}]
    timeout = int(config.get("timeout", 10))
    out_items: list[dict] = []
    for it in items_in:
        result = ctx.run_code(code, it, timeout=timeout)
        if not result.get("success"):
            return {"text": result.get("text", "Code failed"), "items": [], "success": False}
        out_items.extend(result.get("items", []))
    return {"text": _json_summary(out_items), "items": out_items, "success": True}


def handle_set_fields(ctx, node):
    config = node["data"].get("config") or {}
    items = [dict(it) for it in all_items(ctx, _upstream_id(ctx, node))]
    for it in items:
        for f in config.get("fields") or []:
            if f.get("name"):
                it[f["name"]] = _render(str(f.get("value", "")), ctx)
    return {"text": _json_summary(items), "items": items}


def handle_split_out(ctx, node):
    config = node["data"].get("config") or {}
    field = _render(config.get("field", "items"), ctx)
    item_field = config.get("item_field", "item")
    out_items: list[dict] = []
    for it in all_items(ctx, _upstream_id(ctx, node)):
        arr = it.get(field)
        if isinstance(arr, list) and arr:
            for elem in arr:
                out_items.append({**it, item_field: elem})
        else:
            out_items.append(it)
    return {"text": _json_summary(out_items), "items": out_items}


def handle_remove_duplicates(ctx, node):
    config = node["data"].get("config") or {}
    field = _render(config.get("field", "id"), ctx)
    seen: set[str] = set()
    out_items: list[dict] = []
    for it in all_items(ctx, _upstream_id(ctx, node)):
        key = str(it.get(field, ""))
        if key in seen:
            continue
        seen.add(key)
        out_items.append(it)
    return {"text": _json_summary(out_items), "items": out_items}


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
        "items": upstream.get("items") or [{"text": value}],
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
                "items": upstream.get("items") or [{"text": value_to_test}],
            }
    return {
        "text": value,
        "case": "default",
        "branch": config.get("fallback_label", "default"),
        "_route": [SWITCH_DEFAULT_PORT],
        "items": upstream.get("items") or [{"text": value}],
    }


def handle_output(ctx, node):
    config = node["data"].get("config") or {}
    fmt = config.get("format", "markdown")
    upstream = next(iter(ctx.current_upstream.values()), {})
    text = upstream.get("text", "")
    items = upstream.get("items")
    if fmt == "json":
        text = json.dumps(upstream, ensure_ascii=False)
    elif fmt == "table" and items:
        text = "\n".join(json.dumps(i, ensure_ascii=False) for i in items)
    return {"text": text, "format": fmt, "items": items}


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
    "http_request": handle_http_request,
    "code": handle_code,
    "set_fields": handle_set_fields,
    "split_out": handle_split_out,
    "remove_duplicates": handle_remove_duplicates,
    "condition": handle_condition,
    "switch": handle_switch,
    "output": handle_output,
}
