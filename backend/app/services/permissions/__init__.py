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

from app.services.permissions.approval import (
    APPROVAL_GRANTS_KEY,
    ApprovalContext,
    approval_context_from_context,
    grant_for,
    has_standing_grant,
    make_approval_context,
    make_grant,
    parse_grants,
    resolve_risk,
)

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
    # Phase 1b: the resolved risk for this concrete call, so callers (and the
    # approval record) can say *what* is being approved, not just that
    # something is. ``risk`` is the level, ``action`` the multiplexed action.
    risk: str = ""
    action: str = ""


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
        approval_context: Any = None,
    ) -> PermissionCheckResult:
        """Check if a tool call is allowed.

        Args:
            tool_name: The name of the tool to check.
            args: The tool arguments (for path extraction).
            agent_name: Optional agent name for AgentDefinition lookup.
            conversation_metadata: Optional conversation metadata dict.
                If it contains ``permission_mode``, that mode overrides
                the agent-level and default config modes (conversation-level
                permission override). If it contains standing approval
                grants (``_approval_grants``), those suppress re-prompting
                for the actions they cover.
            approval_context: Optional
                :class:`~app.services.permissions.approval.ApprovalContext`
                proving that THIS tool call was already approved by the user.
                It suppresses *confirmation* for that one call only — it never
                overrides an authorization deny.

        Returns:
            PermissionCheckResult with allowed=True/False and reason.
        """
        args = args or {}

        # Try to get permission config from AgentDefinition, with
        # conversation-level override taking highest priority.
        config, authz_ok = self._get_effective_config(agent_name, conversation_metadata)
        if not authz_ok:
            # Authorization could not be established (see _get_effective_config).
            # Fail CLOSED: an agent-scoped ``allowed_tools`` posture we cannot
            # read must not silently degrade into "every tool allowed".
            return PermissionCheckResult(
                allowed=False,
                risk="unknown",
                reason=(
                    f"Authorization for agent '{agent_name}' could not be "
                    "resolved; refusing to run tool "
                    f"'{tool_name}' rather than running it unchecked"
                ),
            )

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

        # 5. Confirmation gate. Risk is declared by the TOOL (registry
        #    metadata), not inferred from the user's text — see
        #    app/services/permissions/approval.py.
        risk = resolve_risk(tool_name, args)

        # 5a. Already approved, this exact call? Then no second prompt.
        if risk.destructive and approval_context is not None:
            if getattr(approval_context, "covers", None) and approval_context.covers(tool_name, args):
                return PermissionCheckResult(
                    allowed=True, risk=risk.level, action=risk.action,
                    reason=f"Tool call already approved (risk: {risk.level})",
                )

        # 5b. A standing grant for this tool+action already exists in this
        #     conversation ("allow for this conversation").
        if risk.destructive and has_standing_grant(tool_name, args, conversation_metadata):
            return PermissionCheckResult(
                allowed=True, risk=risk.level, action=risk.action,
                reason=f"Covered by an existing approval grant (risk: {risk.level})",
            )

        # 5c. Destructive action on a user-facing resource — always confirm,
        #     even for tools the legacy WRITE_TOOLS list does not know.
        if risk.destructive:
            what = f"'{tool_name}' action '{risk.action}'" if risk.action else f"'{tool_name}'"
            return PermissionCheckResult(
                allowed=True,
                requires_confirmation=True,
                risk=risk.level,
                action=risk.action,
                reason=f"Destructive operation {what} requires user confirmation",
            )

        # 5d. Default mode requires confirmation for write operations
        if config.mode == "default" and tool_name in WRITE_TOOLS:
            return PermissionCheckResult(
                allowed=True,
                requires_confirmation=True,
                risk=risk.level or "write",
                reason=f"Write operation '{tool_name}' requires user confirmation in Default mode",
            )

        if risk.source in ("registry", "fallback") and risk.level == "write":
            # Tool declared itself as write-level but is not in WRITE_TOOLS.
            # Do not change today's behaviour (that would be a silent
            # tightening); record the level so follow-up phases can migrate.
            return PermissionCheckResult(allowed=True, risk=risk.level, action=risk.action)

        return PermissionCheckResult(allowed=True, risk=risk.level, action=risk.action)

    def _get_effective_config(
        self,
        agent_name: str | None,
        conversation_metadata: dict[str, Any] | None = None,
    ) -> tuple[PermissionConfig, bool]:
        """Get the effective permission config, merging AgentDefinition if available.

        Priority (highest wins):
        1. Conversation-level ``permission_mode`` from ``conversation_metadata``
        2. AgentDefinition ``permission_mode``
        3. ``self.config.mode`` (default)

        Returns ``(config, authorization_resolved)``.  The second element is
        False when the agent's definition could not be read at all, which the
        caller must treat as a fail-CLOSED condition: ``allowed_tools`` is an
        authorization posture (spec §2.2), so degrading it to ``None`` — "all
        tools allowed" — on a transient lookup error would silently widen the
        agent's reach.  ``agent_name=None`` is not a failure (there is simply no
        agent-scoped posture to apply).
        """
        # Start with the base config
        base_mode = self.config.mode

        # Merge AgentDefinition if available
        agent_mode = base_mode
        agent_denied_tools: list[str] = []
        agent_allowed_tools: list[str] | None = None
        authorization_resolved = True
        if agent_name:
            try:
                from app.services.agent_definitions import get_agent_definition
                agent_def = get_agent_definition(agent_name)
            except Exception as err:
                # Never silently swallow this: it is an authorization lookup.
                logger.error(
                    "Authorization lookup failed for agent '%s'; failing closed "
                    "for this call: %s", agent_name, err,
                )
                agent_def = None
                authorization_resolved = False
            if agent_def is not None:
                try:
                    agent_mode = (
                        agent_def.permission_mode
                        if agent_def.permission_mode in ("default", "plan", "full_auto")
                        else base_mode
                    )
                    agent_denied_tools = list(agent_def.denied_tools)
                    agent_allowed_tools = agent_def.tools
                except Exception as err:
                    logger.error(
                        "Authorization fields unreadable for agent '%s'; failing "
                        "closed for this call: %s", agent_name, err,
                    )
                    authorization_resolved = False

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
        return merged, authorization_resolved

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
    approval_context: ApprovalContext | None = None,
) -> PermissionCheckResult:
    """Convenience function to check tool permissions."""
    return get_permission_checker().check_tool(
        tool_name, args, agent_name,
        conversation_metadata=conversation_metadata,
        approval_context=approval_context,
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
    # Phase 1b — approval primitive
    "ApprovalContext",
    "APPROVAL_GRANTS_KEY",
    "approval_context_from_context",
    "make_approval_context",
    "has_standing_grant",
    "make_grant",
    "grant_for",
    "parse_grants",
    "resolve_risk",
]
