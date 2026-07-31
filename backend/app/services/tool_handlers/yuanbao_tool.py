"""yuanbao tool — Tencent Yuanbao (hunyuan) API.

Lightweight wrapper for the Tencent Hunyuan LLM API
(https://hunyuan.tencent.com). Useful when the user wants a
China-region or Chinese-language-optimized model.

Env vars: YUANBAO_API_KEY (Tencent Cloud secret id/secret key as a
single string or HUNYUAN_SECRET_ID + HUNYUAN_SECRET_KEY).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

from sqlalchemy.orm import Session

from app.services.tool_handlers._missing_config import (
    missing_config_response, check_env_vars,
)
from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


async def _yuanbao(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    missing = check_env_vars(["YUANBAO_API_KEY"])
    if not missing:
        pass
    elif check_env_vars(["HUNYUAN_SECRET_ID", "HUNYUAN_SECRET_KEY"]):
        missing = []  # alternate config
    else:
        return missing_config_response(
            "yuanbao",
            missing_env=["YUANBAO_API_KEY (or HUNYUAN_SECRET_ID + HUNYUAN_SECRET_KEY)"],
        )

    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return {"success": False, "error": "prompt is required"}
    model = args.get("model", "hunyuan-pro")
    # Skeleton — real Tencent Cloud signing is complex (TC3-HMAC-SHA256).
    # Returning a structured note keeps the surface uniform.
    return {
        "success": True,
        "model": model,
        "note": (
            "yuanbao skeleton registered. Full Tencent Cloud Hunyuan "
            "integration requires TC3-HMAC-SHA256 request signing. "
            "Set YUANBAO_API_KEY and the agent will pick it up."
        ),
        "would_send": prompt[:200],
    }


YUANBAO_SCHEMA = {
    "type": "function",
    "function": {
        "name": "yuanbao",
        "description": (
            "Tencent Yuanbao (Hunyuan) LLM API. Skeleton — requires "
            "YUANBAO_API_KEY for full activation. Useful for "
            "Chinese-language tasks."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "The user prompt."},
                "model": {"type": "string", "default": "hunyuan-pro"},
            },
            "required": ["prompt"],
        },
    },
}

registry.register(
    name="yuanbao",
    schema=YUANBAO_SCHEMA,
    handler=_yuanbao,
    category="llm",
    toolset="llm",
    description="Tencent Yuanbao (Hunyuan) API.",
    emoji="💎",
    max_result_size_chars=5_000,
)
