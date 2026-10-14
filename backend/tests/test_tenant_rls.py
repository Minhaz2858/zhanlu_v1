"""Tenant context + RLS plumbing tests (tenancy D3, 2026-10-13).

The Postgres policies themselves are verified live (scripts/apply_rls.sql
+ psql checks) — SQLite can't run RLS. Here we test the plumbing:
org-id validation, the SET LOCAL statement builder, the pure-ASGI
middleware's claim extraction, and that the session listener is a no-op
on non-Postgres dialects.
"""
import asyncio


def test_tenant_context_validation():
    from app.services.tenant_context import set_tenant_org, get_tenant_org

    set_tenant_org("default-org")
    assert get_tenant_org() == "default-org"
    set_tenant_org(None)
    assert get_tenant_org() is None


def test_tenant_context_rejects_malformed_id():
    from app.services.tenant_context import set_tenant_org, get_tenant_org

    try:
        set_tenant_org("bad'; DROP TABLE users; --")
        raised = False
    except ValueError:
        raised = True
    assert raised  # injection attempt refused at the boundary
    assert get_tenant_org() is None


def test_tenant_set_sql_builds_statement():
    from app.services.tenant_context import set_tenant_org, tenant_set_sql

    set_tenant_org("org-a")
    assert tenant_set_sql() == "SET LOCAL app.current_org = 'org-a'"
    set_tenant_org(None)
    assert tenant_set_sql() is None


def test_middleware_extracts_org_from_bearer():
    import jwt as _jwt
    from datetime import datetime, timedelta, timezone

    from app.config import settings as s
    from app.middleware.tenant_middleware import TenantContextMiddleware

    token = _jwt.encode(
        {"sub": "u1", "org_id": "org-x",
         "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        s.JWT_SECRET, algorithm=s.JWT_ALGORITHM,
    )
    headers = [(b"authorization", f"Bearer {token}".encode())]
    assert TenantContextMiddleware._org_from_headers(headers) == "org-x"
    assert TenantContextMiddleware._org_from_headers([]) is None
    assert TenantContextMiddleware._org_from_headers(
        [(b"authorization", b"Bearer garbage")]) is None


def test_middleware_propagates_context_to_downstream():
    """Pure-ASGI: the context set in the middleware is visible downstream
    (the reason BaseHTTPMiddleware was NOT used)."""
    from app.config import settings as s
    from app.middleware.tenant_middleware import TenantContextMiddleware
    from app.services.tenant_context import get_tenant_org

    import jwt as _jwt
    from datetime import datetime, timedelta, timezone

    token = _jwt.encode(
        {"sub": "u1", "org_id": "org-x",
         "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        s.JWT_SECRET, algorithm=s.JWT_ALGORITHM,
    )

    async def app(scope, receive, send):
        seen["org"] = get_tenant_org()

    seen = {}
    mw = TenantContextMiddleware(app)
    scope = {
        "type": "http",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    }
    asyncio.run(mw(scope, None, None))
    assert seen["org"] == "org-x"


def test_session_listener_noop_on_sqlite(tmp_path):
    """Non-Postgres sessions skip the tenant pin entirely (tests run on
    SQLite); queries work normally."""
    from app.database import SessionLocal
    from app.services.tenant_context import set_tenant_org

    set_tenant_org("org-a")
    db = SessionLocal()
    try:
        from sqlalchemy import text

        db.execute(text("SELECT 1"))  # must not raise
        ok = True
    finally:
        db.close()
    assert ok


def test_rls_policies_treat_empty_setting_as_unpinned_source():
    """REGRESSION (2026-10-14, RLS batch 5): Postgres custom-GUC quirk —
    after `SET LOCAL app.current_org` on a pooled connection, later
    transactions see EMPTY STRING (not NULL) from current_setting, so the
    original `... IS NULL` bypass silently failed on unpinned queries
    (intermittent login 401s). Every tenant_isolation policy MUST use the
    coalesce('' = '') bypass. SQLite can't run RLS — guard the SCRIPTS."""
    import pathlib
    import re

    scripts = pathlib.Path(__file__).resolve().parent.parent / "scripts"
    policy_re = re.compile(
        r"CREATE POLICY tenant_isolation.*?USING\s*\((.*?)\)\s*", re.DOTALL
    )
    for name in ("apply_rls.sql", "apply_rls_batch4.sql", "apply_rls_batch5.sql"):
        path = scripts / name
        if not path.exists():
            continue
        src = path.read_text()
        # The bare IS NULL bypass must never appear inside a tenant_isolation
        # policy definition again.
        for m in policy_re.finditer(src):
            body = m.group(1)
            assert "IS NULL" not in body or "coalesce" in body.lower(), (
                f"{name}: policy bypass must treat '' as unpinned "
                f"(SET LOCAL sticky-GUC quirk) — got: {body[:120]}"
            )
