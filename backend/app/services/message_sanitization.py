"""Message and tool-payload sanitization helpers.

Pure functions that walk OpenAI-format message lists and repair/strip
problematic content before each LLM API call. Prevents:

1. **Lone surrogate crashes**: U+D800-U+DFFF code points are invalid in
   UTF-8 and crash ``json.dumps()`` inside the OpenAI SDK.
2. **Malformed tool_call arguments**: models can emit truncated JSON,
   trailing commas, Python ``None``, etc. — causes HTTP 400.
3. **Interrupted tool sequences**: an orphaned ``tool`` message at the end
   of the list (turn cut short) causes role-alternation violations with
   strict providers.

Inspired by Hermes' ``agent/message_sanitization.py``, adapted for
Zhanlu's message format.
"""
from __future__ import annotations

import ast
import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Lone surrogate code points are invalid in UTF-8 and crash json.dumps.
_SURROGATE_RE = re.compile(r'[\ud800-\udfff]')


def sanitize_surrogates(text: str) -> str:
    """Replace lone surrogate code points with U+FFFD (replacement character).

    Surrogates are invalid in UTF-8 and will crash ``json.dumps()`` inside
    the OpenAI SDK. This is a fast no-op when the text contains no surrogates.
    """
    if not isinstance(text, str):
        return text
    if _SURROGATE_RE.search(text):
        return _SURROGATE_RE.sub('\ufffd', text)
    return text


def _sanitize_structure_surrogates(payload: Any) -> bool:
    """Replace surrogate code points in nested dict/list payloads in-place.

    Returns True if any surrogates were replaced.
    """
    found = False

    def _walk(node):
        nonlocal found
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str):
                    if _SURROGATE_RE.search(value):
                        node[key] = _SURROGATE_RE.sub('\ufffd', value)
                        found = True
                elif isinstance(value, (dict, list)):
                    _walk(value)
        elif isinstance(node, list):
            for idx, value in enumerate(node):
                if isinstance(value, str):
                    if _SURROGATE_RE.search(value):
                        node[idx] = _SURROGATE_RE.sub('\ufffd', value)
                        found = True
                elif isinstance(value, (dict, list)):
                    _walk(value)

    _walk(payload)
    return found


def sanitize_messages_surrogates(messages: list[dict]) -> bool:
    """Sanitize surrogate characters from all string content in a messages list.

    Walks message dicts in-place. Returns True if any surrogates were found
    and replaced. Covers content, name, tool_call metadata/arguments, AND
    any additional string or nested structured fields (reasoning_content, etc.).
    """
    found = False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str) and _SURROGATE_RE.search(content):
            msg["content"] = _SURROGATE_RE.sub('\ufffd', content)
            found = True
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str) and _SURROGATE_RE.search(text):
                        part["text"] = _SURROGATE_RE.sub('\ufffd', text)
                        found = True
        name = msg.get("name")
        if isinstance(name, str) and _SURROGATE_RE.search(name):
            msg["name"] = _SURROGATE_RE.sub('\ufffd', name)
            found = True
        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                tc_id = tc.get("id")
                if isinstance(tc_id, str) and _SURROGATE_RE.search(tc_id):
                    tc["id"] = _SURROGATE_RE.sub('\ufffd', tc_id)
                    found = True
                fn = tc.get("function")
                if isinstance(fn, dict):
                    fn_name = fn.get("name")
                    if isinstance(fn_name, str) and _SURROGATE_RE.search(fn_name):
                        fn["name"] = _SURROGATE_RE.sub('\ufffd', fn_name)
                        found = True
                    fn_args = fn.get("arguments")
                    if isinstance(fn_args, str) and _SURROGATE_RE.search(fn_args):
                        fn["arguments"] = _SURROGATE_RE.sub('\ufffd', fn_args)
                        found = True
        # Walk any additional string / nested fields
        for key, value in msg.items():
            if key in {"content", "name", "tool_calls", "role"}:
                continue
            if isinstance(value, str):
                if _SURROGATE_RE.search(value):
                    msg[key] = _SURROGATE_RE.sub('\ufffd', value)
                    found = True
            elif isinstance(value, (dict, list)):
                if _sanitize_structure_surrogates(value):
                    found = True
    return found


def _try_python_literal_eval(raw: str) -> str | None:
    """Repair single-quoted Python dict literals by using ast.literal_eval.

    qwen3.6-27b emits tool_call args as Python-style dict literals:
        'query': 'show me sales'
        {'query': 'show me sales'}
    Standard JSON parsers reject single quotes. ast.literal_eval handles
    them, and we re-serialize as valid JSON.

    Returns valid JSON string, or None if ast.literal_eval also fails
    (or if the input is already valid JSON — caller should use json.loads
    directly in that case).
    """
    if not raw:
        return None
    # If it's already valid JSON, don't touch it (fast path).
    try:
        json.loads(raw)
        return None  # already valid; caller handles it
    except (json.JSONDecodeError, TypeError):
        pass
    # Try ast.literal_eval. If the raw has no braces, wrap it.
    candidate = raw.strip()
    if not candidate.startswith("{"):
        candidate = "{" + candidate
    if not candidate.endswith("}"):
        candidate = candidate + "}"
    try:
        parsed = ast.literal_eval(candidate)
        if isinstance(parsed, dict):
            return json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)
    except (ValueError, SyntaxError):
        return None
    return None


