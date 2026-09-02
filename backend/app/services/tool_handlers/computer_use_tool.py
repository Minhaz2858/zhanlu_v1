"""computer_use tool — managed-browser primitive facade.

Turns the legacy stub into a Claude-style computer-use surface driven by
the Synexia FSM agent loop, one primitive per call, over the Rust
``agent-browser`` CLI (Chrome/Chromium via CDP):

  - open       → navigate (SSRF guard + COMPUTER_USE_ALLOWED_URLS)
  - screenshot → PNG file path
  - get_state  → accessibility-tree JSON with compact @eN element refs
  - click/type/scroll → interact via @eN refs or CSS selectors
  - close      → free the shared per-conversation Chromium session

No pixel coordinates and no ``eval`` — the model sees the a11y tree, not
the host shell. When ``COMPUTER_USE_ENABLED=false`` (the default) the
handler returns the original "not implemented" stub so nothing regresses.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.services.computer_use import dispatch, is_available
from app.services.tool_handlers._missing_config import (
    check_binaries,
    missing_config_response,
)
from app.services.tool_registry import registry

logger = logging.getLogger(__name__)

_LEGACY_STUB_SCREENSHOT = (
    "computer_use is not yet implemented for this platform. Use the 'browser' "
    "tool for headless web pages, or implement a platform-specific "
    "screencapture helper and register it."
)
_LEGACY_STUB_OTHER = (
    "computer_use.{action} is not yet implemented. Use 'browser' for web tasks."
)


async def _computer_use(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    action = (args.get("action") or "screenshot").lower()

    # Disabled flag ⇒ legacy stub response (behavior parity).
    if not settings.COMPUTER_USE_ENABLED:
        if action == "screenshot":
            return {"success": False, "error": _LEGACY_STUB_SCREENSHOT}
        return {"success": False, "error": _LEGACY_STUB_OTHER.format(action=action)}

    conversation_id = (
        (context or {}).get("conversation_id", "default") if context else "default"
    )

    # Missing-binary check — same contract as every other CLI-backed tool.
    missing = check_binaries(["agent-browser"])
    if missing:
        return missing_config_response(
            "computer_use",
            missing_binaries=["agent-browser"],
            missing_infra=[
                "agent-browser CLI not installed in the backend container. "
                "Rebuild the backend image (docker compose build backend) — "
                "the Dockerfile installs it via `npm i -g agent-browser`."
            ],
        )

    return dispatch(action, args, conversation_id)


COMPUTER_USE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "computer_use",
        "description": (
            "Managed browser automation (computer-use). Drive a real "
            "Chromium session one primitive per call: `open` a URL, "
            "`screenshot` the viewport, `get_state` for the accessibility "
            "tree (compact @eN element refs), then `click`/`type`/`scroll` "
            "using @eN refs (or CSS selectors), and `close` to free the "
            "session. Prefer this over `agent_browser` when the task needs "
            "screenshot + element-ref interaction, like Claude computer "
            "use. SSRF guard + COMPUTER_USE_ALLOWED_URLS apply to `open`. "
            "Sessions are per-conversation with a step budget; call `close` "
            "to reset it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "open", "screenshot", "get_state",
                        "click", "type", "scroll", "close",
                    ],
                },
                "url": {
                    "type": "string",
                    "description": "URL to open (for `open`).",
                },
                "element": {
                    "type": "string",
                    "description": (
                        "Element target for click/type: an @eN ref from "
                        "get_state, or a CSS selector."
                    ),
                },
                "text": {
                    "type": "string",
                    "description": "Text to type (for `type`).",
                },
                "direction": {
                    "type": "string",
                    "enum": ["up", "down"],
                    "description": "Scroll direction (for `scroll`).",
                },
                "full_page": {
                    "type": "boolean",
                    "description": "Capture the full page height (for `screenshot`).",
                    "default": False,
                },
            },
            "required": ["action"],
        },
    },
}

registry.register(
    name="computer_use",
    schema=COMPUTER_USE_SCHEMA,
    handler=_computer_use,
    category="browser",
    toolset="browser",
    enabled_by_default=settings.COMPUTER_USE_ENABLED,
    check_fn=is_available,
    description="Managed browser automation via the agent-browser CLI (computer-use).",
    emoji="🖥️",
    max_result_size_chars=30_000,
)
