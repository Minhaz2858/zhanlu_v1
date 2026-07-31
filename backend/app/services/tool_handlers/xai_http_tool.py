"""xai_http tool — thin HTTP client for the xAI (Grok) API.

Mirrors openrouter but targets https://api.x.ai/v1/chat/completions.
Grok is a useful alternative when the user wants a different perspective
or needs a model with different safety tuning.

Env vars: XAI_API_KEY.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

from sqlalchemy.orm import Session

from app.services.tool_handlers._missing_config import missing_config_response, check_env_vars
from app.services.tool_registry import registry

logger = logging.getLogger(__name__)

XAI_URL = "https://api.x.ai/v1/chat/completions"


async def _xai_http(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    missing = check_env_vars(["XAI_API_KEY"])
    if missing:
        return missing_config_response("xai_http", missing_env=missing)

    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return {"success": False, "error": "prompt is required"}
    model = (args.get("model") or os.environ.get("XAI_MODEL", "grok-2-latest")).strip()
    system = (args.get("system") or "").strip()
    temperature = float(args.get("temperature", 0.7))

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    headers = {
        "Authorization": f"Bearer {os.environ['XAI_API_KEY']}",
        "Content-Type": "application/json",
    }
    payload = {"model": model, "messages": messages, "temperature": temperature}
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(XAI_URL, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        return {"success": False, "error": f"xAI call failed: {exc}"}
    choices = data.get("choices") or []
    if not choices:
        return {"success": False, "error": "xAI returned no choices"}
    return {
        "success": True,
        "model": model,
        "text": choices[0].get("message", {}).get("content", ""),
        "usage": data.get("usage", {}),
    }


XAI_HTTP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "xai_http",
        "description": (
            "Call the xAI (Grok) API. Requires XAI_API_KEY. Use when you "
            "want a different model perspective than the default."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "The user prompt."},
                "model": {"type": "string", "description": "Model id (default 'grok-2-latest').", "default": "grok-2-latest"},
                "system": {"type": "string", "description": "Optional system prompt."},
                "temperature": {"type": "number", "description": "Sampling temperature.", "default": 0.7},
            },
            "required": ["prompt"],
        },
    },
}

registry.register(
    name="xai_http",
    schema=XAI_HTTP_SCHEMA,
    handler=_xai_http,
    category="llm",
    toolset="llm",
    description="xAI (Grok) API client.",
    emoji="𝕏",
    requires_env=["XAI_API_KEY"],
    max_result_size_chars=20_000,
)
