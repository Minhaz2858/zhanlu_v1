"""Token estimation utilities for compaction.

Uses a simple heuristic (4 chars ≈ 1 token, with 4/3 padding) when tiktoken
is unavailable, and tiktoken for precise estimation when installed.
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)

# Conservative padding factor (matches OpenHarness)
TOKEN_ESTIMATION_PADDING = 4 / 3
_DEFAULT_VISION_IMAGE_TOKEN_ESTIMATE = 3_072

# Cache the tiktoken encoder to avoid re-initialization on every call
_tiktoken_encoder = None
_tiktoken_available: bool | None = None


def _get_tiktoken_encoder():
    """Lazily load tiktoken encoder. Returns None if tiktoken is unavailable."""
    global _tiktoken_encoder, _tiktoken_available
    if _tiktoken_available is False:
        return None
    if _tiktoken_encoder is not None:
        return _tiktoken_encoder
    try:
        import tiktoken
        _tiktoken_encoder = tiktoken.get_encoding("cl100k_base")
        _tiktoken_available = True
        return _tiktoken_encoder
    except ImportError:
        log.debug("tiktoken not installed, using heuristic token estimation")
        _tiktoken_available = False
        return None
    except Exception as exc:
        log.warning("Failed to initialize tiktoken: %s, using heuristic", exc)
        _tiktoken_available = False
        return None


def estimate_text_tokens(text: str) -> int:
    """Estimate token count for a text string."""
    if not text:
        return 0
    encoder = _get_tiktoken_encoder()
    if encoder is not None:
        try:
            return len(encoder.encode(text))
        except Exception:
            pass
    # Heuristic: ~4 chars per token
    return max(1, len(text) // 4)


def _estimate_image_tokens(image_data: Any) -> int:
    """Estimate tokens for an image block."""
    raw = os.environ.get("ZHANLU_IMAGE_TOKEN_ESTIMATE", "").strip()
    if raw:
        try:
            return max(64, int(raw))
        except ValueError:
            pass
    return _DEFAULT_VISION_IMAGE_TOKEN_ESTIMATE


def estimate_message_tokens(message: dict[str, Any]) -> int:
    """Estimate token count for a single message dict.

    Handles Zhanlu's message format:
    - {"role": "user"/"assistant"/"system"/"tool", "content": "..."}
    - {"role": "assistant", "content": None, "tool_calls": [...]}
    - {"role": "tool", "tool_call_id": "...", "content": "..."}
    """
    total = 0

    # Role overhead
    role = message.get("role", "")
    total += estimate_text_tokens(role)

    # Content
    content = message.get("content")
    if isinstance(content, str):
        total += estimate_text_tokens(content)
    elif isinstance(content, list):
        # Content blocks (multimodal)
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    total += estimate_text_tokens(block.get("text", ""))
                elif block.get("type") == "image_url":
                    total += _estimate_image_tokens(block)
            elif isinstance(block, str):
                total += estimate_text_tokens(block)

    # Tool calls (assistant messages)
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if isinstance(tc, dict):
                func = tc.get("function", {})
                total += estimate_text_tokens(func.get("name", ""))
                total += estimate_text_tokens(func.get("arguments", ""))

    return int(total * TOKEN_ESTIMATION_PADDING)


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimate total tokens for a conversation message list."""
    total = 0
    for msg in messages:
        total += estimate_message_tokens(msg)
    return total
