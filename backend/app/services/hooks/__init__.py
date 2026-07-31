"""Hooks lifecycle system for Zhanlu — adapted from OpenHarness.

Supports 4 hook types: command, http, prompt, agent.
Supports 10 event points: session_start/end, pre/post_compact,
pre/post_tool_use, user_prompt_submit, notification, stop/subagent_stop.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class HookEvent(str, Enum):
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    PRE_COMPACT = "pre_compact"
    POST_COMPACT = "post_compact"
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    USER_PROMPT_SUBMIT = "user_prompt_submit"
    NOTIFICATION = "notification"
    STOP = "stop"
    SUBAGENT_STOP = "subagent_stop"


class HookType(str, Enum):
    COMMAND = "command"
    HTTP = "http"
    PROMPT = "prompt"
    AGENT = "agent"


class HookConfig(BaseModel):
    id: str
    name: str = ""
    event: str
    type: str
    command: str | None = None
    url: str | None = None
    method: str = "POST"
    headers: dict[str, str] | None = None
    prompt: str | None = None
    timeout: int = 30
    priority: int = 0
    matcher: str | None = None
    block_on_failure: bool = False
    enabled: bool = True

    def matches(self, target: str | None) -> bool:
        if not self.matcher:
            return True
        if not target:
            return False
        import fnmatch
        return fnmatch.fnmatch(target, self.matcher)


@dataclass
class HookResult:
    hook_id: str
    success: bool
    output: str = ""
    blocked: bool = False
    reason: str = ""
    error: str = ""


@dataclass
class HookExecutionResult:
    results: list[HookResult] = field(default_factory=list)
    blocked: bool = False
    reason: str = ""

    @property
    def output(self) -> str:
        return "\n".join(r.output for r in self.results if r.output)


class HookExecutor:
    """Executes hooks for lifecycle events."""

    def __init__(self):
        self._hooks: dict[str, list[HookConfig]] = {}

    def add_hook(self, config: HookConfig) -> None:
        event = config.event
        if event not in self._hooks:
            self._hooks[event] = []
        self._hooks[event].append(config)
        self._hooks[event].sort(key=lambda h: -h.priority)

    def remove_hook(self, hook_id: str) -> None:
        for event in self._hooks:
            self._hooks[event] = [h for h in self._hooks[event] if h.id != hook_id]

    def list_hooks(self, event: str | None = None) -> list[HookConfig]:
        if event:
            return list(self._hooks.get(event, []))
        return [h for hooks in self._hooks.values() for h in hooks]

    def clear_hooks(self) -> None:
        """Remove all registered hooks (used by the loader before re-registering)."""
        self._hooks.clear()

    async def execute(
        self,
        event: str | HookEvent,
        payload: dict[str, Any] | None = None,
    ) -> HookExecutionResult:
        event_str = event.value if isinstance(event, HookEvent) else str(event)
        hooks = self._hooks.get(event_str, [])
        if not hooks:
            return HookExecutionResult()

        payload = payload or {}
        target = payload.get("tool_name") or payload.get("prompt") or payload.get("target")

        results: list[HookResult] = []
        blocked = False
        block_reason = ""

        for hook in hooks:
            if not hook.enabled:
                continue
            if not hook.matches(target):
                continue
            try:
                result = await self._execute_hook(hook, payload)
                results.append(result)
                if not result.success and hook.block_on_failure:
                    blocked = True
                    block_reason = result.reason or result.error or f"Hook '{hook.id}' blocked"
                    break
            except Exception as e:
                logger.warning("Hook '%s' exception: %s", hook.id, e)
                result = HookResult(hook_id=hook.id, success=False, error=str(e))
                results.append(result)
                if hook.block_on_failure:
                    blocked = True
                    block_reason = str(e)
                    break

        return HookExecutionResult(results=results, blocked=blocked, reason=block_reason)

    async def _execute_hook(self, hook: HookConfig, payload: dict[str, Any]) -> HookResult:
        if hook.type == HookType.COMMAND.value:
            return await self._execute_command_hook(hook, payload)
        elif hook.type == HookType.HTTP.value:
            return await self._execute_http_hook(hook, payload)
        elif hook.type == HookType.PROMPT.value:
            return await self._execute_prompt_hook(hook, payload)
        elif hook.type == HookType.AGENT.value:
            return await self._execute_agent_hook(hook, payload)
        return HookResult(hook_id=hook.id, success=False, error=f"Unknown hook type: {hook.type}")

    async def _execute_command_hook(self, hook: HookConfig, payload: dict[str, Any]) -> HookResult:
        if not hook.command:
            return HookResult(hook_id=hook.id, success=False, error="No command")
        cmd = hook.command.replace("$ARGUMENTS", json.dumps(payload))
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd.split(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=hook.timeout)
            output = stdout.decode("utf-8", errors="replace").strip()
            if stderr:
                output += "\n" + stderr.decode("utf-8", errors="replace").strip()
            success = proc.returncode == 0
            return HookResult(hook_id=hook.id, success=success, output=output,
                              reason="" if success else f"Exit code {proc.returncode}")
        except asyncio.TimeoutError:
            return HookResult(hook_id=hook.id, success=False, error=f"Timeout {hook.timeout}s")
        except Exception as e:
            return HookResult(hook_id=hook.id, success=False, error=str(e))

    async def _execute_http_hook(self, hook: HookConfig, payload: dict[str, Any]) -> HookResult:
        if not hook.url:
            return HookResult(hook_id=hook.id, success=False, error="No URL")
        try:
            async with httpx.AsyncClient(timeout=hook.timeout) as client:
                headers = hook.headers or {"Content-Type": "application/json"}
                method = hook.method.upper()
                if method == "POST":
                    resp = await client.post(hook.url, json=payload, headers=headers)
                elif method == "GET":
                    resp = await client.get(hook.url, params=payload, headers=headers)
                else:
                    resp = await client.request(method, hook.url, json=payload, headers=headers)
                success = 200 <= resp.status_code < 300
                return HookResult(hook_id=hook.id, success=success, output=resp.text[:2000],
                                  reason="" if success else f"HTTP {resp.status_code}")
        except Exception as e:
            return HookResult(hook_id=hook.id, success=False, error=str(e))

    async def _execute_prompt_hook(self, hook: HookConfig, payload: dict[str, Any]) -> HookResult:
        if not hook.prompt:
            return HookResult(hook_id=hook.id, success=False, error="No prompt")
        try:
            from app.services.llm_service import call_llm
            full_prompt = hook.prompt.replace("$ARGUMENTS", json.dumps(payload, default=str))
            result = await call_llm(
                messages=[
                    {"role": "system", "content": "You are a validation agent. Respond with 'PASS' or 'FAIL' and a brief reason."},
                    {"role": "user", "content": full_prompt},
                ],
                temperature=0.1,
            )
            response = result.get("response", "").strip()
            success = response.upper().startswith("PASS")
            return HookResult(hook_id=hook.id, success=success, output=response,
                              reason="" if success else f"Validation failed: {response}")
        except Exception as e:
            return HookResult(hook_id=hook.id, success=False, error=str(e))

    async def _execute_agent_hook(self, hook: HookConfig, payload: dict[str, Any]) -> HookResult:
        # Agent hooks are like prompt hooks but with more context
        return await self._execute_prompt_hook(hook, payload)


# Singleton
_executor: HookExecutor | None = None


def get_hook_executor() -> HookExecutor:
    global _executor
    if _executor is None:
        _executor = HookExecutor()
    return _executor


__all__ = [
    "HookEvent", "HookType", "HookConfig", "HookResult",
    "HookExecutionResult", "HookExecutor", "get_hook_executor",
]
