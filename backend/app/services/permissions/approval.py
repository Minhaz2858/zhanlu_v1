"""Tool risk declarations + approval grants — the approval primitive.

Design rules (see docs/plans/2026-09-18-agent-harness-migration.md §2.4):

* A tool declares its OWN risk at registration (``ToolRegistry.register(
  risk=..., dangerous_actions=...)``).  Risk is not inferred from the user's
  text, and it is not a central judgement about intent — it is static security
  metadata, which the migration spec explicitly keeps.
* Only *irreversible deletions of user-facing durable resources* require
  confirmation.  Agent scratch state (``memory``, ``todo``, ``checkpoint_manager``)
  must NEVER be gated: gating it paused every turn in ``awaiting_approval``
  and froze the chat, which is the failure mode that produced the current
  over-broad ``WRITE_TOOLS`` list.  See ``NEVER_GATE``.
* Approving a call must never widen permissions globally.  It is expressed as
  an :class:`ApprovalContext` that covers exactly one tool call.
* Standing grants let one approval cover a whole conversation, so the user is
  not re-prompted for the same class of action every turn.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

logger = logging.getLogger(__name__)

RiskLevel = Literal["read", "write", "destructive", "external"]

# Grant scopes.
#   "once"         — covers the single tool call that was approved (no storage)
#   "conversation" — persisted in ``conversation.metadata_["_approval_grants"]``
#                    and consumed by :func:`has_standing_grant`.
# Agent-wide (permanent) grants are deliberately NOT implemented yet; they need
# a durable store on the agent definition, see the migration doc's follow-ups.
GrantScope = Literal["once", "conversation"]

APPROVAL_GRANTS_KEY = "_approval_grants"

# Actions whose effect cannot be undone from inside the product.
DESTRUCTIVE_ACTIONS: frozenset[str] = frozenset({
    "delete",
    "drop",
    "revoke",
    "purge",
    "unbind",
    "destroy",
})

# Tools that must never require confirmation, whatever they declare.  These
# mutate agent-internal scratch state only; gating them pauses the turn with
# nothing the user could sensibly approve.
#
# mode_get / mode_set are here for a sharper reason: they read and change the
# agent's own operating mode, so gating them in plan mode would trap the model
# in plan mode with no way to switch back to execute.
NEVER_GATE: frozenset[str] = frozenset({
    "todo",
    "memory",
    "checkpoint_manager",
    "mode_get",
    "mode_set",
})

# Fallback declarations for handlers that perform a destructive action but have
# not yet declared ``dangerous_actions=`` at registration.  Registry
# declarations always win; this table exists so gating is not silently absent
# while registrations are migrated, and should shrink to empty over time.
FALLBACK_DANGEROUS_ACTIONS: dict[str, frozenset[str]] = {
    # Deletions of user-facing durable resources.  Preferred form is the tool
    # declaring ``dangerous_actions=`` at registration; entries here exist so
    # gating is not silently absent for handlers not yet migrated.
    "cronjob": frozenset({"delete"}),
    "kanban": frozenset({"delete"}),
    "update_env_config": frozenset({"delete"}),
}

# Deliberately NOT gated, and why (do not "fix" these by adding them):
#   memory / todo / checkpoint_manager — agent-internal scratch state. Gating
#     them paused every turn in awaiting_approval and froze the chat.
#   env_passthrough("remove") — removes a key from the *current call's* env
#     passthrough, not a durable resource.
#   feishu_drive — its schema advertises a ``delete`` action but the handler has
#     no branch for it, so there is nothing to approve.


@dataclass(frozen=True)
class ToolRisk:
    """Resolved risk for one concrete tool call."""
    tool_name: str
    level: RiskLevel = "read"
    action: str = ""
    destructive: bool = False
    source: str = "default"   # registry | fallback | write_tools | default


def _registry_risk(tool_name: str) -> tuple[RiskLevel | None, frozenset[str]]:
    """Read ``risk`` / ``dangerous_actions`` off the tool's registry entry.

    Imported lazily: ``permissions`` is imported by ``agent_tools``, which
    imports ``tool_registry``, so a module-level import risks a cycle.
    """
    try:
        from app.services.tool_registry import ToolRegistry
        entry = ToolRegistry.get_instance().get_entry(tool_name)
    except Exception as err:  # pragma: no cover - defensive
        logger.debug("risk: registry lookup failed for %s: %s", tool_name, err)
        return None, frozenset()
    if entry is None:
        return None, frozenset()
    level = getattr(entry, "risk", None)
    if level not in ("read", "write", "destructive", "external"):
        level = None
    actions = getattr(entry, "dangerous_actions", None) or ()
    return level, frozenset(a for a in actions if isinstance(a, str))


def _call_action(args: dict[str, Any] | None) -> str:
    """The multiplexed action this call targets, e.g. ``delete``."""
    if not isinstance(args, dict):
        return ""
    for key in ("action", "operation", "op", "command"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return ""


def resolve_risk(tool_name: str, args: dict[str, Any] | None = None) -> ToolRisk:
    """Resolve the risk of one concrete tool call.

    Precedence: tool's own registry declaration → fallback table → legacy
    ``WRITE_TOOLS`` membership → read-only.

    A destructive action is gated even when its tool is otherwise a plain read
    tool, which is the whole point: ``cronjob`` is not in ``WRITE_TOOLS``, yet
    ``cronjob(action="delete")`` destroys a scheduled job.
    """
    if tool_name in NEVER_GATE:
        return ToolRisk(tool_name=tool_name, level="write", source="never_gate")

    action = _call_action(args)
    level, declared_actions = _registry_risk(tool_name)
    source = "registry" if (level or declared_actions) else ""

    if not declared_actions:
        declared_actions = FALLBACK_DANGEROUS_ACTIONS.get(tool_name, frozenset())
        if declared_actions and not source:
            source = "fallback"

    if declared_actions:
        # An action-scoped destructive declaration only fires on that action;
        # a bare declaration (no actions listed) is treated as write-level.
        if action and action in declared_actions:
            return ToolRisk(
                tool_name=tool_name, level="destructive", action=action,
                destructive=True, source=source or "fallback",
            )
        if level == "destructive" and not action:
            return ToolRisk(
                tool_name=tool_name, level="destructive", action=action,
                destructive=True, source=source,
            )
    elif level == "destructive":
        return ToolRisk(
            tool_name=tool_name, level="destructive", action=action,
            destructive=True, source=source,
        )

    if level:
        return ToolRisk(tool_name=tool_name, level=level, action=action, source=source)

    if tool_name in _legacy_write_tools():
        return ToolRisk(
            tool_name=tool_name, level="write", action=action, source="write_tools",
        )

    return ToolRisk(tool_name=tool_name, level="read", action=action)


def _legacy_write_tools() -> frozenset[str]:
    try:
        from app.services.permissions import WRITE_TOOLS
        return WRITE_TOOLS
    except Exception:  # pragma: no cover - defensive
        return frozenset()


# ---------------------------------------------------------------------------
# Approval context — approval covers exactly ONE call, never a global mode
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ApprovalContext:
    """Proof that a specific tool call was already approved by the user.

    Produced server-side by the resume path from the persisted
    ``ApprovalRequest`` (status ``approved``) and handed to ``execute_tool`` via
    its ``context`` dict.  It deliberately bypasses *confirmation* only — never
    an authorization deny (``denied_tools`` / ``allowed_tools`` / plan mode).

    The approval is bound to the **exact call** (tool name + canonical argument
    digest) rather than to a session or a mode.  This matters because a tool can
    itself invoke other tools (``execute_code`` does): a nested gated call has
    different arguments, so it does NOT inherit the approval and still prompts.
    """
    approved_tool_name: str = ""
    approved_args_digest: str = ""

    @property
    def active(self) -> bool:
        return bool(self.approved_tool_name and self.approved_args_digest)

    def covers(self, tool_name: str, args: dict[str, Any] | None) -> bool:
        if not self.active:
            return False
        if self.approved_tool_name != tool_name:
            return False
        return self.approved_args_digest == arguments_digest(args)


def arguments_digest(args: dict[str, Any] | None) -> str:
    """Stable digest of a tool call's arguments.

    Key order does not matter and non-JSON values fall back to ``repr`` so the
    digest is always computable.  ``_tool_call_id``-style private keys are
    ignored so a caller cannot perturb the digest.
    """
    payload = {
        k: v for k, v in (args or {}).items()
        if isinstance(k, str) and not k.startswith("_")
    }
    try:
        blob = json.dumps(payload, sort_keys=True, default=repr, ensure_ascii=False)
    except Exception:  # pragma: no cover - defensive
        blob = repr(sorted(payload.items(), key=lambda kv: kv[0]))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def make_approval_context(
    tool_name: str,
    args: dict[str, Any] | None,
) -> ApprovalContext:
    """Build the context the resume path passes for an approved tool call."""
    return ApprovalContext(
        approved_tool_name=tool_name or "",
        approved_args_digest=arguments_digest(args),
    )


def approval_context_from_context(context: dict[str, Any] | None) -> ApprovalContext:
    """Build an :class:`ApprovalContext` from an ``execute_tool`` context dict.

    Accepts either a nested ``approval_context`` (an :class:`ApprovalContext`
    or its plain dict form) or the flat ``approved_tool_name`` /
    ``approved_args_digest`` keys.  Only server-built context dicts carry these;
    the model cannot set them.
    """
    if not isinstance(context, dict):
        return ApprovalContext()
    raw = context.get("approval_context")
    if isinstance(raw, ApprovalContext):
        return raw
    if isinstance(raw, dict):
        return ApprovalContext(
            approved_tool_name=str(raw.get("approved_tool_name") or ""),
            approved_args_digest=str(raw.get("approved_args_digest") or ""),
        )
    return ApprovalContext(
        approved_tool_name=str(context.get("approved_tool_name") or ""),
        approved_args_digest=str(context.get("approved_args_digest") or ""),
    )


# ---------------------------------------------------------------------------
# Standing grants
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StandingGrant:
    tool_name: str
    action: str = ""            # "" = any action of this tool
    scope: GrantScope = "conversation"
    granted_by: str = ""
    granted_at: str = ""

    def matches(self, tool_name: str, action: str) -> bool:
        if self.tool_name != tool_name:
            return False
        if action and self.action and self.action != action:
            return False
        return True


def make_grant(
    tool_name: str,
    action: str = "",
    scope: GrantScope = "conversation",
    granted_by: str = "",
) -> dict[str, Any]:
    """Build the dict persisted under ``conversation.metadata_[APPROVAL_GRANTS_KEY]``."""
    return {
        "tool_name": tool_name,
        "action": (action or "").lower(),
        "scope": scope,
        "granted_by": granted_by,
        "granted_at": datetime.now(timezone.utc).isoformat(),
    }


def parse_grants(conversation_metadata: dict[str, Any] | None) -> list[StandingGrant]:
    """Read standing grants out of conversation metadata. Never raises."""
    if not isinstance(conversation_metadata, dict):
        return []
    raw = conversation_metadata.get(APPROVAL_GRANTS_KEY)
    if not isinstance(raw, list):
        return []
    grants: list[StandingGrant] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        tool_name = item.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name:
            continue
        scope = item.get("scope")
        grants.append(StandingGrant(
            tool_name=tool_name,
            action=str(item.get("action") or "").lower(),
            scope=scope if scope in ("once", "conversation") else "conversation",
            granted_by=str(item.get("granted_by") or ""),
            granted_at=str(item.get("granted_at") or ""),
        ))
    return grants


def has_standing_grant(
    tool_name: str,
    args: dict[str, Any] | None,
    conversation_metadata: dict[str, Any] | None,
) -> bool:
    """True when a stored grant already covers this call."""
    if tool_name in NEVER_GATE:
        return False
    action = _call_action(args)
    return any(g.matches(tool_name, action) for g in parse_grants(conversation_metadata))


def grant_for(tool_name: str, args: dict[str, Any] | None) -> dict[str, Any]:
    """The grant a user creates by choosing 'allow for this conversation'."""
    return make_grant(tool_name=tool_name, action=_call_action(args))


__all__ = [
    "RiskLevel",
    "GrantScope",
    "APPROVAL_GRANTS_KEY",
    "DESTRUCTIVE_ACTIONS",
    "NEVER_GATE",
    "FALLBACK_DANGEROUS_ACTIONS",
    "ToolRisk",
    "resolve_risk",
    "ApprovalContext",
    "make_approval_context",
    "approval_context_from_context",
    "arguments_digest",
    "StandingGrant",
    "make_grant",
    "grant_for",
    "parse_grants",
    "has_standing_grant",
]
