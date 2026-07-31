"""Multi-layer permission system for Zhanlu — adapted from OpenHarness.

Three permission modes:
- Default: read/write operations require user confirmation (interactive)
- Plan: blocks all write operations (safe review/planning mode)
- Full Auto: allows everything

Plus:
- Path-level rules (glob patterns: allow/deny specific paths)
- Command blacklist (deny dangerous bash commands)
- Sensitive path protection (SSH keys, AWS/GCP/Azure/Docker/K8s credentials)
"""

from __future__ import annotations

import fnmatch
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

PermissionMode = Literal["default", "plan", "full_auto"]

# ---------------------------------------------------------------------------
# Sensitive paths — always protected, cannot be overridden
# ---------------------------------------------------------------------------

SENSITIVE_PATH_PATTERNS: list[str] = [
    "*/.ssh/*",
    "*/.ssh",
    "*/.aws/credentials",
    "*/.aws/config",
    "*/.config/gcloud/*",
    "*/.azure/*",
    "*/.docker/config.json",
    "*/.kube/config",
    "*/.netrc",
    "*/.env",
    "*/.env.local",
    "*/.env.production",
    "*/.git/credentials",
    "*/.gitconfig",
    "*/.npmrc",
    "*/.pypirc",
    "*/.gnupg/*",
]

# ---------------------------------------------------------------------------
# Dangerous commands — always blocked in plan mode, configurable in default
# ---------------------------------------------------------------------------

DANGEROUS_COMMANDS: list[str] = [
    "rm -rf /",
    "rm -rf ~",
    "rm -rf /*",
    "rm -rf $HOME",
    "mkfs",
    "dd if=/dev/zero of=",
    ":(){ :|:& };:",
    "chmod -R 777 /",
    "chown -R",
    "kill -9 -1",
    "shutdown",
    "reboot",
    "halt",
    "poweroff",
    "init 0",
    "init 6",
    "> /dev/sda",
    "mv / /dev/null",
]

# Tools that perform write operations
WRITE_TOOLS: frozenset[str] = frozenset({
    "write_file",
    "create_agent",
    "update_agent",
    "create_skill",
    "update_skill",
    "create_automation",
    "update_automation",
    "execute_code",
    "image_generation",
    "delegate_task",
    # NOTE: "todo" and "memory" are intentionally NOT write tools. They are
    # internal agent-state tools (per-conversation task list, agent memory)
    # that modify no user-facing resources. Gating them behind interactive
    # confirmation paused every turn in "awaiting_approval" with no UI to
    # resume, freezing the chat at the "Updating the task list" step.
})

# Tools that perform read-only operations
READ_TOOLS: frozenset[str] = frozenset({
    "read_file",
    "web_search",
    "web_extract",
    "list_tools",
    "list_market_agents",
    "list_knowledge_bases",
})


@dataclass
class PathRule:
    """A glob-based path rule."""
    pattern: str
    allow: bool


@dataclass
class PermissionConfig:
    """Permission configuration for an agent or session."""
    mode: PermissionMode = "default"
    path_rules: list[PathRule] = field(default_factory=list)
    denied_commands: list[str] = field(default_factory=list)
    denied_tools: list[str] = field(default_factory=list)
    allowed_tools: list[str] | None = None  # None = all allowed


@dataclass
class PermissionCheckResult:
    """Result of a permission check."""
    allowed: bool
    reason: str = ""
    requires_confirmation: bool = False


