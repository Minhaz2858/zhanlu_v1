"""Integration router — LLM invocation and file upload endpoints."""

import os
import json
import re
import uuid
import logging
from datetime import datetime
import httpx
from fastapi import APIRouter, UploadFile, File, HTTPException, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from app.config import settings
from app.deps import get_current_user_required, get_db
from app.services.llm_service import (
    build_llm_payload,
    llm_headers as _llm_headers,
    llm_url as _llm_url,
    get_model,
)
from app.services.tool_registry import registry

router = APIRouter(tags=["integrations"])

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Live-web grounding for the raw InvokeLLM / InvokeLLMStream path.
#
# The raw integration path is a tool-less LLM proxy: when the user message is
# time-sensitive ("today's news", "latest price") the base model has no browsing
# and refuses ("I don't have real-time information"). This hook runs
# ``web_search`` and prepends the results to the prompt — but only when the
# message is time-sensitive, only when the search tool is registered, and never
# if anything goes wrong (a search outage must not break the chat).
# ---------------------------------------------------------------------------

_TIME_SENSITIVE_RE = re.compile(
    r"\b("
    r"today|todays?|tonight|tomorrow|yesterday|"
    r"latest|newest|current|currently|now|live|breaking|recent|recently|"
    r"news|headline|headlines|update[sd]?|trending|"
    r"weather|forecast|price|prices|stock|stocks|market|score|scores|"
    r"this (?:week|month|year|morning|evening|afternoon)"
    r")\b",
    re.IGNORECASE,
)

# Hard cap on the search query handed to web_search; very long prompts would
# otherwise produce absurd queries.
_MAX_QUERY_CHARS = 300
# Max results injected into the prompt — keeps prompt size bounded.
_MAX_RESULTS = 5


def _is_time_sensitive(text: str) -> bool:
    return bool(text) and bool(_TIME_SENSITIVE_RE.search(text))


def _query_from(prompt: str, messages: list[dict] | None) -> str:
    """Derive the search query: last USER message when a ``messages`` list is
    given (avoids feeding the whole system+history blob), else the prompt."""
    source = prompt or ""
    if isinstance(messages, list):
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, str) and content.strip():
                    source = content
                break
    return source.strip()[:_MAX_QUERY_CHARS]


async def _maybe_ground_prompt(
    prompt: str,
    messages: list[dict] | None = None,
    db=None,
    user_id=None,
) -> str:
    """Return ``prompt``, optionally prefixed with live web-search results.

    Fail-safe by design: returns the prompt unchanged whenever the message is
    not time-sensitive, the ``web_search`` tool is not registered, the search
    reports failure / returns nothing, or the handler raises.
    """
    if not _is_time_sensitive(prompt):
        return prompt

    handler = registry.get_handler("web_search")
    if handler is None:
        return prompt

    query = _query_from(prompt, messages)
    if not query:
        return prompt

    try:
        result = await handler({"query": query}, db, user_id, context=None)
    except Exception as exc:  # noqa: BLE001 — a search outage must not break chat
        logger.warning("grounding: web_search handler raised (non-fatal): %s", exc)
        return prompt

    if not isinstance(result, dict) or not result.get("success"):
        return prompt

    results = result.get("results") or []
    if not results:
        return prompt

    lines = [
        "[Live web results — current information retrieved just now; "
        "cite the source URLs below:]",
        "",
    ]
    for idx, item in enumerate(results[:_MAX_RESULTS], start=1):
        if not isinstance(item, dict):
            continue
        title = item.get("title") or item.get("name") or ""
        url = item.get("url") or item.get("link") or ""
        desc = item.get("description") or item.get("snippet") or ""
        lines.append(f"{idx}. {title}")
        if url:
            lines.append(f"   Source: {url}")
        if desc:
            lines.append(f"   {desc}")
        lines.append("")

    block = "\n".join(lines).rstrip() + "\n\n"
    return f"{block}{prompt}"



def _with_grounded_prompt(body: dict, grounded: str) -> dict:
    """Return a copy of ``body`` with ``prompt`` (and the last user message, when
    a ``messages`` list is present) replaced by the grounded prompt."""
    new_body = dict(body)
    new_body["prompt"] = grounded
    msgs = new_body.get("messages")
    if isinstance(msgs, list) and msgs:
        copied = [dict(m) if isinstance(m, dict) else m for m in msgs]
        for m in reversed(copied):
            if isinstance(m, dict) and m.get("role") == "user":
                m["content"] = grounded
                break
        new_body["messages"] = copied
    return new_body


