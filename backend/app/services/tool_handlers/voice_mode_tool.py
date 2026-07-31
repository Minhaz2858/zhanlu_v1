"""voice_mode tool — orchestrate a live voice conversation.

Combines tts (speech) + transcription (listen) into a single "voice
mode" entrypoint. Useful when the chat UI exposes a mic button — the
agent can enter voice mode, stream transcripts to/from the user, and
reply with synthesized speech.

This is a thin orchestrator: it returns a structured instruction for
the chat UI to drive the audio capture/playback, and the actual audio
I/O happens client-side.

For now this returns a no-op acknowledgement so the agent can opt into
voice mode. Full bidirectional audio requires sounddevice / portaudio
on the backend, which is environment-specific.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


async def _voice_mode(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    action = (args.get("action") or "enter").lower()
    if action == "enter":
        return {
            "success": True,
            "mode": "voice",
            "instruction": (
                "Voice mode entered. Speak to the user; the chat UI will "
                "transcribe your speech to text (use the transcription "
                "tool) and read your replies aloud (use the tts tool). "
                "Maintain the same conversational contract as text mode."
            ),
        }
    if action == "exit":
        return {
            "success": True,
            "mode": "text",
            "instruction": "Voice mode exited. Continue in text mode.",
        }
    return {"success": False, "error": f"Unknown action: {action!r}"}


VOICE_MODE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "voice_mode",
        "description": (
            "Enter or exit voice mode (live audio conversation). The "
            "agent uses tts for output and transcription for input. The "
            "actual audio capture/playback is client-side."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["enter", "exit"]},
            },
            "required": ["action"],
        },
    },
}

registry.register(
    name="voice_mode",
    schema=VOICE_MODE_SCHEMA,
    handler=_voice_mode,
    category="media",
    toolset="media",
    description="Enter/exit voice mode (live audio conversation).",
    emoji="🎙️",
)
