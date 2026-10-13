"""Tenant context — the org isolation variable for the DB layer (RLS D3).

Set from the VERIFIED identity in the auth dependency (never the client),
read by the session after_begin listener which executes
``SET LOCAL app.current_org = <org>`` at every transaction start on
Postgres. Postgres Row-Level-Security policies then confine every query
to the tenant — even if application code forgets a WHERE clause.

asyncio.to_thread propagates this ContextVar (verified in the cost
recorder work), so FSM thread-pool execution is covered too.

Background jobs and scripts that never set the variable run UNSET —
the RLS policies treat NULL as the internal/maintenance bypass. This is
the documented v1 escape hatch; per-path tightening comes later.
"""

from __future__ import annotations

import contextvars
import re

# org ids are platform-generated UUIDs / slugs — validated before they may
# reach a SQL SET (injection guard at the only string-interpolation point).
_ORG_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

current_tenant_org: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_tenant_org", default=None
)


def set_tenant_org(org_id: str | None) -> None:
    """Set the tenant for this request's DB transactions."""
    if org_id and not _ORG_ID_RE.match(org_id):
        # Refuse malformed ids rather than sanitizing silently.
        raise ValueError(f"invalid org_id for tenant context: {org_id[:20]!r}")
    current_tenant_org.set(org_id or None)


def get_tenant_org() -> str | None:
    return current_tenant_org.get()


def tenant_set_sql() -> str | None:
    """The SET LOCAL statement to run at transaction start, or None."""
    org = current_tenant_org.get()
    if not org:
        return None
    return f"SET LOCAL app.current_org = '{org}'"
