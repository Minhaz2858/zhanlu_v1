"""openrouter_client tool — multi-model LLM router via OpenRouter.

Forwards a prompt to https://openrouter.ai/api/v1/chat/completions and
returns the response. Useful when the user wants a different model than
the default (DeepSeek) — e.g. a faster or larger model for a specific
task.

Env vars: OPENROUTER_API_KEY. Optional: OPENROUTER_MODEL (default
'anthropic/claude-3.5-sonnet'), OPENROUTER_HTTP_REFERER (for attribution).
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

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


async def _openrouter_client(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    missing = check_env_vars(["OPENROUTER_API_KEY"])
    if missing:
        return missing_config_response("openrouter", missing_env=missing)

    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return {"success": False, "error": "prompt is required"}
    model = (args.get("model") or os.environ.get("OPENROUTER_MODEL", "anthropic/claude-3.5-sonnet")).strip()
    system = (args.get("system") or "").strip()
    temperature = float(args.get("temperature", 0.7))
    max_tokens = int(args.get("max_tokens", 1024))

    headers = {
        "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
        "Content-Type": "application/json",
    }
    if "OPENROUTER_HTTP_REFERER" in os.environ:
        headers["HTTP-Referer"] = os.environ["OPENROUTER_HTTP_REFERER"]
    if "OPENROUTER_TITLE" in os.environ:
        headers["X-Title"] = os.environ["OPENROUTER_TITLE"]

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(OPENROUTER_URL, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        return {"success": False, "error": f"OpenRouter {exc.response.status_code}: {exc.response.text[:200]}"}
    except Exception as exc:
        return {"success": False, "error": f"OpenRouter call failed: {exc}"}

    choices = data.get("choices") or []
    if not choices:
        return {"success": False, "error": "OpenRouter returned no choices"}
    text = choices[0].get("message", {}).get("content", "")
    return {
        "success": True,
        "model": model,
        "text": text,
        "usage": data.get("usage", {}),
    }


OPENROUTER_CLIENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "openrouter",
        "description": (
            "Route a prompt through OpenRouter.ai to any supported LLM. "
            "Use to access models not configured as the default "
            "(Claude, GPT-4, Gemini, Llama, etc.). Requires "
            "OPENROUTER_API_KEY."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "The user prompt."},
                "model": {"type": "string", "description": "Model id (e.g. 'anthropic/claude-3.5-sonnet').", "default": "anthropic/claude-3.5-sonnet"},
                "system": {"type": "string", "description": "Optional system prompt."},
                "temperature": {"type": "number", "description": "Sampling temperature.", "default": 0.7},
                "max_tokens": {"type": "integer", "description": "Max output tokens.", "default": 1024},
            },
            "required": ["prompt"],
        },
    },
}

registry.register(
    name="openrouter",
    schema=OPENROUTER_CLIENT_SCHEMA,
    handler=_openrouter_client,
    category="llm",
    toolset="llm",
    description="Multi-model LLM router via OpenRouter.",
    emoji="🌐",
    requires_env=["OPENROUTER_API_KEY"],
    max_result_size_chars=20_000,
)
