"""Fusion 360 tool — execute Python inside Autodesk Fusion 360.

Talks to the local FusionMCP add-in over TCP (127.0.0.1:9876, newline-delimited
JSON). The add-in runs the submitted code on Fusion's MAIN thread (the adsk API
is not thread-safe) and returns stdout. This is the same bridge the Hermes CLI
uses — it bypasses the platform MCP proxy (which is still a stub).

Pre-bound names inside Fusion: `app`, `ui`, `product`, `design`, `root`
(rootComponent), `adsk`, `core` (adsk.core), `fusion` (adsk.fusion).

Units: the Fusion API is in CENTIMETRES. Use mm(v) = ValueInput.createByReal(v/10.0).
"""

from __future__ import annotations

import json
import logging
import os
import socket

from app.services.tool_registry import registry
from app.services.tool_handlers.fusion360_validation import (
    enhance_fusion360_error,
    validate_fusion360_code,
)

logger = logging.getLogger(__name__)

# The backend runs inside Docker; the FusionMCP add-in binds on the HOST.
# `host.docker.internal` reaches the host loopback from inside the container.
# Override with FUSION360_HOST=127.0.0.1 when running the backend natively.
FUSION_HOST = os.environ.get("FUSION360_HOST", "host.docker.internal")
FUSION_PORT = 9876
CONNECT_TIMEOUT = 10
CALL_TIMEOUT = 180

HELP = (
    "Fusion 360 bridge add-in is not reachable. Open Autodesk Fusion 360, then "
    "Tools > Add-Ins > Scripts and Add-Ins > Add-Ins tab > select 'FusionMCP' > "
    "Run. The add-in must stay running while you work."
)


def _resolve_endpoint(db=None) -> tuple[str, int]:
    """Resolve (host, port) for the Fusion bridge, per-agent aware.

    Priority: the calling agent's ``tool_config.fusion_endpoint`` ("host" or
    "host:port") -> ``FUSION360_HOST`` env -> ``host.docker.internal:9876``.
    The agent is identified via ``TOOL_CONTEXT_VAR`` (concurrency-safe, set by
    ``execute_tool``), so a per-agent endpoint never leaks across users.
    """
    host = os.environ.get("FUSION360_HOST", "host.docker.internal")
    port = FUSION_PORT
    ep = None
    try:
        from app.services.agent_tools import TOOL_CONTEXT_VAR
        app_id = (TOOL_CONTEXT_VAR.get() or {}).get("agent_app_id")
        if app_id and db is not None:
            from app.models.agent_app import AgentApp
            app = db.get(AgentApp, app_id)
            if app is not None and isinstance(getattr(app, "tool_config", None), dict):
                ep = app.tool_config.get("fusion_endpoint")
    except Exception:  # noqa: BLE001
        ep = None
    if ep:
        ep = str(ep).strip() or None
    if ep:
        if ":" in ep:
            h, _, p = ep.rpartition(":")
            host = h.strip() or host
            if p.strip().isdigit():
                port = int(p.strip())
        else:
            host = ep
    return host, port


def _call(code: str, db=None) -> dict:
    """Send code to the Fusion add-in over TCP and return the parsed response."""
    host, port = _resolve_endpoint(db)
    try:
        s = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
    except OSError:
        return {"error": HELP}
    try:
        s.settimeout(CALL_TIMEOUT)
        s.sendall((json.dumps({"id": "zhanlu", "code": code}) + "\n").encode("utf-8"))
        data = b""
        while b"\n" not in data:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        if not data:
            return {"error": "Fusion add-in closed the connection without a response."}
        return json.loads(data.decode("utf-8"))
    except socket.timeout:
        return {"error": "Timed out waiting for Fusion to execute the code."}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    finally:
        try:
            s.close()
        except OSError:
            pass


def _fusion360_execute_python(args: dict, db, user_id: str | None) -> dict:
    """Run Python inside Fusion 360 with full access to the adsk API."""
    code = (args.get("code") or "").strip()
    if not code:
        return {"success": False, "error": "code is required"}

    # Pre-flight: reject hallucinated adsk.* names BEFORE they run, so a typo
    # like adsk.core.Cylinder3D can't leave broken geometry in the timeline.
    ok, verr = validate_fusion360_code(code)
    if not ok:
        return {"success": False, "error": verr, "retryable": True}

    r = _call(code, db)
    if r.get("error"):
        # A connection failure (HELP) is not retryable; a code traceback is.
        retryable = r["error"] != HELP
        return {"success": False, "error": enhance_fusion360_error(r["error"]), "retryable": retryable}

    return {"success": True, "stdout": r.get("result", "")}


def _fusion360_ping(args: dict, db, user_id: str | None) -> dict:
    """Check whether the Fusion 360 bridge add-in is reachable."""
    r = _call("None", db)
    if r.get("error"):
        return {"success": False, "error": r["error"], "retryable": False}
    return {"success": True, "stdout": "pong — Fusion 360 bridge connected"}


_MANIFEST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fusion360_api_manifest.json")


