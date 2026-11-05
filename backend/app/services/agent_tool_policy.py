"""Effective agent tool policy — invocation-boundary enforcement helper.

Milestone acceptance finding (2026-11-01): toolset assembly alone is NOT a
security boundary. A model can guess a tool name it was never shown, so the
AgentApp's ``strict_tools`` allowlist and ``disabled_tools`` hard deny must
be re-checked inside ``execute_tool`` (the single governed invocation path)
and ``search_tools`` must never advertise forbidden tools to a caller.

Policy resolution is fail-open ONLY on lookup errors (DB transient) with a
warning — consistent with the governance layer's advisory-on-error contract;
a LOADED policy is always enforced fail-closed.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Always-on child tools (delegate_tool assembly contract): available under
# strict mode unless explicitly disabled.
ALWAYS_ON_CHILD_TOOLS = frozenset({"provide_evidence", "request_evidence"})


def load_tool_policy(db: "Session | None", agent_app_id: str | None) -> dict | None:
    """Load the caller AgentApp's effective tool policy.

    Returns ``None`` when there is no caller AgentApp or the row cannot be
    loaded (fail-open on lookup errors, with a warning). Never raises.
    """
    if not agent_app_id or db is None:
        return None
    try:
        from app.models.agent_app import AgentApp

        row = db.query(AgentApp).filter(AgentApp.id == agent_app_id).first()
        if row is None:
            return None
        tc = row.tool_config or {}
        return {
            "strict": bool(tc.get("strict_tools", False)),
            "enabled": set(tc.get("enabled_tools") or []),
            "disabled": set(tc.get("disabled_tools") or []),
        }
    except Exception as exc:  # noqa: BLE001 — advisory on lookup errors
        logger.warning(
            "agent_tool_policy: lookup failed for app %s (fail-open): %s",
            agent_app_id, exc,
        )
        return None


def policy_denies(policy: dict | None, tool_name: str) -> str | None:
    """Return a denial reason when the policy forbids ``tool_name``, else None.

    Order matters: the hard deny (disabled_tools) wins even over the
    always-on evidence contract; the strict allowlist admits enabled_tools
    plus the always-on evidence tools.
    """
    if not policy:
        return None
    if tool_name in policy["disabled"]:
        return (
            f"Tool '{tool_name}' is disabled by this agent's tool policy "
            "(disabled_tools)"
        )
    if policy["strict"] and tool_name not in policy["enabled"] \
            and tool_name not in ALWAYS_ON_CHILD_TOOLS:
        return (
            f"Tool '{tool_name}' is outside this agent's strict tool "
            "allowlist (strict_tools)"
        )
    return None


def policy_filters_catalog(
    policy: dict | None, matches: list[dict],
) -> list[dict]:
    """Drop catalog cards the caller's policy forbids (discovery honesty)."""
    if not policy:
        return matches
    out = []
    for m in matches:
        name = (m or {}).get("name")
        if name and not policy_denies(policy, name):
            out.append(m)
    return out
