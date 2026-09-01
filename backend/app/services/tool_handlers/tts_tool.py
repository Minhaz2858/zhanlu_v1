"""tts tool — multi-provider text-to-speech.

Supports three providers (auto-selected by env):
  - openai  (OPENAI_API_KEY) — tts-1 model, 6 voices, mp3
  - elevenlabs (ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID) — high quality
  - mistral  (MISTRAL_API_KEY) — mistral TTS (lazy-deps installed)

Returns the audio file path on disk under
``/root/zhanlu/backend/tool_artifacts/tts/<timestamp>.mp3``.

Use the missing-config flow if no provider is configured — the agent
will ask the user for the key and run update_env_config.
"""

from __future__ import annotations

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

_TTS_DIR = Path(
    os.environ.get("ZHANLU_TTS_DIR", "/tmp/zhanlu_tts")
)


def _detect_provider() -> Optional[str]:
    """Return the first configured provider, or None if none are set.

    Skips the "openai" provider when the configured OPENAI_BASE_URL is
    not OpenAI's own API (e.g. DeepSeek), since DeepSeek / other
    OpenAI-compatible providers don't have an /audio/speech endpoint.
    """
    for provider, envs in {
        "elevenlabs": ["ELEVENLABS_API_KEY"],
        "openai": ["OPENAI_API_KEY"],
        "mistral": ["MISTRAL_API_KEY"],
    }.items():
        if not all(os.environ.get(e) for e in envs):
            continue
        if provider == "openai":
            base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
            if not base.startswith("https://api.openai.com"):
                # OPENAI_API_KEY is set but the base URL isn't OpenAI's;
                # the openai TTS endpoint won't exist there.
                continue
        return provider
    return None


async def _tts_openai(text: str, voice: str, out_path: Path) -> dict:
    api_key = os.environ["OPENAI_API_KEY"]
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    url = f"{base}/audio/speech"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": "tts-1", "input": text, "voice": voice, "response_format": "mp3"}
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        out_path.write_bytes(resp.content)
    return {"provider": "openai", "voice": voice, "bytes": out_path.stat().st_size}


async def _tts_elevenlabs(text: str, voice_id: str, out_path: Path) -> dict:
    api_key = os.environ["ELEVENLABS_API_KEY"]
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    payload = {"text": text, "model_id": "eleven_monolingual_v1", "voice_settings": {"stability": 0.5, "similarity_boost": 0.5}}
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        out_path.write_bytes(resp.content)
    return {"provider": "elevenlabs", "voice_id": voice_id, "bytes": out_path.stat().st_size}


async def _tts(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    text = (args.get("text") or "").strip()
    if not text:
        return {"success": False, "error": "text is required"}
    voice = (args.get("voice") or "alloy").strip()
    provider = (args.get("provider") or _detect_provider() or "").lower()

    if not provider:
        return missing_config_response(
            "tts",
            missing_env=[
                name for name in ["ELEVENLABS_API_KEY", "OPENAI_API_KEY", "MISTRAL_API_KEY"]
                if not os.environ.get(name)
            ][:1] or ["ELEVENLABS_API_KEY"],  # suggest one as primary
        )

    _TTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _TTS_DIR / f"tts_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}.mp3"

    try:
        if provider == "openai":
            info = await _tts_openai(text, voice, out_path)
        elif provider == "elevenlabs":
            voice_id = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")  # Rachel default
            info = await _tts_elevenlabs(text, voice_id, out_path)
        elif provider == "mistral":
            return {"success": False, "error": "Mistral TTS support requires the 'mistral' extra — install with: pip install mistral"}
        else:
            return {"success": False, "error": f"Unknown provider: {provider}"}
    except Exception as exc:
        return {"success": False, "error": f"TTS call failed: {exc}"}

    return {
        "success": True,
        "text": text,
        "file_path": str(out_path),
        "file_size": out_path.stat().st_size,
        **info,
    }


TTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "tts",
        "description": (
            "Convert text to speech using one of three providers "
            "(elevenlabs, openai, mistral). Provider is auto-selected "
            "from configured env vars. Returns the audio file path."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The text to synthesize."},
                "voice": {"type": "string", "description": "Voice name (openai: 'alloy'|'echo'|'fable'|'onyx'|'nova'|'shimmer')."},
                "provider": {"type": "string", "enum": ["openai", "elevenlabs", "mistral"], "description": "Force a specific provider (default: auto-detect from env)."},
            },
            "required": ["text"],
        },
    },
}

registry.register(
    name="tts",
    schema=TTS_SCHEMA,
    handler=_tts,
    category="media",
    toolset="media",
    description="Multi-provider text-to-speech.",
    emoji="🗣️",
    max_result_size_chars=5_000,
)
