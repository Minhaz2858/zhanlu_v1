"""clarify tool — surface a structured question to the user.

Hermes uses this for the cli/gateway UI to render a clickable multi-choice
question. In zhanlu, the chat UI already supports :::options blocks in
the agent's reply text, so this tool simply records the question and
choices as a structured event the UI can pick up.

The actual response is the user's next message — the agent uses the
question's intent to interpret the response.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import List, Optional

from sqlalchemy.orm import Session

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)

MAX_CHOICES = 4


async def _clarify(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    question = (args.get("question") or "").strip()
    choices = args.get("choices") or []
    if not question:
        return {"success": False, "error": "question is required"}
    if not isinstance(choices, list):
        return {"success": False, "error": "choices must be a list of strings"}
    choices = [str(c).strip() for c in choices if str(c).strip()]
    if len(choices) > MAX_CHOICES:
        choices = choices[:MAX_CHOICES]

    question_id = uuid.uuid4().hex[:8]
    payload = {
        "question_id": question_id,
        "question": question,
        "choices": choices,
        "open_ended": len(choices) == 0,
        "issued_at": time.time(),
    }
    # Push to a per-conversation event queue so the UI can render the question.
    conversation_id = (context or {}).get("conversation_id") if context else None
    if conversation_id:
        try:
            from app.services.tool_output import push_event
            push_event(conversation_id, {
                "type": "clarify_question",
                **payload,
            })
        except Exception as exc:
            logger.debug("Could not push clarify event: %s", exc)

    return {
        "success": True,
        "instruction": (
            "Surface this question to the user (it has been queued for the "
            "chat UI to render). Do NOT call this tool again — the user's "
            "next message is the answer. If no choices are listed, the "
            "question is open-ended."
        ),
        **payload,
    }


CLARIFY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "clarify",
        "description": (
            "Ask the user a structured clarifying question, optionally "
            "with up to 4 multiple-choice options. The question is queued "
            "for the chat UI to render; the user's next message is the "
            "answer. LAST-RESORT ONLY: do NOT call this for things you "
            "could reasonably infer or default (output format, tone, "
            "length, scope, tool choice, field names). Default to acting "
            "with sensible assumptions instead. Use this only when a "
            "missing input is required, cannot be inferred, and a wrong "
            "guess would cause irreversible harm. Never call this more "
            "than once per turn."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The question to ask."},
                "choices": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Up to 4 choices. Empty/open-ended if omitted.",
                },
            },
            "required": ["question"],
        },
    },
}

registry.register(
    name="clarify",
    schema=CLARIFY_SCHEMA,
    handler=_clarify,
    category="ux",
    toolset="ux",
    description="Ask the user a structured clarifying question.",
    emoji="❓",
)
