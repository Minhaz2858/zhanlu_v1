"""Adapter: expose ``SynexiaFSM`` to the chat loop (``add_message`` /
``add_message_stream``) without rewriting the existing 2000-line
handlers.

Contract:

* ``run_via_fsm(conv, message, db, *, on_state_change)`` — runs the full
  FSM pipeline for one user message and returns the ``ExecutionResult``
  so the chat loop can serialize it for the frontend.
* Falls back to ``None`` when the FSM is disabled
  (``SYNEXIA_FSM_ENABLED`` env / setting is false) or when the FSM
  itself raises — the caller then runs its legacy ReAct loop as a
  rollback path.  This keeps the legacy loop reachable via
  ``LEGACY_REACT_LOOK=1`` for instant rollback.

The module is intentionally tiny.  Most of the work lives in
``synexia.fsm.SynexiaFSM`` already; we just construct the
``ExecutionRequest`` and translate errors.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


def is_fsm_enabled() -> bool:
    """Read the feature flag from env or settings; default True.

    The flag is *True by default* in this build because the existing
    FSM implementation is already mature; flipping it off is the
    rollback path.
    """
    env = os.environ.get("SYNEXIA_FSM_ENABLED")
    if env is not None:
        return env.lower() in ("1", "true", "yes", "on")
    try:
        from app.config import settings  # type: ignore

        val = getattr(settings, "SYNEXIA_FSM_ENABLED", None)
        if val is None:
            return True
        return bool(val)
    except Exception as exc:
        logger.debug("chat_loop_adapter: cannot read settings (%s); defaulting to enabled", exc)
        return True


def run_via_fsm(
    *,
    conversation: Any,
    user_message: str,
    db: Any,
    agent_name: str = "general_assistant",
    attachments: Optional[list[str]] = None,
    on_state_change: Optional[Callable[[str], None]] = None,
    endpoint: Optional[Any] = None,
) -> Optional[Any]:
    """Run the FSM for one user turn; return ExecutionResult or None.

    Returns ``None`` when:

    * the FSM feature flag is off (legacy loop should run instead), or
    * the FSM raises an exception (legacy loop should run as fallback).

    The caller is expected to log + render the assistant reply either
    way; this adapter never *replaces* the legacy path, it only
    *prefers* the FSM when enabled.

    ``endpoint`` (hierarchical LLMEndpoint) is forwarded to the
    ExecutionRequest so FSM internals use the pinned model.
    """
    if not is_fsm_enabled():
        return None
    try:
        # Lazy imports so the legacy loop doesn't pay the import cost.
        from app.services.synexia.fsm import ExecutionRequest, SynexiaFSM
    except Exception as exc:
        logger.warning("chat_loop_adapter: cannot import SynexiaFSM (%s)", exc)
        return None
    try:
        request = ExecutionRequest(
            conversation_id=getattr(conversation, "id", None),
            agent_name=agent_name,
            user_message=user_message or "",
            attachments=list(attachments or []),
            mode="dynamic",
            org_id=getattr(conversation, "org_id", "default-org") or "default-org",
            app_id=getattr(conversation, "app_id", "default-app") or "default-app",
            endpoint=endpoint,
        )
        fsm = SynexiaFSM(db=db)
        return fsm.run(request=request, on_state_change=on_state_change)
    except Exception as exc:
        logger.warning(
            "chat_loop_adapter: FSM raised (%s); falling back to legacy loop",
            exc,
            exc_info=True,
        )
        return None


__all__ = ["is_fsm_enabled", "run_via_fsm"]
