"""agent_browser tool — thin wrapper around the ``agent-browser`` CLI.

The ``agent-browser`` CLI is a fast Rust-based browser automation tool
(Chrome/Chromium via CDP) with accessibility-tree snapshots and compact
``@eN`` element references. It is shipped as an npm package and is
installed at the backend image build time (see
``backend/docker/backend.Dockerfile``).

This module replaces the previous Playwright-based ``browser`` tool. The
new tool surface mirrors the most common CLI verbs:

  - navigate     → ``agent-browser open <url>``
  - snapshot     → ``agent-browser snapshot``        (accessibility tree)
  - act          → ``agent-browser click|type|...``  (interact)
  - screenshot   → ``agent-browser screenshot``
  - extract      → ``agent-browser read``          (agent-readable page text)
  - eval         → ``agent-browser eval <expression>``
  - close        → ``agent-browser close``

Heavy dep: ``agent-browser`` (npm package) + a working Chromium. The CLI
is invoked via the shared ``app.services.computer_use`` manager
(``subprocess.run`` per call); per-conversation sessions are keyed on
``context["conversation_id"]`` and shared with the ``computer_use`` tool,
so the same CLI session (one Chromium per chat) is reused across calls in
the same conversation.

If the CLI binary is not on ``$PATH`` the handler returns the standard
``missing_config_response`` shape (same contract as every other tool in
``tool_handlers/``) so the LLM can guide the user through the
install/restart flow.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.services.computer_use import manager as _cu_manager
from app.services.computer_use.session import (
    BROWSER_DIR as _BROWSER_DIR,
    cleanup_session as _cleanup_session_impl,
    get_session as _get_session_impl,
)
from app.services.tool_handlers._missing_config import (
    missing_config_response,
    check_binaries,
)
from app.services.tool_registry import registry
from app.services.tool_security import is_safe_url

logger = logging.getLogger(__name__)


# ── Shared session store ─────────────────────────────────────────────────
# The session store lives in ``app.services.computer_use.session`` so
# ``agent_browser`` and ``computer_use`` share ONE Chromium session per
# conversation (with TTL + per-conversation action budget). The CLI
# manages its own Chromium process; we just remember the ``--session
# <id>`` argument passed on ``navigate`` so subsequent calls in the same
# chat can reuse it. ``_get_session`` / ``_cleanup_session`` / ``_run_cli``
# are thin wrappers kept for call-site and test compatibility.

def _get_session(conversation_id: str) -> Dict[str, Any]:
    """Return the shared per-conversation session record (TTL-managed)."""
    return _get_session_impl(
        conversation_id, ttl_seconds=settings.COMPUTER_USE_SESSION_TTL_S
    )


def _cleanup_session(conversation_id: str) -> None:
    """Forget the session and best-effort close the underlying CLI session."""
    _cleanup_session_impl(conversation_id)


def _run_cli(args: List[str], timeout: int = 60) -> Dict[str, Any]:
    """Run the agent-browser CLI via the shared computer_use manager."""
    return _cu_manager.run_cli(args, timeout=timeout)


def _maybe_json(text: str) -> Any:
    """If ``text`` is JSON, parse it; else return the raw string.

    The CLI emits JSON for ``snapshot`` / ``extract`` / ``eval`` and plain
    text for everything else. We try JSON first and fall back gracefully.
    """
    if not text:
        return ""
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return text
    return text


# SSRF guard only applies to URLs the LLM passes in. Element refs (@e12)
# and JS expressions (eval) skip this check.

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def _looks_like_url(value: str) -> bool:
    return bool(value and _URL_RE.match(value.strip()))


# ── Extract post-processing helpers (adapted from Hermes) ────────────────

# Inline base64 image payloads are token bombs (a single PNG can be tens of
# thousands of characters). Strip them from extracted markdown before the
# text ever reaches the model, replacing with a compact placeholder.
_B64_IMG_RE = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\(\s*data:image/[^;]+;base64,[A-Za-z0-9+/=\s]+\)"
)


def _strip_base64_images(text: str) -> str:
    """Replace inline base64 image blobs with a compact placeholder."""
    def _repl(m: "re.Match") -> str:
        alt = m.group("alt")
        return f"![{alt}](<base64 image omitted>)"
    return _B64_IMG_RE.sub(_repl, text)


def _truncate_with_footer(
    content: str,
    url: str,
    char_limit: int,
    conversation_id: str,
) -> tuple[str, bool, Optional[str]]:
    """Return ``(model_text, was_truncated, stored_path)`` for extracted text.

    Pages at or under ``char_limit`` are returned whole. Larger pages get a
    head+tail window (~75% head / ~25% tail) cut on a markdown line boundary,
    plus an explicit footer telling the model exactly how much it is seeing,
    where the full text is stored, and which ``read_file`` call pages in the
    omitted middle. Deterministic — no model involvement.
    """
    if len(content) <= char_limit:
        return content, False, None

    stored_path = _BROWSER_DIR / f"extract_{conversation_id}_{uuid.uuid4().hex[:8]}.md"
    try:
        stored_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        logger.warning("Failed to store full extract text: %s", exc)
        # Fall back to head-only truncation so we still return something.
        return content[:char_limit], True, None

    head_size = int(char_limit * 0.75)
    tail_size = char_limit - head_size

    # Cut the head on a markdown line boundary (don't split mid-line).
    head = content[:head_size]
    nl = head.rfind("\n")
    if nl > head_size // 2:
        head = head[:nl]
    # Cut the tail on a line boundary too.
    tail = content[-tail_size:]
    nl = tail.find("\n")
    if nl != -1 and nl < tail_size // 2:
        tail = tail[nl + 1:]

    # Line number where the omitted middle starts, so the footer can tell
    # the model the exact read_file offset to page through the full text.
    head_line_count = head.count("\n") + 1
    total_lines = content.count("\n") + 1

    footer = (
        f"\n\n---\n[EXTRACT TRUNCATED] Showing {len(head) + len(tail):,} of "
        f"{len(content):,} chars (head + tail window). "
        f"Full text ({total_lines} lines) saved to: {stored_path}\n"
        f'To read the omitted middle: read_file path="{stored_path}" '
        f"offset={head_line_count + 1} limit=200  "
        f"(the file is the complete page; raise/lower offset to page through it)."
    )
    return (
        head + "\n\n[... omitted middle ...]\n\n" + tail + footer,
        True,
        str(stored_path),
    )


def _detect_degraded_extract(text: str) -> tuple[bool, Optional[str]]:
    """Flag a 'successful but thin' extraction so the agent can escalate.

    Returns ``(degraded, reason)``. A page that navigated and returned 200 but
    yielded almost no visible text is usually JS-rendered, paywalled, or
    blocking bots — the agent should know to try ``snapshot`` or a different
    URL rather than treating the empty text as "the answer is: nothing here".
    """
    stripped = text.strip()
    if len(stripped) < 200:
        reason = (
            "extracted text is very short despite successful navigation — "
            "page may be JS-rendered, paywalled, or blocking bots. "
            "Consider `snapshot` to see the accessibility tree, or try a "
            "different URL."
        )
        return True, reason
    return False, None


async def _agent_browser(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    action = (args.get("action") or "navigate").lower()
    conversation_id = (
        (context or {}).get("conversation_id", "default") if context else "default"
    )

    # ── Missing-binary check ────────────────────────────────────────────
    missing = check_binaries(["agent-browser"])
    if missing:
        return missing_config_response(
            "agent_browser",
            missing_binaries=["agent-browser"],
            missing_infra=[
                "agent-browser CLI not installed in the backend container. "
                "Rebuild the backend image (docker compose build backend) — "
                "the Dockerfile installs it via `npm i -g agent-browser`."
            ],
        )

    session = _get_session(conversation_id)
    session_arg = ["--session", session["session_id"]]

    # ── Action dispatch ─────────────────────────────────────────────────
    if action == "navigate":
        url = (args.get("url") or "").strip()
        if not url:
            return {"success": False, "error": "url is required for navigate"}
        if not _looks_like_url(url) and not url.startswith("about:"):
            return {"success": False, "error": f"url must be http(s); got: {url!r}"}
        if _looks_like_url(url) and not is_safe_url(url):
            return {"success": False, "error": f"URL blocked by SSRF guard: {url}"}
        cli = ["agent-browser", "open", url, *session_arg]
        result = _run_cli(cli, timeout=60)
        if not result.get("success"):
            return result
        session["url"] = url
        return {
            "success": True,
            "url": url,
            "session_id": session["session_id"],
            "title": session.get("title"),
        }

    if action == "snapshot":
        cli = ["agent-browser", "snapshot", *session_arg]
        result = _run_cli(cli, timeout=30)
        if not result.get("success"):
            return result
        return {
            "success": True,
            "snapshot": _maybe_json(result.get("stdout", "")),
            "url": session.get("url"),
        }

    if action == "act":
        verb = (args.get("verb") or "").lower().strip()
        if not verb:
            return {
                "success": False,
                "error": (
                    "act requires a `verb` (e.g. click, type, scroll, press, "
                    "hover, focus, select, check) and an `element` ref like "
                    "`@e12` or a CSS selector"
                ),
            }
        element = (args.get("element") or "").strip()
        text = args.get("text", "")
        cli = ["agent-browser", verb, *session_arg]
        if element:
            cli.append(element)
        if text:
            cli.append(text)
        result = _run_cli(cli, timeout=30)
        if not result.get("success"):
            return result
        return {
            "success": True,
            "verb": verb,
            "element": element,
            "typed": text or None,
            "url": session.get("url"),
        }

    if action == "screenshot":
        full_page = bool(args.get("full_page", False))
        out_path = _BROWSER_DIR / f"{conversation_id}_{uuid.uuid4().hex[:8]}.png"
        cli = [
            "agent-browser", "screenshot",
            *session_arg,
            "--out", str(out_path),
        ]
        if full_page:
            cli.append("--full-page")
        result = _run_cli(cli, timeout=60)
        if not result.get("success"):
            return result
        try:
            size = out_path.stat().st_size
        except OSError:
            size = None
        return {
            "success": True,
            "file_path": str(out_path),
            "file_size": size,
            "full_page": full_page,
            "url": session.get("url"),
        }

    if action == "extract":
        # NOTE: the CLI verb is `read` (fetch agent-readable text). An
        # earlier version of this handler called `agent-browser extract`,
        # which does NOT exist in agent-browser >= 0.31 and returned
        # "Unknown command: extract". `read [url]` returns the rendered
        # page text as markdown — the core real-time-data primitive.
        url = (args.get("url") or "").strip()
        if url:
            # One-step: open + read in a single CLI call.
            if not _looks_like_url(url):
                return {"success": False, "error": f"url must be http(s); got: {url!r}"}
            if not is_safe_url(url):
                return {"success": False, "error": f"URL blocked by SSRF guard: {url}"}
            cli = ["agent-browser", "read", url, *session_arg]
            session["url"] = url
        else:
            # Read the page already loaded in this session (after navigate).
            cli = ["agent-browser", "read", *session_arg]
        result = _run_cli(cli, timeout=45)
        if not result.get("success"):
            return result
        raw = result.get("stdout", "")
        # Strip inline base64 image blobs (token bombs) before anything else.
        raw = _strip_base64_images(raw)
        # Head+tail window with stored full text + read_file footer, so the
        # agent can page through a long article without re-navigating.
        char_limit = settings.TOOL_MAX_OUTPUT_CHARS
        text, was_truncated, stored_path = _truncate_with_footer(
            raw, url or session.get("url") or "", char_limit, conversation_id,
        )
        # Detect "successful but thin" extractions so the agent can escalate.
        degraded, degraded_reason = _detect_degraded_extract(text)
        resp: Dict[str, Any] = {
            "success": True,
            "url": url or session.get("url"),
            "text": text,
            "truncated": was_truncated,
        }
        if stored_path:
            resp["full_text_path"] = stored_path
        if degraded:
            resp["degraded"] = True
            resp["degraded_reason"] = degraded_reason
        return resp

    if action == "eval":
        expression = (args.get("expression") or "").strip()
        if not expression:
            return {"success": False, "error": "expression is required for eval"}
        cli = ["agent-browser", "eval", *session_arg, expression]
        result = _run_cli(cli, timeout=30)
        if not result.get("success"):
            return result
        return {
            "success": True,
            "result": _maybe_json(result.get("stdout", "")),
        }

    if action == "close":
        _cleanup_session(conversation_id)
        return {"success": True, "message": "Browser session closed"}

    return {"success": False, "error": f"Unknown action: {action!r}"}


# ── Schema + registration ───────────────────────────────────────────────

AGENT_BROWSER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "agent_browser",
        "description": (
            "Browser automation via the agent-browser CLI (Chrome/Chromium "
            "via CDP, accessibility-tree snapshots, compact @eN element "
            "refs). Prefer this over the older `browser` tool — it is "
            "faster, supports a richer action surface, and does not "
            "require Playwright. Actions: navigate, snapshot, act, "
            "screenshot, extract, eval, close. SSRF guard applies to "
            "`navigate` URLs. Sessions are per-conversation; call `close` "
            "explicitly to free the Chromium process. REAL-TIME DATA: this "
            "is a first-class data-collection path when web_search is "
            "unavailable, thin, or geo-blocked — `navigate` to a URL (or a "
            "search-engine results page) then `extract` returns the "
            "rendered visible page text; no API key required."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "navigate", "snapshot", "act",
                        "screenshot", "extract", "eval", "close",
                    ],
                },
                "url": {
                    "type": "string",
                    "description": (
                        "URL to navigate to (for `navigate`), or to open-and-"
                        "read in one step (for `extract`)."
                    ),
                },
                "verb": {
                    "type": "string",
                    "description": (
                        "Interaction verb for `act`: click, type, scroll, "
                        "press, hover, focus, select, check, etc."
                    ),
                },
                "element": {
                    "type": "string",
                    "description": (
                        "Element target for `act`: an @eN ref from a "
                        "snapshot, or a CSS selector."
                    ),
                },
                "text": {
                    "type": "string",
                    "description": "Text to type (for `act` with verb=type).",
                },
                "expression": {
                    "type": "string",
                    "description": "JS expression to evaluate in page (for eval).",
                },
                "full_page": {
                    "type": "boolean",
                    "description": "Capture full page (for screenshot).",
                    "default": False,
                },
            },
            "required": ["action"],
        },
    },
}


def _is_available() -> bool:
    """Check the agent-browser binary is on PATH (TTL-cached by registry)."""
    return _cu_manager.is_available()


registry.register(
    name="agent_browser",
    schema=AGENT_BROWSER_SCHEMA,
    handler=_agent_browser,
    category="browser",
    toolset="agent-browser",
    check_fn=_is_available,
    description="Browser automation via the agent-browser CLI.",
    emoji="🧭",
    max_result_size_chars=30_000,
)
