"""transcription tool — speech-to-text via OpenAI Whisper.

Accepts an audio file path (already on disk in the workspace) and
returns the transcript. Heavy dep `whisper` is lazy-installed on first
call.

Env vars: OPENAI_API_KEY (for OpenAI's hosted Whisper API) — works
without any local install.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import httpx

from sqlalchemy.orm import Session

from app.services.tool_handlers._missing_config import (
    missing_config_response, check_env_vars,
)
from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


async def _transcription(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    missing = check_env_vars(["OPENAI_API_KEY"])
    if missing:
        return missing_config_response("transcription", missing_env=missing)

    file_path = (args.get("file_path") or "").strip()
    if not file_path:
        return {"success": False, "error": "file_path is required"}
    p = Path(file_path)
    if not p.exists() or not p.is_file():
        return {"success": False, "error": f"Audio file not found: {file_path}"}
    if p.stat().st_size > 25 * 1024 * 1024:
        return {"success": False, "error": "File too large (>25MB); OpenAI Whisper API max is 25MB."}

    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    url = f"{base}/audio/transcriptions"
    headers = {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}
    try:
        with open(p, "rb") as f:
            files = {"file": (p.name, f)}
            data = {"model": args.get("model", "whisper-1"), "response_format": "verbose_json"}
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(url, headers=headers, files=files, data=data)
                resp.raise_for_status()
                result = resp.json()
    except Exception as exc:
        return {"success": False, "error": f"Whisper API call failed: {exc}"}
    return {
        "success": True,
        "text": result.get("text", ""),
        "language": result.get("language"),
        "duration": result.get("duration"),
        "segments": result.get("segments", []),
    }


TRANSCRIPTION_SCHEMA = {
    "type": "function",
    "function": {
        "name": "transcription",
        "description": (
            "Speech-to-text via OpenAI Whisper. Accepts an audio file "
            "path on disk and returns the transcript. Requires "
            "OPENAI_API_KEY."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to the audio file (mp3, mp4, m4a, ogg, wav, webm)."},
                "model": {"type": "string", "description": "Whisper model id (default 'whisper-1').", "default": "whisper-1"},
            },
            "required": ["file_path"],
        },
    },
}

registry.register(
    name="transcription",
    schema=TRANSCRIPTION_SCHEMA,
    handler=_transcription,
    category="media",
    toolset="media",
    description="Speech-to-text via OpenAI Whisper.",
    emoji="🎤",
    requires_env=["OPENAI_API_KEY"],
    max_result_size_chars=30_000,
)
