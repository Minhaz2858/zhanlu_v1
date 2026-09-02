"""Integration router — LLM invocation and file upload endpoints."""

import os
import json
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

router = APIRouter(tags=["integrations"])

logger = logging.getLogger(__name__)


@router.post("/apps/{app_id}/integration-endpoints/Core/InvokeLLM")
async def invoke_llm(app_id: str, body: dict = None, user=Depends(get_current_user_required)):
    """Invoke the configured LLM (OpenAI-compatible API) and return the response."""
    body = body or {}
    prompt = body.get("prompt", "")
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")

    # Always use the model configured in .env — ignore the model sent by the
    # frontend (Base44 apps hardcode names like "gpt_5_4" which won't exist
    # on a self-hosted OpenAI-compatible API such as DeepSeek).
    model = settings.LLM_MODEL
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
async def invoke_llm_stream(app_id: str, body: dict = None, user=Depends(get_current_user_required)):
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
