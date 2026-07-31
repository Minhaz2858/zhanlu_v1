"""Contract tests — lock in the routes/endpoints the smoke_e2e.sh script relies on.

These tests verify that the API routes referenced by scripts/smoke_e2e.sh
actually exist in the FastAPI app. If a route is renamed or removed, these
tests catch it before the smoke script fails.

Note: routes are prefixed with /api/apps/{app_id}/ at runtime, so we check
the underlying router paths (which appear in app.routes without the prefix).
"""
import importlib


def _get_routes():
    from main import app
    return {r.path for r in app.routes if hasattr(r, "path")}


def _paths_contain(routes, *needles):
    """Check that all needles appear in any route path."""
    return all(any(n in r for r in routes) for n in needles)


def test_healthz_endpoint_exists():
    """/healthz must be a routable endpoint."""
    routes = _get_routes()
    assert "/healthz" in routes, f"/healthz not in {sorted(routes)[:5]}..."


def test_auth_login_endpoint_exists():
    """The auth router must expose a login endpoint."""
    routes = _get_routes()
    assert _paths_contain(routes, "auth", "login"), "auth login route not found"


def test_conversations_endpoint_exists():
    """The agents router must expose conversations endpoints."""
    routes = _get_routes()
    assert _paths_contain(routes, "conversations"), "conversations route not found"


def test_v2_messages_endpoint_exists():
    """The agents router must expose a v2 messages endpoint (for v2 message post)."""
    routes = _get_routes()
    # v2 is the synchronous message post path: /agents/conversations/v2/{id}/messages
    assert _paths_contain(routes, "v2", "messages"), "v2 messages route not found"


def test_sandbox_jobs_endpoint_exists():
    """The sandbox router must expose jobs endpoints."""
    routes = _get_routes()
    job_routes = [r for r in routes if "sandbox" in r and "job" in r]
    assert len(job_routes) > 0, f"No sandbox job routes found"


def test_artifacts_endpoint_exists():
    """The artifacts router must exist."""
    routes = _get_routes()
    art_routes = [r for r in routes if "artifact" in r]
    assert len(art_routes) > 0, f"No artifact routes found"
