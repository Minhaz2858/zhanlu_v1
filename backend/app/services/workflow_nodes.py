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
        if op == "gt":
            return float(value) > float(target)
        if op == "lt":
            return float(value) < float(target)
        if op == "eq":
            return value == target
        if op == "contains":
            return target in value
    except (TypeError, ValueError):
        return False
    return False


def handle_condition(ctx, node):
    config = node["data"].get("config") or {}
    upstream = next(iter(ctx.current_upstream.values()), {})
    value = str(upstream.get("text", ""))
    for branch in config.get("branches", []):
        if branch.get("op") == "default" or _eval_op(
            value, branch.get("op", "eq"), str(branch.get("value", ""))
        ):
            return {"branch": branch.get("on_true", "pass")}
    return {"branch": "fail"}


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
    "llm": handle_llm,
    "agent": handle_agent,
    "tool": handle_tool,
    "kb_search": handle_kb_search,
    "condition": handle_condition,
    "output": handle_output,
}
