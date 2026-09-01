"""vision tool — image analysis.

Two modes:
  - "describe": use the LLM's vision capabilities to describe an image.
  - "ocr":      pull text out of an image (delegated to the same vision call).

Implementation: reads the image, base64-encodes it, and sends a vision
chat completion to the configured LLM (DeepSeek / OpenAI-compatible).
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Optional

import httpx

from sqlalchemy.orm import Session

from app.config import settings
from app.services.tool_handlers._missing_config import (
    missing_config_response, check_env_vars,
)
from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


async def _vision(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    missing = check_env_vars(["OPENAI_API_KEY"])
    if missing:
        return missing_config_response("vision", missing_env=missing)

    file_path = (args.get("file_path") or "").strip()
    if not file_path:
        return {"success": False, "error": "file_path is required"}
    p = Path(file_path)
    if not p.exists() or not p.is_file():
        return {"success": False, "error": f"Image not found: {file_path}"}
    mode = (args.get("mode") or "describe").lower()
    question = (args.get("question") or "").strip()
    if mode == "describe":
        prompt = question or "Describe this image in detail."
    elif mode == "ocr":
        prompt = question or "Extract all text visible in this image, preserving structure."
    else:
        return {"success": False, "error": f"Unknown mode: {mode!r}"}

    try:
        b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    except Exception as exc:
        return {"success": False, "error": f"Failed to read image: {exc}"}

    # Determine mime type from extension
    ext = p.suffix.lower()
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(ext, "image/jpeg")

    base = settings.OPENAI_BASE_URL.rstrip("/")
    url = f"{base}/chat/completions"
    headers = {"Authorization": f"Bearer {settings.OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": settings.LLM_MODEL,
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ]}
        ],
        "max_tokens": 1024,
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        return {"success": False, "error": f"Vision call failed: {exc}"}
    choices = data.get("choices") or []
    if not choices:
        return {"success": False, "error": "Vision model returned no choices"}
    return {
        "success": True,
        "mode": mode,
        "description": choices[0].get("message", {}).get("content", ""),
        "model": settings.LLM_MODEL,
    }


VISION_SCHEMA = {
    "type": "function",
    "function": {
        "name": "vision",
        "description": (
            "Analyze an image. Modes: 'describe' (default) for general "
            "description, 'ocr' for text extraction. Uses the configured "
            "vision-capable LLM (requires OPENAI_API_KEY or equivalent)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to the image file (png, jpg, gif, webp)."},
                "mode": {"type": "string", "enum": ["describe", "ocr"], "default": "describe"},
                "question": {"type": "string", "description": "Custom question about the image (optional)."},
            },
            "required": ["file_path"],
        },
    },
}

registry.register(
    name="vision",
    schema=VISION_SCHEMA,
    handler=_vision,
    category="media",
    toolset="media",
    description="Image analysis (describe / OCR).",
    emoji="👁️",
    requires_env=["OPENAI_API_KEY"],
    max_result_size_chars=20_000,
)
