"""send_message tool — generic messaging dispatcher.

Routes a message to a channel configured via env. The actual delivery
backend is selected by SEND_MESSAGE_BACKEND (default 'log').

Backends:
  - log:      append to a JSON log under tool_artifacts/messages/
  - webhook:  POST to SEND_MESSAGE_WEBHOOK_URL
  - email:    SMTP (uses SEND_MESSAGE_SMTP_* env vars)
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

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)

_MSG_DIR = Path(
    os.environ.get("ZHANLU_MESSAGE_LOG_DIR", "/tmp/zhanlu_messages")
)


async def _send_message(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    backend = (args.get("backend") or os.environ.get("SEND_MESSAGE_BACKEND", "log")).lower()
    recipient = (args.get("to") or "").strip()
    body = (args.get("body") or "").strip()
    subject = (args.get("subject") or "").strip()
    if not recipient or not body:
        return {"success": False, "error": "to and body are required"}

    record = {
        "id": uuid.uuid4().hex[:12],
        "backend": backend,
        "to": recipient,
        "subject": subject,
        "body": body,
        "timestamp": time.time(),
    }

    if backend == "log":
        _MSG_DIR.mkdir(parents=True, exist_ok=True)
        path = _MSG_DIR / f"{record['id']}.json"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"success": True, "message_id": record["id"], "logged_to": str(path)}

    if backend == "webhook":
        url = os.environ.get("SEND_MESSAGE_WEBHOOK_URL")
        if not url:
            return {"success": False, "error": "SEND_MESSAGE_WEBHOOK_URL is not set"}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, json={"to": recipient, "subject": subject, "body": body})
                resp.raise_for_status()
        except Exception as exc:
            return {"success": False, "error": f"Webhook send failed: {exc}"}
        return {"success": True, "message_id": record["id"], "delivered_to": "webhook"}

    if backend == "email":
        return {
            "success": False,
            "error": "SMTP backend not yet implemented. Use backend='log' or 'webhook'.",
        }

    return {"success": False, "error": f"Unknown backend: {backend!r}"}


SEND_MESSAGE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "send_message",
        "description": (
            "Generic message dispatcher. Backends: 'log' (default — "
            "writes to a JSON file), 'webhook' (POSTs to "
            "SEND_MESSAGE_WEBHOOK_URL), 'email' (SMTP, not yet "
            "implemented)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient identifier (email, phone, channel id, etc.)."},
                "subject": {"type": "string", "description": "Optional subject / title."},
                "body": {"type": "string", "description": "The message body."},
                "backend": {"type": "string", "enum": ["log", "webhook", "email"], "default": "log"},
            },
            "required": ["to", "body"],
        },
    },
}

registry.register(
    name="send_message",
    schema=SEND_MESSAGE_SCHEMA,
    handler=_send_message,
    category="communication",
    toolset="communication",
    description="Generic message dispatcher (log / webhook / email).",
    emoji="✉️",
    max_result_size_chars=5_000,
)
