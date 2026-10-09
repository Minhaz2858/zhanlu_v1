"""Framework interop proof point: a LangGraph agent delegating to Zhanlu via A2A.

Demonstrates that Zhanlu's A2A endpoint is a standard, framework-agnostic
boundary: an agent built with ANY framework (here: LangGraph, the framework
Zhanlu itself does NOT depend on) can discover Zhanlu through
``/.well-known/agent.json`` and delegate governed work to it over
JSON-RPC 2.0 — no Zhanlu client library required.

Graph::

    START → discover_zhanlu → delegate_task → extract_answer → END

Usage (from the backend container, against a running Zhanlu):

    python scripts/interop_langgraph_a2a_demo.py \
        --base-url http://localhost:5002 \
        --agent general_assistant \
        --question "What are the main capabilities of this platform?"

Auth: dev JWT via /auth/login (admin seed), or set ZHANLU_A2A_API_KEY and
the script sends X-API-Key instead (the machine-to-machine path).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph


class _State(TypedDict):
    base_url: str
    token: dict[str, str]  # auth headers
    target_agent: str
    question: str
    agent_id: str
    task: dict[str, Any]
    answer: str


def _http_json(url: str, payload: dict | None = None, headers: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method="POST" if payload is not None else "GET",
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode())


def _auth_headers(args) -> dict[str, str]:
    if args.api_key:
        return {"X-API-Key": args.api_key}
    login = _http_json(
        f"{args.base_url}/api/apps/local-zhanlu-app/auth/login",
        {"email": args.email, "password": args.password},
    )
    return {"Authorization": f"Bearer {login['access_token']}"}


# ── LangGraph nodes ───────────────────────────────────────────────────────

def discover_zhanlu(state: _State) -> dict:
    """A2A step 1: discover the platform's agent card and resolve the
    target agent id by name."""
    card = _http_json(f"{state['base_url']}/.well-known/agent.json")
    skills = card.get("skills") or card.get("agents") or []
    agent_id = ""
    for skill in skills:
        if state["target_agent"] in (skill.get("id") or "", skill.get("name") or ""):
            agent_id = skill["id"]
            break
    if not agent_id and skills:
        agent_id = skills[0]["id"]  # fall back to the first published agent
    if not agent_id:
        raise RuntimeError("no published agents found in the agent card")
    return {"agent_id": agent_id}


def delegate_task(state: _State) -> dict:
    """A2A step 2: delegate the question as a governed task (JSON-RPC 2.0).
    The response is the SETTLED task — Zhanlu awaits dispatch inline."""
    rpc = _http_json(
        f"{state['base_url']}/a2a",
        {
            "jsonrpc": "2.0",
            "id": "langgraph-demo-1",
            "method": "tasks/send",
            "params": {
                "agent_id": state["agent_id"],
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": state["question"]}],
                },
                "metadata": {"via": "langgraph-interop-demo"},
            },
        },
        headers=state["token"],
    )
    if "error" in rpc:
        raise RuntimeError(f"A2A error: {rpc['error']}")
    task = rpc["result"]
    return {"task": task}


def extract_answer(state: _State) -> dict:
    """A2A step 3: pull the assistant text out of the settled task."""
    task = state["task"]
    text = ""
    for m in reversed(task.get("messages") or []):
        for part in m.get("parts") or []:
            kind = part.get("kind") or part.get("type") or "text"
            if kind == "text" and part.get("text"):
                text = part["text"]
                break
        if text:
            break
    if not text:
        for art in task.get("artifacts") or []:
            # artifact shape: flat "text" (Zhanlu) or parts (generic A2A)
            if art.get("text"):
                text = art["text"]
                break
            for part in art.get("parts") or []:
                if part.get("text"):
                    text = part["text"]
                    break
            if text:
                break
    if task.get("error") and not text:
        text = f"(task error: {task['error']})"
    return {"answer": text}


def build_graph(args):
    graph = StateGraph(_State)
    graph.add_node("discover_zhanlu", lambda s: discover_zhanlu({**s, "base_url": args.base_url, "target_agent": args.agent}))
    graph.add_node("delegate_task", lambda s: delegate_task({**s, "token": _auth_headers(args), "question": args.question}))
    graph.add_node("extract_answer", extract_answer)
    graph.add_edge(START, "discover_zhanlu")
    graph.add_edge("discover_zhanlu", "delegate_task")
    graph.add_edge("delegate_task", "extract_answer")
    graph.add_edge("extract_answer", END)
    return graph.compile()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:5002")
    parser.add_argument("--agent", default="general_assistant")
    parser.add_argument("--question", default="In two sentences, what can you help me with?")
    parser.add_argument("--email", default="admin@zhanlu.dev")
    parser.add_argument("--password", default="admin123")
    parser.add_argument("--api-key", default=os.environ.get("ZHANLU_A2A_API_KEY", ""))
    args = parser.parse_args()

    app = build_graph(args)
    final = app.invoke({
        "base_url": args.base_url, "token": {}, "target_agent": args.agent,
        "question": args.question, "agent_id": "", "task": {}, "answer": "",
    })
    task = final["task"]
    print("=" * 60)
    print("LangGraph agent → Zhanlu (A2A) interop proof point")
    print("=" * 60)
    print(f"agent card discovery : {'ok'}")
    print(f"resolved agent id    : {final['agent_id']}")
    print(f"task id              : {task.get('id')}")
    print(f"task status          : {task.get('status')}")
    print(f"external delegation  : {task.get('external')}")
    print("-" * 60)
    print(final["answer"][:800])
    print("=" * 60)
    ok = task.get("status") == "completed" and bool(final["answer"].strip()) \
        and "no text returned" not in final["answer"]
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
