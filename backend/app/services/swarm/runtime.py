"""Swarm runtime — real tool-calling loop for spawned agents.

The base :class:`app.services.swarm.SwarmCoordinator` runs each spawned
agent via a raw ``call_llm(prompt=task)`` call. That has no tool access,
no system prompt, and no agent-specific instructions — so every spawned
agent was effectively the same dumb LLM call.

This module provides :class:`SwarmRuntime`, a thin wrapper that resolves
the agent name to an :class:`AgentDefinition`, builds a proper system
prompt + tool list, and runs a tool-calling loop (the same one used by
``add_message_stream`` in the main agent router).

The result is a spawned agent that:
- Has the system prompt of its named definition (general-purpose, explore, etc.)
- Can call the same tools as the main agent (subject to the same permission rules)
- Returns its final text response to the mailbox, not a raw LLM dump

The runtime is intentionally a standalone component (no inheritance from
``SwarmCoordinator``) so it can be reused outside the swarm context —
e.g. a worker pool, a delegation helper, or a future "agent-of-agents"
node.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class SwarmAgentResult:
    """The final result of a spawned agent's run."""
    member_name: str
    agent_name: str
    task: str
    final_response: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    success: bool = True
    error: str | None = None


class SwarmRuntime:
    """Runtime that runs a spawned agent through the real tool-calling loop.

    The runtime is a thin orchestration layer — it does not own a database
    session, an LLM client, or a tool registry. Callers (typically
    :class:`SwarmCoordinator`) inject these via the ``llm_fn`` and
    ``tool_dispatcher`` callables, which keeps the runtime unit-testable
    without spinning up the full FastAPI app.

    Example wiring from ``SwarmCoordinator._run_agent``::

        runtime = SwarmRuntime()
        result = await runtime.run(
            agent_name="general-purpose",
            task="Find the top 5 customers by revenue",
            llm_fn=my_llm_caller,
            tool_dispatcher=my_execute_tool,
        )
    """

    def __init__(self) -> None:
        self._resolved: dict[str, dict] = {}

    def resolve_agent(self, agent_name: str) -> dict | None:
        """Resolve an agent name to its system prompt + tool list.

        Returns a dict with keys ``name``, ``system_prompt``, ``tools``,
        ``description`` (or ``None`` if the agent is unknown).
        """
        if agent_name in self._resolved:
            return self._resolved[agent_name]
        try:
            from app.services.agent_definitions import get_agent_definition
        except ImportError as e:
            logger.warning("Cannot import agent_definitions: %s", e)
            return None
        definition = get_agent_definition(agent_name)
        if definition is None:
            return None
        resolved = {
            "name": definition.name,
            "description": getattr(definition, "description", ""),
            "system_prompt": getattr(definition, "system_prompt", ""),
            "tools": list(getattr(definition, "tools", []) or []),
        }
        self._resolved[agent_name] = resolved
        return resolved

    async def run(
        self,
        agent_name: str,
        task: str,
        llm_fn: Callable[[str, list[dict]], Awaitable[dict]],
        tool_dispatcher: Callable[[str, dict, Any, str | None, dict | None], Awaitable[dict]],
        db: Any = None,
        user_id: str | None = None,
        member_name: str | None = None,
        max_iterations: int = 8,
        use_harness: bool | None = None,
    ) -> SwarmAgentResult:
        """Run a single agent on a task using the tool-calling loop.

        Args:
            agent_name: The name of the agent definition to use
                (e.g. "general-purpose", "explore", "worker").
            task: The natural-language task description.
            llm_fn: Async callable that takes a system prompt + messages
                list and returns ``{"response": str, "tool_calls": list[dict]}``.
                Typically a thin wrapper over ``call_llm``.
            tool_dispatcher: Async callable matching the signature of
                ``execute_tool(tool_name, arguments, db, user_id, context)``.
            db: Optional database session forwarded to the tool dispatcher.
            user_id: Optional user ID forwarded to the tool dispatcher.
            member_name: Optional display name for the spawned member.
            max_iterations: Maximum tool-calling iterations before forcing
                a final answer. Default 8.
            use_harness: If True, delegate to AgentRunOrchestrator (P1 harness).
                If None, reads AGENT_HARNESS_ENABLED from config.

        Returns:
            A :class:`SwarmAgentResult` with the final response, the
            list of tool calls made, and a success flag.
        """
        # ---- Harness path (P1 unified agent harness) ----
        if use_harness is None:
            try:
                from app.config import settings
                use_harness = settings.AGENT_HARNESS_ENABLED
            except Exception:
                use_harness = False

        if use_harness:
            return await self._run_via_harness(
                agent_name=agent_name,
                task=task,
                llm_fn=llm_fn,
                tool_dispatcher=tool_dispatcher,
                db=db,
                user_id=user_id,
                member_name=member_name,
                max_iterations=max_iterations,
            )
        definition = self.resolve_agent(agent_name)
        if definition is None:
            return SwarmAgentResult(
                member_name=member_name or agent_name,
                agent_name=agent_name,
                task=task,
                success=False,
                error=f"Unknown agent: {agent_name}",
            )

        system_prompt = definition["system_prompt"]
        allowed_tools = set(definition.get("tools") or [])
        messages: list[dict] = [{"role": "user", "content": task}]
        tool_calls_log: list[dict] = []

        for iteration in range(max_iterations):
            try:
                llm_result = await llm_fn(system_prompt, messages)
            except Exception as e:  # noqa: BLE001
                logger.warning("LLM call failed in swarm runtime: %s", e)
                return SwarmAgentResult(
                    member_name=member_name or agent_name,
                    agent_name=agent_name,
                    task=task,
                    tool_calls=tool_calls_log,
                    success=False,
                    error=f"LLM error: {e}",
                )

            tool_calls = llm_result.get("tool_calls") or []
            if not tool_calls:
                # No more tool calls — the LLM produced a final text answer.
                return SwarmAgentResult(
                    member_name=member_name or agent_name,
                    agent_name=agent_name,
                    task=task,
                    tool_calls=tool_calls_log,
                    final_response=llm_result.get("response", ""),
                )

            # Execute each tool call sequentially, appending results to messages
            for tc in tool_calls:
                name = tc.get("name") or tc.get("function", {}).get("name")
                args = tc.get("arguments") or tc.get("function", {}).get("arguments") or {}
                if isinstance(args, str):
                    import json
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}

                # Enforce tool allow-list if the agent definition specifies one
                if allowed_tools and name not in allowed_tools:
                    result = {"success": False, "error": f"Tool '{name}' not allowed for agent '{agent_name}'"}
                else:
                    try:
                        result = await tool_dispatcher(name, args, db, user_id, None)
                    except Exception as e:  # noqa: BLE001
                        result = {"success": False, "error": str(e)}

                tool_calls_log.append({
                    "tool": name,
                    "arguments": args,
                    "result": result,
                    "iteration": iteration,
                })
                # Tool result back to the LLM as a synthetic message
                messages.append({
                    "role": "tool",
                    "name": name,
                    "content": str(result)[:4000],
                })

        # Hit max iterations — force a final answer
        try:
            llm_result = await llm_fn(system_prompt, messages)
            final = llm_result.get("response", "")
        except Exception as e:  # noqa: BLE001
            final = f"(hit max iterations {max_iterations}; LLM error: {e})"
        return SwarmAgentResult(
            member_name=member_name or agent_name,
            agent_name=agent_name,
            task=task,
            tool_calls=tool_calls_log,
            final_response=final,
            success=False,
            error=f"max_iterations={max_iterations}",
        )

    async def _run_via_harness(
        self,
        *,
        agent_name: str,
        task: str,
        llm_fn: Callable[[str, list[dict]], Awaitable[dict]],
        tool_dispatcher: Callable[[str, dict, Any, str | None, dict | None], Awaitable[dict]],
        db: Any = None,
        user_id: str | None = None,
        member_name: str | None = None,
        max_iterations: int = 8,
    ) -> SwarmAgentResult:
        """Delegate the run to AgentRunOrchestrator (P1 harness)."""
        definition = self.resolve_agent(agent_name)
        if definition is None:
            return SwarmAgentResult(
                member_name=member_name or agent_name,
                agent_name=agent_name,
                task=task,
                success=False,
                error=f"Unknown agent: {agent_name}",
            )

        system_prompt = definition["system_prompt"]
        allowed_tools = set(definition.get("tools") or [])
        tool_names = sorted(allowed_tools)

        # Bridge llm_fn: SwarmRuntime signature → orchestrator signature
        async def _harness_llm(
            messages: list[dict],
            tools: list[dict] | None,
            temperature: float,
        ) -> dict:
            raw = await llm_fn(system_prompt, messages)
            return {
                "content": raw.get("response", raw.get("content", "")),
                "tool_calls": raw.get("tool_calls", []),
            }

        from app.services.harness.orchestrator import AgentRunOrchestrator

        orch = AgentRunOrchestrator(
            agent_name=agent_name,
            task=task,
            system_prompt=system_prompt,
            tool_schemas=[],  # Swarm agents use their own tool schemas
            allowed_tools=allowed_tools,
            llm_fn=_harness_llm,
            tool_dispatcher=tool_dispatcher,
            db=db,
            user_id=user_id,
            max_iterations=max_iterations,
        )
        result = await orch.run()
        return SwarmAgentResult(
            member_name=member_name or agent_name,
            agent_name=agent_name,
            task=task,
            final_response=result.answer,
            tool_calls=result.tool_calls,
            success=result.success,
            error=result.error,
        )


# Singleton — SwarmCoordinator can reuse it.
_runtime: SwarmRuntime | None = None


def get_swarm_runtime() -> SwarmRuntime:
    global _runtime
    if _runtime is None:
        _runtime = SwarmRuntime()
    return _runtime
