"""computer_use action dispatcher — primitive facade over ``agent-browser``.

The Synexia FSM agent loop drives one primitive per call (like Claude's
computer-use tool / modern browser MCP tools): ``open``, ``screenshot``,
``get_state`` (accessibility tree with compact ``@eN`` refs), ``click``,
``type``, ``scroll``, ``close``. No pixel coordinates, no ``eval``, no
host shell. A Kimi-style vision ``run`` loop is a later flag-gated follow-up.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from typing import Any, Dict, List

from app.config import settings
from app.services.computer_use.session import (
    BROWSER_DIR,
    cleanup_session,
    get_session,
    is_exhausted,
    register_action,
)
from app.services.tool_handlers._missing_config import missing_config_response
from app.services.tool_security import is_safe_url, is_url_in_allowlist

ALLOWED_ACTIONS = frozenset(
    {"open", "screenshot", "get_state", "click", "type", "scroll", "close"}
)


def is_available() -> bool:
    """True when the agent-browser binary is on PATH."""
    return shutil.which("agent-browser") is not None


def run_cli(args: List[str], timeout: int = 60) -> Dict[str, Any]:
    """Run the agent-browser CLI and return a normalized result dict.

    agent-browser writes its primary result to stdout. Non-zero exit means
    an error; stderr usually explains why. A missing binary returns the
    standard ``missing_config_response`` shape.
    """
    try:
        proc = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"agent-browser timed out after {timeout}s"}
    except FileNotFoundError:
        return missing_config_response(
            "agent_browser",
            missing_binaries=["agent-browser"],
        )
    except Exception as exc:  # pragma: no cover
        return {"success": False, "error": f"agent-browser invocation failed: {exc}"}

    if proc.returncode != 0:
        return {
            "success": False,
            "error": (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}",
        }
    return {"success": True, "stdout": proc.stdout, "stderr": proc.stderr}


def maybe_json(text: str) -> Any:
    """Parse JSON when the CLI emitted JSON; else return the raw string."""
    if not text:
        return ""
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return text
    return text


def _is_http_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def dispatch(action: str, args: dict, conversation_id: str) -> dict:
    """Dispatch a single computer-use action for a conversation."""
    action = (action or "").strip().lower()
    if action not in ALLOWED_ACTIONS:
        return {"success": False, "error": f"Unknown computer_use action: {action!r}"}

    # close always works — it resets the action budget.
    if action == "close":
        cleanup_session(conversation_id)
        return {"success": True, "message": "Browser session closed"}

    max_steps = int(settings.COMPUTER_USE_MAX_STEPS or 0)
    if is_exhausted(conversation_id, max_steps):
        return {
            "success": False,
            "error": (
                f"computer_use step budget exhausted ({max_steps} steps). "
                "Call `close` to reset the session budget, or raise "
                "COMPUTER_USE_MAX_STEPS."
            ),
        }

    session = get_session(conversation_id, ttl_seconds=settings.COMPUTER_USE_SESSION_TTL_S)
    session_arg = ["--session", session["session_id"]]

    if action == "open":
        url = (args.get("url") or "").strip()
        if not url:
            return {"success": False, "error": "url is required for open"}
        if not _is_http_url(url) and not url.startswith("about:"):
            return {"success": False, "error": f"url must be http(s); got: {url!r}"}
        if _is_http_url(url):
            if not is_safe_url(url):
                return {"success": False, "error": f"URL blocked by SSRF guard: {url}"}
            if not is_url_in_allowlist(url, settings.COMPUTER_USE_ALLOWED_URLS):
                return {
                    "success": False,
                    "error": (
                        f"URL blocked by allowlist (not in "
                        f"COMPUTER_USE_ALLOWED_URLS): {url}"
                    ),
                }
        result = run_cli(["agent-browser", "open", url, *session_arg], timeout=60)
        if not result.get("success"):
            return result
        session["url"] = url
        register_action(conversation_id)
        return {
            "success": True,
            "url": url,
            "session_id": session["session_id"],
            "title": session.get("title"),
        }

    if action == "screenshot":
        full_page = bool(args.get("full_page", False))
        out_path = BROWSER_DIR / f"computer_use_{conversation_id}_{uuid.uuid4().hex[:8]}.png"
        cli = ["agent-browser", "screenshot", *session_arg, "--out", str(out_path)]
        if full_page:
            cli.append("--full-page")
        result = run_cli(cli, timeout=60)
        if not result.get("success"):
            return result
        try:
            size = out_path.stat().st_size
        except OSError:
            size = None
        register_action(conversation_id)
        return {
            "success": True,
            "file_path": str(out_path),
            "file_size": size,
            "full_page": full_page,
            "url": session.get("url"),
        }

    if action == "get_state":
        result = run_cli(["agent-browser", "snapshot", *session_arg], timeout=30)
        if not result.get("success"):
            return result
        register_action(conversation_id)
        return {
            "success": True,
            "state": maybe_json(result.get("stdout", "")),
            "url": session.get("url"),
        }

    if action == "click":
        element = (args.get("element") or args.get("ref") or "").strip()
        if not element:
            return {
                "success": False,
                "error": "element is required for click (e.g. @e12 or a CSS selector)",
            }
        result = run_cli(["agent-browser", "click", *session_arg, element], timeout=30)
        if not result.get("success"):
            return result
        register_action(conversation_id)
        return {"success": True, "clicked": element, "url": session.get("url")}

    if action == "type":
        element = (args.get("element") or args.get("ref") or "").strip()
        text = args.get("text", "")
        if not element:
            return {"success": False, "error": "element is required for type (e.g. @e12)"}
        if not text:
            return {"success": False, "error": "text is required for type"}
        result = run_cli(["agent-browser", "type", *session_arg, element, text], timeout=30)
        if not result.get("success"):
            return result
        register_action(conversation_id)
        return {
            "success": True,
            "typed": text,
            "element": element,
            "url": session.get("url"),
        }

    if action == "scroll":
        cli = ["agent-browser", "scroll", *session_arg]
        direction = (args.get("direction") or "").strip().lower()
        if direction in {"up", "down"}:
            cli.append(direction)
        result = run_cli(cli, timeout=30)
        if not result.get("success"):
            return result
        register_action(conversation_id)
        return {
            "success": True,
            "direction": direction or None,
            "url": session.get("url"),
        }

    return {"success": False, "error": f"Unknown computer_use action: {action!r}"}