@router.post("/apps/{app_id}/integration-endpoints/Core/InvokeLLM")
async def invoke_llm(
    app_id: str,
    body: dict = None,
    user=Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    """Invoke the configured LLM (OpenAI-compatible API) and return the response."""
    body = body or {}
    prompt = body.get("prompt", "")
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")

    # Always use the model configured in .env — ignore the model sent by the
    # frontend (Base44 apps hardcode names like "gpt_5_4" which won't exist
    # on a self-hosted OpenAI-compatible API such as DeepSeek).
    model = settings.LLM_MODEL

    # Live-web grounding for time-sensitive prompts (fail-safe: returns the
    # prompt unchanged if search is unavailable or anything goes wrong).
    grounded = await _maybe_ground_prompt(
        prompt, messages=body.get("messages"), db=db, user_id=getattr(user, "id", None)
    )
    if grounded and grounded != prompt:
        body = _with_grounded_prompt(body, grounded)

    payload, messages, json_schema = build_llm_payload(body, model)


    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(_llm_url(), headers=_llm_headers(), json=payload)
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=e.response.status_code,
            detail=f"LLM API error: {e.response.text}",
        )
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"LLM request failed: {str(e)}")

    data = resp.json()
    choice = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})

    result = {
        "model": model,
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
    }

    if json_schema:
        try:
            parsed = json.loads(choice)
            # Spread structured fields (text, clarify, trace, create_resource, etc.)
            # to the top level so the frontend can access them directly.
            if isinstance(parsed, dict):
                result.update(parsed)
            else:
                result["response"] = parsed
        except (json.JSONDecodeError, TypeError):
            result["response"] = choice
    else:
        result["response"] = choice

    return result


