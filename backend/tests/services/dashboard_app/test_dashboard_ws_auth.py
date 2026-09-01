"""T9: WebSocket auth + reconnect for full-stack dashboards.

The generated template ``api.py`` rejects anonymous/invalid WebSocket
connections (close code 1008) and the template frontend appends the session
access token to the WS URL as a query param (browser WS cannot set headers).
"""
import importlib.util
import shutil
import sys
import types
from pathlib import Path

import pytest

from app.services.auth_service import auth_service
from app.services.dashboard_app.generator import DashboardAppGenerator, TEMPLATE_DIR

WS_SPEC = {
    "name": "WS Auth",
    "slug": "ws-auth",
    "description": None,
    "datasource_id": "kb-123",
    "design_system_ref": None,
    "refresh_interval_seconds": 15,
    "theme": "light",
    "metrics": [
        {
            "id": "kpi",
            "type": "kpi",
            "title": "Total",
            "sql": "SELECT sum(amount) AS v FROM erp_sales",
            "options": {"value_column": "v"},
        }
    ],
}


@pytest.fixture
def gen(tmp_path):
    apps_dir = tmp_path / "apps"
    g = DashboardAppGenerator(template_dir=TEMPLATE_DIR, apps_dir=apps_dir)
    yield g
    shutil.rmtree(apps_dir, ignore_errors=True)


def _load_generated_api(gen, spec=WS_SPEC):
    """Load the generated api.py as a real importable package module."""
    app_dir = gen.generate(spec)
    pkg_name = f"genapi_{abs(hash(str(app_dir)))}"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(app_dir)]  # makes it a package for `from .queries import …`
    sys.modules[pkg_name] = pkg
    spec_ = importlib.util.spec_from_file_location(f"{pkg_name}.api", app_dir / "api.py")
    mod = importlib.util.module_from_spec(spec_)
    mod.__package__ = pkg_name
    sys.modules[f"{pkg_name}.api"] = mod
    spec_.loader.exec_module(mod)
    return mod


# ── generated api.py gate ──


def test_generated_api_has_ws_auth_gate(gen):
    src = (gen.generate(WS_SPEC) / "api.py").read_text(encoding="utf-8")
    assert "_authorize_ws_token" in src
    assert "auth_service.verify_token(token)" in src
    assert "close(code=1008)" in src


def test_authorize_ws_token_rejects_anonymous_and_bad(gen, monkeypatch):
    mod = _load_generated_api(gen)
    monkeypatch.setattr(mod.auth_service, "verify_token", lambda token, db=None: None)
    assert mod._authorize_ws_token(None) is False
    assert mod._authorize_ws_token("") is False
    assert mod._authorize_ws_token("garbage-token") is False
    monkeypatch.setattr(
        mod.auth_service, "verify_token", lambda token, db=None: "user-1"
    )
    assert mod._authorize_ws_token("valid-token") is True


def test_auth_service_rejects_garbage_tokens():
    # The security primitive itself: signature/expiry must gate the handshake.
    assert auth_service.verify_token(None) is None
    assert auth_service.verify_token("") is None
    assert auth_service.verify_token("not-a-jwt") is None


# ── template frontend handshake ──


def test_frontend_stream_appends_token_to_ws_url(gen):
    # The generator only copies the BUILT dist/ into the app dir; the hook
    # source lives in the template and is compiled into the bundle.
    src = (TEMPLATE_DIR / "frontend" / "src" / "hooks" / "useDashboardStream.js").read_text(
        encoding="utf-8"
    )
    # Same-origin iframe ⇒ the app can read the main app's access token.
    assert "base44_access_token" in src
    assert "?token=" in src
    assert "encodeURIComponent(token)" in src


def test_frontend_stream_invokes_on_reconnect(gen):
    src = (TEMPLATE_DIR / "frontend" / "src" / "hooks" / "useDashboardStream.js").read_text(
        encoding="utf-8"
    )
    assert "rcRef.current?.()" in src
    assert "reconnected" in src
