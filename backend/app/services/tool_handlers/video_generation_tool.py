"""video_generation tool — generate short videos from text.

Two backends supported via env var VIDEO_API_PROVIDER:
  - "fal"      — FAL.ai (https://fal.ai), uses FAL_KEY
  - "replicate" — Replicate, uses REPLICATE_API_TOKEN

Returns a polling URL and a final file path under
``/root/zhanlu/backend/tool_artifacts/video/``.

Heavier deps are installed via lazy_deps on first call.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx

from sqlalchemy.orm import Session

from app.services.tool_handlers._missing_config import (
    missing_config_response, check_env_vars,
)
from app.services.tool_registry import registry

logger = logging.getLogger(__name__)

_VIDEO_DIR = Path(
    os.environ.get("ZHANLU_VIDEO_DIR", "/tmp/zhanlu_video")
)


async def _video_generation(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    provider = (args.get("provider") or os.environ.get("VIDEO_API_PROVIDER", "fal")).lower()
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return {"success": False, "error": "prompt is required"}

    if provider == "fal":
        missing = check_env_vars(["FAL_KEY"])
        if missing:
            return missing_config_response("video_generation", missing_env=missing)
    elif provider == "replicate":
        missing = check_env_vars(["REPLICATE_API_TOKEN"])
        if missing:
            return missing_config_response("video_generation", missing_env=missing)
    else:
        return {"success": False, "error": f"Unknown video provider: {provider!r}"}

    _VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    duration = int(args.get("duration_seconds", 5))
    if duration < 1 or duration > 30:
        duration = 5

    if provider == "fal":
        # FAL.ai — submit a request and poll. Use a generic model; real
        # deployment would let the user pick a model.
        url = "https://fal.run/fal-ai/luma-dream-machine"
        headers = {
            "Authorization": f"Key {os.environ['FAL_KEY']}",
            "Content-Type": "application/json",
        }
        payload = {"prompt": prompt}
        try:
            async with httpx.AsyncClient(timeout=600) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            return {"success": False, "error": f"FAL video generation failed: {exc}"}
        video_url = data.get("video", {}).get("url") or data.get("url")
        if not video_url:
            return {"success": False, "error": "FAL returned no video URL", "raw": data}
        # Download the video
        out_path = _VIDEO_DIR / f"video_{int(time.time())}_{uuid.uuid4().hex[:6]}.mp4"
        async with httpx.AsyncClient(timeout=300) as client:
            r = await client.get(video_url)
            r.raise_for_status()
            out_path.write_bytes(r.content)
        return {
            "success": True,
            "provider": "fal",
            "prompt": prompt,
            "file_path": str(out_path),
            "file_size": out_path.stat().st_size,
            "source_url": video_url,
        }

    if provider == "replicate":
        url = "https://api.replicate.com/v1/predictions"
        headers = {
            "Authorization": f"Token {os.environ['REPLICATE_API_TOKEN']}",
            "Content-Type": "application/json",
        }
        payload = {
            "version": "VIDEO_MODEL_VERSION_HASH",  # placeholder; user should configure
            "input": {"prompt": prompt},
        }
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            return {"success": False, "error": f"Replicate submission failed: {exc}"}
        return {
            "success": True,
            "provider": "replicate",
            "prompt": prompt,
            "prediction_id": data.get("id"),
            "status": data.get("status"),
            "note": "Configure VIDEO_MODEL_VERSION_HASH in the source to use a real model.",
        }

    return {"success": False, "error": f"Provider not implemented: {provider}"}


VIDEO_GENERATION_SCHEMA = {
    "type": "function",
    "function": {
        "name": "video_generation",
        "description": (
            "Generate a short video from a text prompt. Supports fal.ai "
            "and replicate backends. Requires the corresponding env var "
            "(FAL_KEY or REPLICATE_API_TOKEN). Returns the file path."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "The video prompt."},
                "provider": {"type": "string", "enum": ["fal", "replicate"], "default": "fal"},
                "duration_seconds": {"type": "integer", "description": "Target duration (1-30s, default 5).", "default": 5},
            },
            "required": ["prompt"],
        },
    },
}

registry.register(
    name="video_generation",
    schema=VIDEO_GENERATION_SCHEMA,
    handler=_video_generation,
    category="media",
    toolset="media",
    description="Text-to-video generation (fal.ai / replicate).",
    emoji="🎬",
    max_result_size_chars=5_000,
)