class PermissionChecker:
    """Multi-layer permission checker.

    Check order:
    1. Tool-level: denied_tools / allowed_tools
    2. Mode-level: Plan mode blocks write tools
    3. Command-level: denied_commands / dangerous commands
    4. Path-level: path_rules + sensitive paths
    """

    def __init__(self, config: PermissionConfig | None = None):
        self.config = config or PermissionConfig()

    def check_tool(
        self,
        tool_name: str,
        args: dict[str, Any] | None = None,
        agent_name: str | None = None,
        conversation_metadata: dict[str, Any] | None = None,
    ) -> PermissionCheckResult:
        """Check if a tool call is allowed.

        Args:
            tool_name: The name of the tool to check.
            args: The tool arguments (for path extraction).
            agent_name: Optional agent name for AgentDefinition lookup.
            conversation_metadata: Optional conversation metadata dict.
                If it contains ``permission_mode``, that mode overrides
                the agent-level and default config modes (conversation-level
                permission override).

        Returns:
            PermissionCheckResult with allowed=True/False and reason.
        """
        args = args or {}

        # Try to get permission config from AgentDefinition, with
        # conversation-level override taking highest priority.
        config = self._get_effective_config(agent_name, conversation_metadata)

        # 1. Tool-level checks
        if tool_name in config.denied_tools:
            return PermissionCheckResult(
                allowed=False,
                reason=f"Tool '{tool_name}' is denied by configuration",
            )

        if config.allowed_tools is not None and tool_name not in config.allowed_tools:
            return PermissionCheckResult(
                allowed=False,
                reason=f"Tool '{tool_name}' is not in the allowed tools list",
            )

        # 2. Mode-level checks
        if config.mode == "plan" and tool_name in WRITE_TOOLS:
            return PermissionCheckResult(
                allowed=False,
                reason=f"Write operations blocked in Plan mode (tool: {tool_name})",
            )

        # 3. Command-level checks (for execute_code / bash)
        if tool_name in ("execute_code", "bash"):
            code = args.get("code", "") or args.get("command", "")
            if code:
                cmd_check = self._check_command(code, config)
                if not cmd_check.allowed:
                    return cmd_check

        # 4. Path-level checks (for read_file / write_file)
        if tool_name in ("read_file", "write_file"):
            file_path = args.get("path", "") or args.get("file_path", "")
            if file_path:
                path_check = self._check_path(file_path, config, is_write=(tool_name == "write_file"))
                if not path_check.allowed:
                    return path_check

        # 5. Default mode requires confirmation for write operations
        if config.mode == "default" and tool_name in WRITE_TOOLS:
            return PermissionCheckResult(
                allowed=True,
                requires_confirmation=True,
                reason=f"Write operation '{tool_name}' requires user confirmation in Default mode",
            )

        return PermissionCheckResult(allowed=True)

    def _get_effective_config(
        self,
        agent_name: str | None,
        conversation_metadata: dict[str, Any] | None = None,
    ) -> PermissionConfig:
        """Get the effective permission config, merging AgentDefinition if available.

        Priority (highest wins):
        1. Conversation-level ``permission_mode`` from ``conversation_metadata``
        2. AgentDefinition ``permission_mode``
        3. ``self.config.mode`` (default)
        """
        # Start with the base config
        base_mode = self.config.mode

        # Merge AgentDefinition if available
        agent_mode = base_mode
        agent_denied_tools: list[str] = []
        agent_allowed_tools: list[str] | None = None
        if agent_name:
            try:
                from app.services.agent_definitions import get_agent_definition
                agent_def = get_agent_definition(agent_name)
                if agent_def:
                    agent_mode = (
                        agent_def.permission_mode
                        if agent_def.permission_mode in ("default", "plan", "full_auto")
                        else base_mode
                    )
                    agent_denied_tools = list(agent_def.denied_tools)
                    agent_allowed_tools = agent_def.tools
            except Exception:
                pass

        # Conversation-level override takes highest priority
        conv_mode = agent_mode
        if conversation_metadata and isinstance(conversation_metadata, dict):
            cv_mode = conversation_metadata.get("permission_mode")
            if cv_mode in ("default", "plan", "full_auto"):
                conv_mode = cv_mode

        merged = PermissionConfig(
            mode=conv_mode,
            path_rules=list(self.config.path_rules),
            denied_commands=list(self.config.denied_commands),
            denied_tools=list(set(self.config.denied_tools + agent_denied_tools)),
            allowed_tools=agent_allowed_tools if agent_allowed_tools is not None else self.config.allowed_tools,
        )
        return merged

    def _check_command(self, code: str, config: PermissionConfig) -> PermissionCheckResult:
        """Check if a command contains dangerous patterns."""
        code_lower = code.lower()

        # Check against dangerous commands (always blocked in plan mode)
        if config.mode == "plan":
            for dangerous in DANGEROUS_COMMANDS:
                if dangerous.lower() in code_lower:
                    return PermissionCheckResult(
                        allowed=False,
                        reason=f"Dangerous command blocked in Plan mode: {dangerous}",
                    )

        # Check against denied_commands
        for denied in config.denied_commands:
            if denied.lower() in code_lower:
                return PermissionCheckResult(
                    allowed=False,
                    reason=f"Command blocked by denied_commands: {denied}",
                )

        # Always block the most dangerous commands
        for dangerous in DANGEROUS_COMMANDS:
            if dangerous.lower() in code_lower:
                return PermissionCheckResult(
                    allowed=False,
                    reason=f"Dangerous command blocked: {dangerous}",
                )

        return PermissionCheckResult(allowed=True)

    def _check_path(self, path: str, config: PermissionConfig, is_write: bool) -> PermissionCheckResult:
        """Check if a file path is allowed."""
        # Normalize path
        expanded = os.path.expanduser(path)
        normalized = str(Path(expanded).resolve())

        # 1. Always block sensitive paths (cannot be overridden)
        for pattern in SENSITIVE_PATH_PATTERNS:
            if fnmatch.fnmatch(normalized, pattern) or fnmatch.fnmatch(path, pattern):
                return PermissionCheckResult(
                    allowed=False,
                    reason=f"Access to sensitive path blocked: {pattern}",
                )

        # 2. Plan mode blocks all write paths
        if config.mode == "plan" and is_write:
            return PermissionCheckResult(
                allowed=False,
                reason=f"Write to path blocked in Plan mode: {path}",
            )

        # 3. Check path rules (in order, first match wins)
        for rule in config.path_rules:
            if fnmatch.fnmatch(normalized, rule.pattern) or fnmatch.fnmatch(path, rule.pattern):
                if not rule.allow:
                    return PermissionCheckResult(
                        allowed=False,
                        reason=f"Path blocked by rule: {rule.pattern}",
                    )
                return PermissionCheckResult(allowed=True)

        return PermissionCheckResult(allowed=True)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_checker: PermissionChecker | None = None


def get_permission_checker() -> PermissionChecker:
    """Get the singleton PermissionChecker instance."""
    global _checker
    if _checker is None:
        _checker = PermissionChecker()
    return _checker


def check_permission(
    tool_name: str,
    args: dict[str, Any] | None = None,
    agent_name: str | None = None,
    conversation_metadata: dict[str, Any] | None = None,
) -> PermissionCheckResult:
    """Convenience function to check tool permissions."""
    return get_permission_checker().check_tool(
        tool_name, args, agent_name, conversation_metadata=conversation_metadata,
    )


__all__ = [
    "PermissionMode",
    "PermissionConfig",
    "PermissionCheckResult",
    "PathRule",
    "PermissionChecker",
    "SENSITIVE_PATH_PATTERNS",
    "DANGEROUS_COMMANDS",
    "WRITE_TOOLS",
    "READ_TOOLS",
    "get_permission_checker",
    "check_permission",
]
