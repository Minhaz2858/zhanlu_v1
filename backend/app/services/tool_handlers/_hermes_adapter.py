"""Hermes-style handler adapter.

Hermes handlers are written as ``def handler(args, **kwargs) -> str`` (or
async) and return JSON-encoded strings like ``{"error": "..."}`` or
``{"success": true, ...}``. Zhanlu handlers are ``async def handler(args, db,
user_id, context=None) -> dict`` and return plain dicts.

This module provides:

  - :func:`adapt_hermes_handler` — wraps a hermes-style sync or async
    function into a zhanlu-style handler. JSON strings are parsed back to
    dicts; hermes' ``{"error": ...}`` is converted to zhanlu's
    ``{"success": False, "error": ...}`` shape; hermes'
    ``{"success": true, ...}`` passes through unchanged.

  - :func:`hermes_tool_error` / :func:`hermes_tool_result` — convenience
    helpers for hermes-style handlers we port verbatim, so they keep their
    native JSON-string return convention internally and only the adapter
    converts at the boundary.

  - :class:`HermesAdapter` — a callable class for cases where the tool
    file's main handler is a module-level function and we need to
    pre-configure the env-var list / missing-config behavior.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
from typing import Any, Callable, Dict, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Return-shape translation
# ---------------------------------------------------------------------------

def _hermes_result_to_zhanlu(result: Any) -> Dict[str, Any]:
    """Convert a hermes-style return value into a zhanlu-style dict.

    - str (assumed JSON) → parsed; if parse fails, wrap in ``{"raw": ...}``
    - dict → normalize {"error": ...} → {"success": False, "error": ...}
    - already-zhanlu-shape (has "success" key) → passthrough
    - other (None, list, int) → wrap in ``{"result": ...}``
    """
    if result is None:
        return {"success": True}
    if isinstance(result, dict):
        if "success" in result:
            return dict(result)  # already zhanlu-shaped
        if "error" in result:
            return {"success": False, "error": str(result["error"]),
                    **{k: v for k, v in result.items() if k != "error"}}
        return {"success": True, **result}
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except (ValueError, TypeError):
            return {"success": True, "raw": result}
        if isinstance(parsed, dict):
            return _hermes_result_to_zhanlu(parsed)
        return {"success": True, "result": parsed}
    if isinstance(result, (list, tuple)):
        return {"success": True, "result": list(result)}
    return {"success": True, "result": result}


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

def adapt_hermes_handler(
    hermes_fn: Callable,
    *,
    requires_env: Optional[list[str]] = None,
    requires_binaries: Optional[list[str]] = None,
    requires_infra: Optional[list[str]] = None,
    tool_name: Optional[str] = None,
) -> Callable:
    """Wrap a hermes-style handler into a zhanlu-style async handler.

    Args:
        hermes_fn: The hermes-style function. Signature is
            ``(args, **kwargs) -> str`` (sync) or coroutine of same.
        requires_env: Env vars the tool needs; checked at handler invocation
            and a structured missing-config response is returned if missing.
        requires_binaries: Binaries the tool needs (e.g. "playwright").
        requires_infra: External infrastructure descriptions.
        tool_name: The registered name to put in the missing-config response.
            Defaults to ``hermes_fn.__name__``.

    Returns:
        An async function with signature
        ``async def(args, db: Session, user_id: str | None, context=None) -> dict``
    """
    # Local import to avoid a circular import at module load.
    from app.services.tool_handlers._missing_config import (
        missing_config_response,
        check_env_vars,
        check_binaries,
    )

    name = tool_name or getattr(hermes_fn, "__name__", "hermes_tool")
    is_async = asyncio.iscoroutinefunction(hermes_fn)

    @functools.wraps(hermes_fn)
    async def zhanlu_handler(
        args: dict,
        db: Session,
        user_id: str | None = None,
        context: dict | None = None,
    ) -> dict:
        # 1. Env / binary / infra check
        missing_env = check_env_vars(requires_env or []) if requires_env else []
        missing_bin = check_binaries(requires_binaries or []) if requires_binaries else []
        if missing_env or missing_bin or requires_infra:
            return missing_config_response(
                tool_name=name,
                missing_env=missing_env,
                missing_binaries=missing_bin,
                missing_infra=requires_infra or [],
            )

        # 2. Build hermes-style kwargs. Hermes handlers commonly read
        #    ``db``, ``user_id``, ``conversation_id``, ``agent_app_id``
        #    from kwargs.
        hermes_kwargs: Dict[str, Any] = {}
        if db is not None:
            hermes_kwargs["db"] = db
        if user_id is not None:
            hermes_kwargs["user_id"] = user_id
        if context:
            for key in ("conversation_id", "agent_app_id", "agent_name"):
                if key in context:
                    hermes_kwargs[key] = context[key]

        # 3. Invoke the underlying function (sync or async)
        try:
            if is_async:
                raw = await hermes_fn(args, **hermes_kwargs)
            else:
                raw = await asyncio.to_thread(hermes_fn, args, **hermes_kwargs)
        except Exception as exc:
            logger.warning("Hermes tool %s raised: %s", name, exc)
            return {"success": False, "error": f"{type(exc).__name__}: {exc}"}

        # 4. Convert the return shape
        return _hermes_result_to_zhanlu(raw)

    # Mark as a coroutine function so the dispatcher awaits it
    if not asyncio.iscoroutinefunction(zhanlu_handler):
        # functools.wraps preserves the underlying sync-ness. Decorate
        # explicitly: just return an awaitable wrapper.
        @functools.wraps(zhanlu_handler)
        async def _async_wrapper(*args, **kwargs):
            return await zhanlu_handler(*args, **kwargs)
        return _async_wrapper
    return zhanlu_handler


# ---------------------------------------------------------------------------
# Convenience helpers — for handlers that want to stay hermes-shaped internally
# ---------------------------------------------------------------------------

def hermes_tool_error(message: str, **extra: Any) -> str:
    """Return a JSON-encoded error string (hermes convention)."""
    payload: Dict[str, Any] = {"error": str(message)}
    if extra:
        payload.update(extra)
    return json.dumps(payload, ensure_ascii=False, default=str)


def hermes_tool_result(data: Any = None, **kwargs: Any) -> str:
    """Return a JSON-encoded result string (hermes convention).

    Accepts either a single positional dict-like or keyword arguments
    (mirrors tools.registry.tool_result in hermes).
    """
    if data is not None:
        if isinstance(data, dict):
            return json.dumps(data, ensure_ascii=False, default=str)
        return json.dumps({"result": data}, ensure_ascii=False, default=str)
    return json.dumps(kwargs, ensure_ascii=False, default=str)