@router.post("/apps/{app_id}/integration-endpoints/Core/InvokeLLMStream")
async def invoke_llm_stream(
    app_id: str,
    body: dict = None,
    user=Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    """Stream the LLM response via Server-Sent Events (SSE).

    Emits ``data: {"delta": "..."}`` for each token chunk and a final
    ``data: {"done": true, "content": "...", "model": "...", "usage": {...}}``
    event when the stream completes.
    """
    body = body or {}
    prompt = body.get("prompt", "")
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")

    model = settings.LLM_MODEL

    # Live-web grounding for time-sensitive prompts (fail-safe: returns the
    # prompt unchanged if search is unavailable or anything goes wrong).
    grounded = await _maybe_ground_prompt(
        prompt, messages=body.get("messages"), db=db, user_id=getattr(user, "id", None)
    )
    if grounded and grounded != prompt:
        body = _with_grounded_prompt(body, grounded)

    payload, messages, json_schema = build_llm_payload(body, model)
    payload["stream"] = True


    # Pre-flight check: if no API key is configured, emit a clear error event
    # to the SSE stream so the frontend surfaces a readable error in the
    # "Send errors" counter instead of hanging until client-side timeout.
    # This protects against the common dev mistake of starting the stack
    # without setting OPENAI_API_KEY (or its provider-specific equivalent).
    if not settings.OPENAI_API_KEY:
        async def missing_key_stream():
            yield (
                "data: " + json.dumps({
                    "error": (
                        "LLM not configured: set OPENAI_API_KEY in /root/zhanlu/.env "
                        "and restart the backend. "
                        f"Currently calling {settings.OPENAI_BASE_URL} with model '{model}'."
                    ),
                }) + "\n\n"
            )
        return StreamingResponse(
            missing_key_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def event_stream():
        full_content = ""
        usage = {}
        try:
            async with httpx.AsyncClient(timeout=180.0) as client:
                async with client.stream(
                    "POST", _llm_url(), headers=_llm_headers(), json=payload
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data: "):
                            continue
                        raw = line[6:]
                        if raw.strip() == "[DONE]":
                            break
                        try:
                            chunk = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if "usage" in chunk:
                            usage = chunk["usage"]
                        choices = chunk.get("choices", [])
                        if choices:
                            delta = choices[0].get("delta", {})
                            token = delta.get("content", "")
                            if token:
                                full_content += token
                                yield f"data: {json.dumps({'delta': token})}\n\n"
            # Final event with complete response
            final = {"done": True, "content": full_content, "model": model, "usage": {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            }}
            yield f"data: {json.dumps(final)}\n\n"
        except httpx.HTTPStatusError as e:
            # The response was opened via client.stream(...), so the
            # body is still unread when raise_for_status fires here.
            # Accessing e.response.text directly raises
            # httpx.ResponseNotRead ("Attempted to access streaming
            # response content, without having called read()"), which
            # then surfaces to the chat as that opaque error string.
            # Consume the body first; fall back to a status-only message
            # if the read itself fails.
            try:
                await e.response.aread()
                err_body = e.response.text or "(empty body)"
            except Exception:
                err_body = f"(body unreadable; status {e.response.status_code})"
            yield f"data: {json.dumps({'error': f'LLM API error: {err_body}'})}\n\n"
        except httpx.RequestError as e:
            yield f"data: {json.dumps({'error': f'LLM request failed: {str(e)}'})}\n\n"
        except Exception as e:  # noqa: BLE001 — fail-safe: never leak mid-stream
            # Any other error (provider SDK bug, malformed payload, unexpected
            # runtime exception) must surface as a readable SSE ``error`` event
            # rather than propagating out of the generator — which Starlette
            # wraps in a TaskGroup ExceptionGroup and returns as an opaque 500,
            # exactly the "opaque Send errors" class of bug this endpoint exists
            # to prevent.
            logger.exception("invoke_llm_stream: unexpected error mid-stream")
            yield f"data: {json.dumps({'error': f'LLM stream failed: {type(e).__name__}: {e}'})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _upload_blocked_reason(user, db) -> str | None:
    """Return a reason string when file upload is disabled for ``user``, else None.

    Reads the per-user ``UserSetting.file_upload_enabled`` row (default True
    when no row exists). Exposed as a pure function so the gating logic is
    unit-testable without booting FastAPI. Fails OPEN (returns None) on any
    settings-read error — the endpoint's own 500 path covers real storage
    failures, and an absent row means "enabled" by model default.
    """
    if user is None or db is None:
        return None
    try:
        from app.models import UserSetting

        setting = (
            db.query(UserSetting)
            .filter(UserSetting.created_by_id == user.id)
            .first()
        )
    except Exception:
        logger.warning(
            "upload gate: settings read failed — allowing upload (default)",
            exc_info=True,
        )
        return None
    if setting is not None and setting.file_upload_enabled is False:
        return (
            "File upload is disabled for this user. "
            "Enable 'Allow file upload' in Settings → Chat."
        )
    return None


@router.post("/apps/{app_id}/integration-endpoints/Core/UploadFile")
async def upload_file(
    app_id: str,
    file: UploadFile = File(...),
    user=Depends(get_current_user_required),
    db: Session = Depends(get_db),
):
    """Upload a file and return its URL. Saves to the uploads/ directory.

    Phase 1 hardening: enforces an extension allowlist + 100MB size cap so
    the agent can't be tricked into reading arbitrary paths or DoSing the
    server with a multi-GB upload. The allowlist covers the text formats
    the extractors support (pdf, docx, pptx, xlsx, csv, txt, md, json,
    html) plus common image formats for the multimodal LLM path.

    Phase 2 gating: refuses uploads (403) when the user's
    ``file_upload_enabled`` setting is off — the Settings toggle is now
    enforced server-side, not just cosmetic.
    """
    # ── Settings gate (file_upload_enabled) ─────────────────────────────
    _blocked = _upload_blocked_reason(user, db)
    if _blocked:
        raise HTTPException(status_code=403, detail=_blocked)

    # ── Extension allowlist ────────────────────────────────────────────
    # Match by extension (lowercased, with the dot). Content-Type is
    # ignored — it's user-controlled and unreliable.
    _ALLOWED_EXTS = {
        # Text / document
        ".txt", ".md", ".csv", ".json", ".html", ".htm",
        ".pdf", ".docx", ".pptx", ".ppt", ".xlsx", ".xls",
        # Images (multimodal LLM path)
        ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".tif",
        # Audio (Whisper transcription — Phase 2)
        ".mp3", ".m4a", ".wav",
        # Video (Whisper transcription — Phase 2)
        ".mp4", ".mov", ".webm",
    }
    _MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB

    raw_name = file.filename or ""
    ext = os.path.splitext(raw_name)[1].lower()
    if not ext:
        raise HTTPException(
            status_code=400,
            detail="File has no extension — cannot determine type.",
        )
    if ext not in _ALLOWED_EXTS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"File type '{ext}' is not allowed. Supported: "
                "pdf, docx, pptx, xlsx, csv, txt, md, json, html, htm, "
                "png, jpg, jpeg, webp, gif, bmp, tiff, mp3, m4a, wav, "
                "mp4, mov, webm."
            ),
        )

    unique_name = f"{uuid.uuid4().hex}{ext}"
    try:
        upload_dir = settings.upload_path
    except Exception as e:
        logger.exception("upload directory unavailable: %s", settings.UPLOAD_DIR)
        raise HTTPException(
            status_code=500,
            detail=f"Upload storage unavailable: {e}",
        )
    file_path = upload_dir / unique_name

    # ── Stream to disk with a size cap ─────────────────────────────────
    # Reading the whole body into memory first (the old `await
    # file.read()`) spikes RAM on a 100MB upload. Stream chunks instead
    # and abort the moment the cap is exceeded.
    total = 0
    try:
        with open(file_path, "wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)  # 1MB chunks
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_UPLOAD_BYTES:
                    f.close()
                    try:
                        file_path.unlink()
                    except Exception:
                        pass
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"File too large: {total} bytes exceeds the "
                            f"{_MAX_UPLOAD_BYTES // (1024 * 1024)}MB cap."
                        ),
                    )
                f.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("upload write failed: %s", file_path)
        try:
            file_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"Upload failed: {e}")

    # Normalise file_type to the extension without the dot (matches what
    # extractors.extract_text expects). The old code returned the raw
    # content_type which was unreliable and often "application/octet-stream".
    file_type = ext.lstrip(".")

    return {
        "file_url": f"/api/uploads/{unique_name}",
        "file_name": raw_name,
        "file_type": file_type,
        "size": total,
    }