def repair_tool_call_arguments(raw_args: str, tool_name: str = "?") -> str:
    """Attempt to repair malformed tool_call argument JSON.

    Models can produce truncated JSON, trailing commas, Python ``None``,
    literal control characters, etc. The API rejects these with HTTP 400
    "invalid tool call arguments". This function applies common repairs;
    if all fail it returns ``"{}"`` so the request succeeds.

    Returns valid JSON string.
    """
    raw_stripped = raw_args.strip() if isinstance(raw_args, str) else ""

    # Fast-path: empty / whitespace-only -> empty object
    if not raw_stripped:
        return "{}"

    # Python-literal None -> normalise to {}
    if raw_stripped == "None":
        return "{}"

    # Repair pass 0: literal control chars inside JSON strings.
    # json.loads with strict=False accepts these and lets us re-serialise.
    try:
        parsed = json.loads(raw_stripped, strict=False)
        reserialised = json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)
        return reserialised
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # Repair pass 0.5: Python-style dict literals (single-quoted).
    # qwen3.6-27b emits 'query': 'value' instead of {"query": "value"}.
    # ast.literal_eval handles this; we re-serialize as valid JSON.
    _py_repaired = _try_python_literal_eval(raw_stripped)
    if _py_repaired is not None:
        logger.warning(
            "Repaired Python-literal tool_call arguments for %s (single-quoted -> JSON)",
            tool_name,
        )
        return _py_repaired

    # Attempt common JSON repairs
    fixed = raw_stripped
    # 1. Strip trailing commas before } or ]
    fixed = re.sub(r',\s*([}\]])', r'\1', fixed)
    # 2. Close unclosed structures
    open_curly = fixed.count('{') - fixed.count('}')
    open_bracket = fixed.count('[') - fixed.count(']')
    if open_curly > 0:
        fixed += '}' * open_curly
    if open_bracket > 0:
        fixed += ']' * open_bracket
    # 3. Remove excess closing braces/brackets
    for _ in range(50):
        try:
            json.loads(fixed)
            break
        except json.JSONDecodeError:
            if fixed.endswith('}') and fixed.count('}') > fixed.count('{'):
                fixed = fixed[:-1]
            elif fixed.endswith(']') and fixed.count(']') > fixed.count('['):
                fixed = fixed[:-1]
            else:
                break

    try:
        json.loads(fixed)
        logger.warning("Repaired malformed tool_call arguments for %s", tool_name)
        return fixed
    except json.JSONDecodeError:
        pass

    # Last resort: replace with empty object
    logger.warning("Unrepairable tool_call arguments for %s — replaced with {}", tool_name)
    return "{}"


def close_interrupted_tool_sequence(messages: list[dict], final_response: str = "") -> bool:
    """Append a synthetic assistant turn when an interrupted tail is a tool result.

    A turn cut short can leave the transcript ending on a raw ``tool`` message
    — the next user message lands as ``tool -> user``, a role-alternation
    violation that strict providers reject.

    Mutates ``messages`` in place. Returns True if a closing turn was appended.
    """
    if not messages:
        return False
    last = messages[-1]
    if not isinstance(last, dict) or last.get("role") != "tool":
        return False
    text = final_response if isinstance(final_response, str) else ""
    messages.append({
        "role": "assistant",
        "content": text.strip() or "Operation interrupted.",
    })
    return True


def sanitize_messages(messages: list[dict]) -> bool:
    """Run all sanitization passes on a message list before an API call.

    Passes (in order):
    1. Surrogate sanitization — replace lone surrogates with U+FFFD.
    2. Tool_call argument repair — fix malformed JSON in tool_calls.
    3. Close interrupted tool sequences — append synthetic assistant turn.
    4. Mid-list system message guard — demote any system message not at
       index 0 to role=user (vLLM compatibility, 2026-08-25).

    Mutates ``messages`` in place. Returns True if any changes were made.
    """
    changed = False

    # Pass 1: surrogates
    if sanitize_messages_surrogates(messages):
        changed = True

    # Pass 2: repair malformed tool_call arguments
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        tool_calls = msg.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function")
            if not isinstance(fn, dict):
                continue
            args = fn.get("arguments")
            if isinstance(args, str):
                repaired = repair_tool_call_arguments(args, fn.get("name", "?"))
                if repaired != args:
                    fn["arguments"] = repaired
                    changed = True

    # Pass 3: close interrupted tool sequences
    if close_interrupted_tool_sequence(messages):
        changed = True

    # Pass 4: demote mid-list system messages to user role.
    # 2026-08-25: vLLM (qwen3.6-27b) rejects any system message not at
    # index 0 with HTTP 400 "System message must be at the beginning."
    # This is a defense-in-depth guard: even if a code path appends a
    # system message mid-conversation, this pass catches it. DeepSeek
    # API tolerates mid-list system messages, so demoting to user is
    # backward-compatible (the content still reaches the LLM).
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        if idx == 0:
            continue  # index 0 system msg is always OK
        if msg.get("role") == "system":
            msg["role"] = "user"
            changed = True
            logger.info(
                "Demoted mid-list system message at index %d to role=user (vLLM compat)",
                idx,
            )

    return changed


__all__ = [
    "sanitize_surrogates",
    "sanitize_messages_surrogates",
    "repair_tool_call_arguments",
    "close_interrupted_tool_sequence",
    "sanitize_messages",
]
