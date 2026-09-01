"""Bridge from the main chatbot to flow-mode Agent Workflows.

The chat message endpoints (agents.py v2/v3) branch on ``agent_app.flow_mode``
and call :func:`run_workflow_for_chat` instead of the prompt-agent loop. It
validates the workflow has a chat trigger, builds conversation history from
``conv.messages`` (option B — context-injected multi-turn), runs the
workflow (trigger="chat"), and returns the reply the chat path persists.

The workflow's own ``WorkflowRun`` / ``WorkflowRunStep`` rows (trigger="chat")
give full per-node visibility in the workflow's run history.
"""

from app.services.workflow_runtime import run_workflow_agent

MAX_HISTORY_TURNS = 10


def has_chat_trigger(flow) -> bool:
    """True when the flow graph contains a trigger_chat node."""
    if not isinstance(flow, dict):
        return False
    return any(
        (n or {}).get("type") == "trigger_chat"
        for n in (flow.get("nodes") or [])
    )


def build_history(messages, exclude_content: str | None = None) -> list[dict]:
    """Last ``MAX_HISTORY_TURNS`` turns of {role, content} from conv.messages.

    Drops the current user message (``exclude_content``) so the workflow only
    sees PRIOR turns; empty-content entries are skipped. Order is preserved
    (oldest → newest as stored).
    """
    turns: list[dict] = []
    for m in messages or []:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
        if role in ("user", "assistant") and content:
            turns.append({"role": role, "content": str(content)})
    if exclude_content and turns and turns[-1]["role"] == "user" and turns[-1]["content"] == str(exclude_content):
        turns.pop()
    return turns[-MAX_HISTORY_TURNS:]


def run_workflow_for_chat(db, conv, agent_app, user_content, user_id: str | None = None) -> dict:
    """Run a flow_mode agent from the chat path.

    Returns ``{"content": str, "status": "completed"|"failed", "run_id": str|None}``.

    ``user_id`` must be a plain string, NOT an ORM User instance: the
    auth dependency's User can be detached/expired by the time the
    stream generator runs (the early user-message commit expires
    attributes), and touching it raises DetachedInstanceError.
    """
    flow = agent_app.flow if isinstance(agent_app.flow, dict) else {}
    if not has_chat_trigger(flow):
        return {
            "content": "This workflow has no Chat Trigger, so it cannot be used from chat.",
            "status": "failed",
            "run_id": None,
        }
    result = run_workflow_agent(
        db,
        agent_app.org_id or "default-org",
        agent_app.app_id or "default-app",
        agent_app,
        user_content,
        created_by_id=user_id,
        trigger="chat",
        history=build_history(getattr(conv, "messages", None) or [], exclude_content=user_content),
    )
    if result["status"] == "completed":
        content = result.get("output")
        return {
            "content": "" if content is None else str(content),
            "status": "completed",
            "run_id": result.get("run_id"),
        }
    return {
        "content": f"Workflow failed: {result.get('error', 'unknown error')}",
        "status": "failed",
        "run_id": result.get("run_id"),
    }
