"""AgentRunOrchestrator — unified agent execution loop.

Generalizes the two existing sub-agent loops into one DI-based runner:

1. ``SwarmRuntime.run()`` — agent-by-id, system_prompt from AgentApp,
   tool allow-list built from AgentApp.tools + skill registry.
2. ``_run_sub_agent()`` — agent-by-name from BUILTIN_AGENTS, guardrails
   (ToolLoopGuardController, IterationBudget, forced synthesis turn).

Both legacy paths will become thin adapters that inject their
dependencies into this orchestrator.

Design principles (from the P1 spec):
- DI over inheritance — ``llm_fn`` + ``tool_dispatcher`` injected as
  callables, no owned DB/LLM clients → unit-testable with fakes.
- Run-store writes are best-effort side effects — failures are logged
  at WARNING and never raised into the loop.
- All guardrails from the reference deployment (denied-tools, iteration budget, guard
  controller, forced synthesis turn) are preserved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
import time as _time
import uuid
from typing import Any, Awaitable, Callable

from app.services.tool_loop_guardrails import (
    ToolLoopGuardController,
    synthetic_blocked_result,
)
from app.services.iteration_budget import IterationBudget

logger = logging.getLogger(__name__)

__all__ = ["AgentRunOrchestrator", "RunResult"]

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    """Normalized result shape returned by every run."""
    run_id: str
    success: bool
    answer: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    iterations: int = 0
    error: str | None = None


# Callable signatures expected by the orchestrator
LlmFn = Callable[
    [list[dict], list[dict] | None, float],
    Awaitable[dict],  # → {"content": str, "tool_calls": list, "reasoning": str}
]
ToolDispatcher = Callable[
    [str, dict, Any, str | None, dict | None],
    Awaitable[dict],  # → tool result dict
]
RunStore = Callable[[str, dict], None]  # (event: "start"|"finish", payload: dict)
EventSink = Callable[[dict], None]  # (event: dict) → side-effect only

# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class AgentRunOrchestrator:
    """Run an agent loop with injected LLM + tool dispatch + guardrails.

    Parameters
    ----------
    agent_name : str
        Human-readable agent name for logging and run records.
    task : str
        Natural-language question / instruction for the agent.
    system_prompt : str
        Full system prompt to use (caller is responsible for building it).
    tool_schemas : list[dict]
        OpenAI-format tool schemas the agent may use.
    allowed_tools : set[str]
        Tool names the agent is allowed to call (enforced by tool_dispatcher
        wrapper).
    llm_fn : LlmFn
        async callable (messages, tools | None, temperature) → dict.
    tool_dispatcher : ToolDispatcher
        async callable (tool_name, args, db, user_id, context) → dict.
    db : Any
        Database session (passed through to tool_dispatcher).
    user_id : str | None
        User ID for permission checks (passed through).
    context : dict | None
        Agent runtime context (org_id, app_id, bound_kb_ids, …).
    denied_tools : set[str] | None
        Tool names blocked even if present in allowed_tools / schemas
        (recursion protection).
    max_iterations : int
        Max tool-calling iterations (default 8).
    temperature : float
        LLM sampling temperature (default 0.3 for sub-agents).
    event_sink : EventSink | None
        Optional callback for per-turn events (logs, metrics, streaming).
    run_store : RunStore | None
        Optional persistence callback: ``("start"|"finish", payload)``.
        Failures are swallowed (logged at WARNING).
    pre_call_prep : Callable[[list[dict]], None] | None
        Optional sanitization / pruning hook called before every LLM call
        (mutates messages in place). If None, messages are sent as-is.
    persist_result_str : Callable[[str, dict], str] | None
        Optional hook to serialize/persist a tool result dict into a string
        suitable for appending to messages. If None, uses ``json.dumps``.
    apply_turn_budget : Callable[[list[dict], list[str], list[str]], None] | None
        Optional hook to enforce per-turn tool-output budget (spills to disk).
    run_id : str | None
        Pre-assigned run_id. Auto-generated if None.
    """

    def __init__(
        self,
        *,
        agent_name: str,
        task: str,
        system_prompt: str,
        tool_schemas: list[dict],
        allowed_tools: set[str],
        llm_fn: LlmFn,
        tool_dispatcher: ToolDispatcher,
        db: Any = None,
        user_id: str | None = None,
        context: dict | None = None,
        denied_tools: set[str] | None = None,
        max_iterations: int = 8,
        temperature: float = 0.3,
        event_sink: EventSink | None = None,
        run_store: RunStore | None = None,
        pre_call_prep: Callable[[list[dict]], None] | None = None,
        persist_result_str: Callable[[str, dict], str] | None = None,
        apply_turn_budget: (
            Callable[[list[dict], list[str], list[str]], None] | None
        ) = None,
        run_id: str | None = None,
    ):
        self.agent_name = agent_name
        self.task = task
        self.system_prompt = system_prompt
        self.tool_schemas = tool_schemas
        self.allowed_tools = allowed_tools
        self.llm_fn = llm_fn
        self.tool_dispatcher = tool_dispatcher
        self.db = db
        self.user_id = user_id
        self.context = context or {}
        self.denied_tools = denied_tools or set()
        self.max_iterations = max_iterations
        self.temperature = temperature
        self.event_sink = event_sink
        self.run_store = run_store
        self.pre_call_prep = pre_call_prep
        self.persist_result_str = persist_result_str or _default_persist
        self.apply_turn_budget = apply_turn_budget
        self.run_id = run_id or _new_run_id()

    # ------------------------------------------------------------------
    async def run(self) -> RunResult:
        """Execute the full agent conversation loop."""
        run_started = _time.monotonic()

        self._emit_run_store("start", {
            "agent_name": self.agent_name,
            "task": self.task,
            "max_iterations": self.max_iterations,
            "tool_count": len(self.tool_schemas),
        })
        self._emit_event("run_start", {
            "agent_name": self.agent_name,
            "max_iterations": self.max_iterations,
        })

        guard_ctrl = ToolLoopGuardController()
        iteration_budget = IterationBudget(max_total=self.max_iterations)

        messages: list[dict] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": self.task},
        ]
        captured_tool_calls: list[dict] = []
        iterations = 0
        final_text = ""

        try:
            for _iteration_idx in range(self.max_iterations):
                if not iteration_budget.consume():
                    logger.info(
                        "AgentRunOrchestrator (%s) budget exhausted (%d/%d)",
                        self.agent_name,
                        iteration_budget.used,
                        iteration_budget.max_total,
                    )
                    break
                iterations += 1

                # --- per-turn prep (sanitize / prune) ---
                if self.pre_call_prep:
                    self.pre_call_prep(messages)

                # --- LLM call ---
                t0 = _time.monotonic()
                try:
                    llm_response = await self.llm_fn(
                        messages,
                        self.tool_schemas if self.tool_schemas else None,
                        self.temperature,
                    )
                except Exception as e:
                    logger.warning(
                        "AgentRunOrchestrator (%s) LLM failed iter %d: %s",
                        self.agent_name, _iteration_idx, e,
                    )
                    self._emit_event("llm_call", {
                        "iteration": iterations,
                        "has_content": False,
                        "tool_call_count": 0,
                        "duration_ms": round((_time.monotonic() - t0) * 1000),
                        "error": str(e)[:256],
                    })
                    return self._finish(
                        captured_tool_calls, iterations,
                        success=False,
                        answer=f"The {self.agent_name} could not reach the language model ({str(e)[:120]}).",
                        error=str(e),
                    )

                content = llm_response.get("content", "") or ""
                raw_tool_calls = llm_response.get("tool_calls", []) or []

                self._emit_event("llm_call", {
                    "iteration": iterations,
                    "has_content": bool(content),
                    "tool_call_count": len(raw_tool_calls),
                    "duration_ms": round((_time.monotonic() - t0) * 1000),
                    "content_preview": content[:500] if content else "",
                    "reasoning": llm_response.get("reasoning", ""),
                    "model": llm_response.get("model", "default"),
                    "prompt_tokens": llm_response.get("usage", {}).get(
                        "prompt_tokens", 0
                    ),
                    "completion_tokens": llm_response.get("usage", {}).get(
                        "completion_tokens", 0
                    ),
                    "messages": _messages_snapshot(messages),
                })

                # --- no tool calls → final answer ---
                if not raw_tool_calls:
                    final_text = content
                    break

                # --- append assistant message ---
                messages.append({
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [
                        {
                            "id": tc.get("id", str(uuid.uuid4())),
                            "type": "function",
                            "function": {
                                "name": tc["function"]["name"],
                                "arguments": tc["function"]["arguments"],
                            },
                        }
                        for tc in raw_tool_calls
                    ],
                })

                # --- execute tools ---
                batch_ids: list[str] = []
                batch_names: list[str] = []
                for tc in raw_tool_calls:
                    tool_name = tc.get("function", {}).get("name", "")
                    raw_args = tc.get("function", {}).get("arguments", "{}")
                    tool_call_id = tc.get("id", str(uuid.uuid4()))

                    batch_ids.append(tool_call_id)
                    batch_names.append(tool_name)

                    try:
                        tool_args = (
                            json.loads(raw_args)
                            if isinstance(raw_args, str)
                            else raw_args
                        )
                    except json.JSONDecodeError:
                        tool_args = {}

                    # --- guardrails ---
                    guard_decision = guard_ctrl.before_call(tool_name, tool_args)
                    if not guard_decision.allows_execution:
                        result = json.loads(synthetic_blocked_result(guard_decision))
                    elif tool_name in self.denied_tools:
                        result = {
                            "success": False,
                            "error": f"Tool {tool_name!r} is not available to the {self.agent_name}.",
                        }
                    elif (
                        self.allowed_tools
                        and tool_name not in self.allowed_tools
                    ):
                        result = {
                            "success": False,
                            "error": f"Tool {tool_name!r} is not available to the {self.agent_name}.",
                        }
                    else:
                        t_tool = _time.monotonic()
                        try:
                            result = await self.tool_dispatcher(
                                tool_name, tool_args,
                                self.db, self.user_id, self.context,
                            )
                            self._emit_event("tool_call", {
                                "tool_name": tool_name,
                                "duration_ms": round(
                                    (_time.monotonic() - t_tool) * 1000
                                ),
                                "success": bool(result.get("success")),
                                "error": result.get("error"),
                                "args_hash": _hash_args(tool_args),
                                "iteration": iterations,
                                "result_preview": (
                                    result.get("result", str(result))[:500]
                                    if isinstance(result, dict) else str(result)[:500]
                                ),
                            })
                        except Exception as dispatch_err:
                            logger.warning(
                                "AgentRunOrchestrator (%s) tool dispatch error "
                                "tool=%s iter=%d: %s",
                                self.agent_name, tool_name, _iteration_idx,
                                dispatch_err,
                            )
                            result = {
                                "success": False,
                                "error": f"Tool execution failed: {dispatch_err}",
                            }
                            self._emit_event("tool_call", {
                                "tool_name": tool_name,
                                "duration_ms": round(
                                    (_time.monotonic() - t_tool) * 1000
                                ),
                                "success": False,
                                "error": str(dispatch_err)[:256],
                                "args_hash": _hash_args(tool_args),
                                "iteration": iterations,
                            })

                    result_str = self.persist_result_str(tool_name, result)  # type: ignore[assignment]
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": result_str,
                    })
                    guard_ctrl.after_call(tool_name, tool_args, result_str)

                    captured_tool_calls.append({
                        "tool": tool_name,
                        "call_id": tool_call_id,
                        "args": tool_args,
                        "result_success": result.get("success"),
                        "iteration": iterations,
                    })

                # --- turn budget enforcement ---
                if self.apply_turn_budget:
                    self.apply_turn_budget(messages, batch_ids, batch_names)

                # --- guardrail halt? ---
                if guard_ctrl.halt_decision:
                    hd = guard_ctrl.halt_decision
                    logger.warning(
                        "AgentRunOrchestrator (%s) guardrail halt: %s "
                        "(tool=%s, count=%d)",
                        self.agent_name, hd.code, hd.tool_name, hd.count,
                    )
                    messages.append({
                        "role": "user",
                        "content": (
                            f"Tool '{hd.tool_name}' is looping (count={hd.count}). "
                            "Stop calling it and write your final answer now."
                        ),
                    })
                    break

            # ----------------------------------------------------------
            # Post-loop: forced synthesis turn if no final text
            # ----------------------------------------------------------
            if not final_text:
                logger.info(
                    "AgentRunOrchestrator (%s): forcing synthesis turn "
                    "(prior iters=%d)",
                    self.agent_name, iterations,
                )
                t_syn = _time.monotonic()
                try:
                    synthesis_messages = list(messages) + [
                        {
                            "role": "user",
                            "content": (
                                "You have the data above. Stop calling tools "
                                "and write a concise prose answer now. Open "
                                "with the direct answer in one sentence, then "
                                "add any necessary breakdown. Do NOT emit any "
                                "more tool calls."
                            ),
                        }
                    ]
                    if self.pre_call_prep:
                        self.pre_call_prep(synthesis_messages)
                    synthesis_response = await self.llm_fn(
                        synthesis_messages, tools=None,
                        temperature=self.temperature,
                    )
                    final_text = (
                        synthesis_response.get("content", "") or ""
                    ).strip()
                    iterations += 1
                    self._emit_event("synthesis", {
                        "duration_ms": round(
                            (_time.monotonic() - t_syn) * 1000
                        ),
                        "iterations": iterations,
                        "has_content": bool(final_text),
                    })
                except Exception as e:
                    logger.warning(
                        "AgentRunOrchestrator (%s) synthesis turn failed "
                        "(soft): %s",
                        self.agent_name, e,
                    )

            if not final_text:
                final_text = (
                    f"The {self.agent_name} did not return a final answer."
                )

            return self._finish(
                captured_tool_calls, iterations,
                success=True, answer=final_text,
            )

        except Exception as e:
            logger.exception(
                "AgentRunOrchestrator (%s) unhandled error: %s",
                self.agent_name, e,
            )
            return self._finish(
                captured_tool_calls, iterations,
                success=False,
                answer=final_text or f"{self.agent_name} encountered an error.",
                error=str(e),
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _finish(
        self,
        tool_calls: list[dict],
        iterations: int,
        *,
        success: bool,
        answer: str,
        error: str | None = None,
    ) -> RunResult:
        """Emit the final run-store event and return RunResult."""
        self._emit_run_store("finish", {
            "success": success,
            "answer": answer,
            "iterations": iterations,
            "tool_call_count": len(tool_calls),
            "error": error,
        })
        self._emit_event("run_finish", {
            "success": success,
            "iterations": iterations,
            "tool_call_count": len(tool_calls),
            "error": error,
        })
        return RunResult(
            run_id=self.run_id,
            success=success,
            answer=answer,
            tool_calls=tool_calls,
            iterations=iterations,
            error=error,
        )

    def _emit_run_store(self, event: str, payload: dict) -> None:
        if self.run_store is None:
            return
        try:
            self.run_store(event, payload)
        except Exception as e:
            logger.warning(
                "AgentRunOrchestrator (%s) run_store(%r) failed: %s",
                self.agent_name, event, e,
            )

    def _emit_event(self, event_type: str, payload: dict) -> None:
        if self.event_sink is None:
            return
        try:
            self.event_sink({
                "type": event_type,
                "agent_name": self.agent_name,
                "run_id": self.run_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                **payload,
            })
        except Exception as e:
            logger.debug(
                "AgentRunOrchestrator (%s) event_sink failed: %s",
                self.agent_name, e,
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _new_run_id() -> str:
    return uuid.uuid4().hex[:32]


def _default_persist(_tool_name: str, result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, default=str)


def _hash_args(args: dict | None, length: int = 12) -> str:
    """Stable hash of tool arguments for tracing dedup."""
    if not args:
        return ""
    try:
        raw = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()[:length]
    except Exception:
        return "err"


def _messages_snapshot(messages: list[dict], keep_last: int = 6) -> str | None:
    """Compact serialisation of the message list for checkpointing."""
    if not messages:
        return None
    # Keep system + user + the most recent N messages
    core = [m for m in messages if m.get("role") in ("system", "user")]
    tail = messages[-keep_last:] if len(messages) > keep_last else []
    snapshot = core + [m for m in tail if m not in core]
    try:
        return json.dumps(snapshot, ensure_ascii=False, default=str)
    except Exception:
        return str(snapshot)[:4000]
