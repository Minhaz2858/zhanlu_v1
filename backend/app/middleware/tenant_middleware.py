"""Tenant middleware — sets the DB tenant context BEFORE any session opens.

Ordering matters: FastAPI dependencies (get_db) create the request's DB
session, whose FIRST transaction begins during the auth user lookup. A
context set inside the auth dependency would be too late for that
transaction. This PURE-ASGI middleware (BaseHTTPMiddleware would run the
endpoint in a separate task and DROP the contextvar) decodes the Bearer
JWT — no DB — and pins the tenant from the token's ``org_id`` claim: the
verified identity, never client-supplied fields.

Tokens without an org claim (legacy) leave the context unset → the RLS
policies' NULL-bypass applies until re-login.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class TenantContextMiddleware:
    """Pure ASGI middleware — context set here propagates downstream."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            org = self._org_from_headers(scope.get("headers") or [])
            if org:
                from app.services.tenant_context import set_tenant_org

                try:
                    set_tenant_org(org)
                except Exception:  # noqa: BLE001 — malformed id: stay unset
                    logger.debug("tenant middleware: invalid org claim ignored")
        await self.app(scope, receive, send)

    @staticmethod
    def _org_from_headers(headers) -> str | None:
        auth = None
        for name, value in headers:
            if name == b"authorization":
                auth = value.decode("latin-1")
                break
        if not auth or not auth.startswith("Bearer "):
            return None
        try:
            import jwt as _jwt

            from app.config import settings as _settings

            payload = _jwt.decode(
                auth[7:].strip(),
                _settings.JWT_SECRET,
                algorithms=[_settings.JWT_ALGORITHM],
                options={"verify_exp": True},
            )
            org = payload.get("org_id")
            return str(org) if org else None
        except Exception:  # noqa: BLE001 — invalid/expired tokens are the
            # auth dependency's problem; here we simply stay unset.
            return None