def _load_manifest() -> dict | None:
    try:
        with open(_MANIFEST_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def _fusion360_lookup_api(args: dict, db, user_id: str | None) -> dict:
    """Query the introspected adsk API manifest (existence + members + suggestions)."""
    import difflib

    m = _load_manifest()
    if m is None:
        return {"success": False, "error": "API manifest not available (fusion360_api_manifest.json missing).", "retryable": False}

    cls = (args.get("class_name") or "").strip()
    if not cls:
        return {"success": False, "error": "class_name is required", "retryable": False}
    member = (args.get("member_name") or "").strip()

    # accept a namespace prefix ("adsk.fusion.ExtrudeFeatures" -> "ExtrudeFeatures")
    for prefix in ("adsk.core.", "adsk.fusion.", "core.", "fusion."):
        if cls.startswith(prefix):
            cls = cls[len(prefix):]
            break

    core_attrs: dict = m.get("core_attrs", {}) or {}
    fusion_attrs: dict = m.get("fusion_attrs", {}) or {}
    core_names: list = list(m.get("core_names", []) or [])
    fusion_names: list = list(m.get("fusion_names", []) or [])

    found: str | None = None
    members: list | None = None
    if cls in fusion_attrs:
        found, members = f"adsk.fusion.{cls}", fusion_attrs[cls]
    elif cls in core_attrs:
        found, members = f"adsk.core.{cls}", core_attrs[cls]

    if found is None or members is None:
        sug = difflib.get_close_matches(cls, fusion_names + core_names, n=5)
        return {"success": False, "error": f"'{cls}' not found in adsk.core or adsk.fusion. Did you mean: {', '.join(sug) or 'none'}", "retryable": True}

    if member:
        if member in members:
            return {"success": True, "stdout": f"FOUND {found}.{member}"}
        sug = difflib.get_close_matches(member, members, n=5)
        return {"success": False, "error": f"{found} has no member '{member}'. Did you mean: {', '.join(sug) or 'none'}", "retryable": True}

    shown = members[:60]
    suffix = f" (+{len(members) - 60} more)" if len(members) > 60 else ""
    return {"success": True, "stdout": f"{found} members: {', '.join(shown)}{suffix}"}


# ---------------------------------------------------------------------------
# Schemas & Registration
# ---------------------------------------------------------------------------

FUSION360_EXECUTE_PYTHON_SCHEMA = {
    "type": "function",
    "function": {
        "name": "fusion360_execute_python",
        "description": (
            "Run a Python script inside Autodesk Fusion 360 with full access to the "
            "adsk API (create sketches, extrude, revolve, fillet, holes, etc.). "
            "Pre-bound names: app, ui, product, design, root (rootComponent), adsk, "
            "core (adsk.core), fusion (adsk.fusion). The API uses CENTIMETRES — use "
            "mm(v) = adsk.core.ValueInput.createByReal(v / 10.0). Anything printed to "
            "stdout is returned (with any traceback on error). Use print() to report "
            "results (e.g. print('BODIES:', root.bRepBodies.count))."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python code to run inside Fusion 360. Use print() for output.",
                },
            },
            "required": ["code"],
        },
    },
}

FUSION360_PING_SCHEMA = {
    "type": "function",
    "function": {
        "name": "fusion360_ping",
        "description": (
            "Check whether the Fusion 360 bridge add-in is reachable (Fusion must be "
            "open with the FusionMCP add-in running)."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

registry.register(
    name="fusion360_execute_python",
    schema=FUSION360_EXECUTE_PYTHON_SCHEMA,
    handler=_fusion360_execute_python,
    category="cad",
    toolset="cad",
    enabled_by_default=True,
    description="Run Python inside Autodesk Fusion 360 to build CAD models.",
    emoji="🧊",
)

registry.register(
    name="fusion360_ping",
    schema=FUSION360_PING_SCHEMA,
    handler=_fusion360_ping,
    category="cad",
    toolset="cad",
    enabled_by_default=True,
    description="Check the Fusion 360 bridge connection.",
    emoji="🧊",
)

registry.register(
    name="fusion360_lookup_api",
    schema={
        "type": "function",
        "function": {
            "name": "fusion360_lookup_api",
            "description": (
                "Look up the REAL Fusion 360 (adsk) API before writing raw Python: does a class "
                "exist, does a member exist on it, and what are its members? Use this to avoid "
                "hallucinating a name like adsk.core.Cylinder3D. class_name e.g. 'ExtrudeFeatures' "
                "or 'adsk.fusion.ExtrudeFeatures'; optional member_name e.g. 'setDistanceExtent'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "class_name": {"type": "string", "description": "Class to look up (with or without adsk.core./adsk.fusion. prefix)."},
                    "member_name": {"type": "string", "description": "Optional member to verify on the class."},
                },
                "required": ["class_name"],
            },
        },
    },
    handler=_fusion360_lookup_api,
    category="cad",
    toolset="cad",
    enabled_by_default=True,
    description="Look up a real adsk API class/member (anti-hallucination).",
    emoji="📖",
)
