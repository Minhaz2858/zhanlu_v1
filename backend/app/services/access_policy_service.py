"""Resource access policy resolution.

Turns the raw ``ResourceAccessPolicy`` rows for a (user, resource) tuple into a
single ``ResolvedPolicy`` object that each enforcement layer can consume.

Resolution model (specificity, most specific wins):

    (kb_id + table)  >  (kb_id only)  >  (table only)  >  (global)

- ``deny`` hides the target (KB or table).
- ``allow`` explicitly permits the target (overrides a coarser deny).
- ``allow_columns`` permits only the listed columns of a specific table.

Default-allow: when no policies exist, the shared user sees every KB/table the
share grants them.  Owners and admins always get a full-access policy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.models.resource_access_policy import ResourceAccessPolicy

logger = logging.getLogger(__name__)

# Valid modes.
MODE_ALLOW = "allow"
MODE_DENY = "deny"
MODE_ALLOW_COLUMNS = "allow_columns"


@dataclass
class ResolvedPolicy:
    """Effective permission set for a user on a resource.

    All table names are stored lower-cased for case-insensitive matching with
    the sqlglot validator and schema introspection (which both normalize).
    """

    # KBs the user cannot see at all (hidden from list_data_sources).
    blocked_kb_ids: set[str] = field(default_factory=set)
    # Table-level denies: {(kb_id, table_lower): True}
    denied_tables: dict[tuple[str, str], str] = field(default_factory=dict)
    # Whitelist-mode KBs (KB-level deny + explicit table-level allow):
    # {kb_id: {table_lower, ...}}
    allowlisted_kbs: dict[str, set[str]] = field(default_factory=dict)
    # Column allowlists (mode='allow_columns'): {(kb_id, table_lower): [cols]}
    column_allowlists: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    # Row filters: {(kb_id, table_lower): {...}}
    row_filters: dict[tuple[str, str], dict] = field(default_factory=dict)
    # True when at least one policy row existed (vs pure default-allow).
    has_policies: bool = False

    # -- KB-level ---------------------------------------------------------

    def is_kb_fully_denied(self, kb_id: str) -> bool:
        return kb_id in self.blocked_kb_ids

    def is_kb_restricted(self, kb_id: str) -> bool:
        """True if this KB has any explicit restriction (deny/allowlist/columns)."""
        if kb_id in self.allowlisted_kbs:
            return True
        if any(k == kb_id for k, _ in self.denied_tables):
            return True
        if any(k == kb_id for k, _ in self.column_allowlists):
            return True
        if any(k == kb_id for k, _ in self.row_filters):
            return True
        return False

    # -- table-level ------------------------------------------------------

    def blocked_tables_for_kb(self, kb_id: str) -> list[str]:
        """Table names explicitly denied within *kb_id* (lower-cased)."""
        return sorted(t for (k, t) in self.denied_tables if k == kb_id)

    def allowed_tables_for_kb(self, kb_id: str) -> list[str] | None:
        """Allowed table names for *kb_id*.

        Returns ``None`` when the KB has no table allowlist (i.e. all tables
        are allowed except the blocked ones), or a list of table names when the
        KB is in whitelist mode (KB-level deny + explicit table-level allows).
        """
        if kb_id in self.allowlisted_kbs:
            return sorted(self.allowlisted_kbs[kb_id])
        return None

    def allowed_columns_for(self, kb_id: str, table: str) -> list[str] | None:
        """Column allowlist for a table, or ``None`` when all columns allowed."""
        return self.column_allowlists.get((kb_id, table.lower()))

    def row_filter_for(self, kb_id: str, table: str) -> dict | None:
        return self.row_filters.get((kb_id, table.lower()))


def resolve_user_policies(
    db: Session,
    *,
    user_id: str | None,
    resource_type: str | None,
    resource_id: str | None,
    bound_kb_ids: Iterable[str] | None = None,
    owner_id: str | None = None,
    is_admin: bool = False,
) -> ResolvedPolicy:
    """Resolve the effective data-access policy for a user on a resource.

    Args:
        db: DB session.
        user_id: The user being checked.
        resource_type: 'project' | 'agent'.
        resource_id: UUID of the shared resource.
        bound_kb_ids: The KBs the calling agent is bound to.  Used to precompute
            per-KB decisions.  May be ``None``/empty (then nothing is blocked).
        owner_id: The resource owner's id.  When equal to *user_id* the policy
            is full-access (owners are never constrained).
        is_admin: Admins always get full-access.

    Returns:
        A ``ResolvedPolicy``.  When no policies exist (or the user is owner/
        admin), returns a full-access policy (``blocked_kb_ids`` empty,
        ``has_policies`` False).
    """
    kb_ids = list(bound_kb_ids or [])

    # Owner / admin bypass — full access.
    if is_admin or (owner_id is not None and user_id is not None and owner_id == user_id):
        return ResolvedPolicy(has_policies=False)

    if not user_id or not resource_type or not resource_id:
        return ResolvedPolicy(has_policies=False)

    policies = (
        db.query(ResourceAccessPolicy)
        .filter(
            ResourceAccessPolicy.resource_type == resource_type,
            ResourceAccessPolicy.resource_id == resource_id,
            ResourceAccessPolicy.user_id == user_id,
            ResourceAccessPolicy.is_deleted == False,  # noqa: E712
        )
        .all()
    )

    if not policies:
        return ResolvedPolicy(has_policies=False)

    try:
        resolved = _build_resolved(policies, kb_ids)
    except Exception as e:
        # Resolution must never crash the tool call — fail open to full access.
        logger.warning("access_policy_service: resolution failed, failing open: %s", e)
        return ResolvedPolicy(has_policies=False)

    logger.info(
        "access_policy_service: resolved policies user=%s resource=%s/%s "
        "blocked_kbs=%d denied_tables=%d allowlisted_kbs=%d",
        user_id, resource_type, resource_id,
        len(resolved.blocked_kb_ids), len(resolved.denied_tables),
        len(resolved.allowlisted_kbs),
    )
    return resolved


def validate_sql_against_policy(sql: str, policy: ResolvedPolicy, kb_id: str):
    """Validate *sql* against a resolved policy for *kb_id*.

    Wraps the existing ``nl2sql.validator.validate`` with the table
    allow/block lists derived from the policy.  Callers must still check
    ``policy.is_kb_fully_denied(kb_id)`` themselves — a fully-denied KB has no
    table-level rules, so validation alone would pass.
    """
    from app.services.nl2sql.validator import validate

    blocked = policy.blocked_tables_for_kb(kb_id)
    allowed = policy.allowed_tables_for_kb(kb_id)
    return validate(sql, allowed_tables=allowed, block_tables=blocked)


def _build_resolved(
    policies: list[ResourceAccessPolicy],
    bound_kb_ids: list[str],
) -> ResolvedPolicy:
    """Build a ResolvedPolicy from raw policy rows + the bound KB set."""
    global_mode: str | None = None
    global_table_policies: dict[str, ResourceAccessPolicy] = {}
    kb_policies: dict[str, ResourceAccessPolicy] = {}
    table_policies: dict[tuple[str, str], ResourceAccessPolicy] = {}

    for p in policies:
        if p.kb_id is None and not p.table_name:
            global_mode = p.mode
        elif p.kb_id is None:
            global_table_policies[p.table_name.lower()] = p
        elif not p.table_name:
            kb_policies[p.kb_id] = p
        else:
            table_policies[(p.kb_id, p.table_name.lower())] = p

    resolved = ResolvedPolicy(has_policies=True)

    for kb_id in bound_kb_ids:
        # Effective KB-level mode.
        kb_policy = kb_policies.get(kb_id)
        kb_mode = kb_policy.mode if kb_policy is not None else (global_mode or MODE_ALLOW)

        # Table-level policies that affect this KB: specific first, global fallback.
        kb_table_overrides: dict[str, ResourceAccessPolicy] = {
            t: p for (k, t), p in table_policies.items() if k == kb_id
        }
        for t, p in global_table_policies.items():
            kb_table_overrides.setdefault(t, p)

        # KB fully denied?
        if kb_mode == MODE_DENY:
            allowed_override = {
                t for t, p in kb_table_overrides.items()
                if p.mode in (MODE_ALLOW, MODE_ALLOW_COLUMNS)
            }
            if allowed_override:
                resolved.allowlisted_kbs[kb_id] = allowed_override
            else:
                resolved.blocked_kb_ids.add(kb_id)

        # Table-level rules.
        for t, p in kb_table_overrides.items():
            if p.mode == MODE_DENY:
                resolved.denied_tables[(kb_id, t)] = MODE_DENY
            elif p.mode == MODE_ALLOW_COLUMNS:
                resolved.column_allowlists[(kb_id, t)] = list(p.column_allowlist or [])
                if p.row_filter:
                    resolved.row_filters[(kb_id, t)] = dict(p.row_filter)

    return resolved
