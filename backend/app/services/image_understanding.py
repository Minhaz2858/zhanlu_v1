"""Vision bridge — lets ANY chat model answer questions about images.

Live 2026-10-13: the deployment's main model (deepseek-chat) is text-only.
The v3 loop already builds OpenAI image content blocks for pasted/uploaded
images, but a text-only model cannot see them — the agent answered "the
image didn't come through in a readable form" for a perfectly legible
screenshot. This bridge closes the gap: each attached image is run through
a configurable OpenAI-compatible VISION endpoint and the analysis (exact
text transcription + visual description, focused on the user's question)
is injected into the system prompt, so the main model — whatever it is —
has eyes.

Config (app.config.settings):
  VISION_MODEL      vision-capable model name; default "" = derive from
                    VISION_BASE_URL/OPENAI_* deployment (must be set for
                    the bridge to activate)
  VISION_BASE_URL   OpenAI-compatible base; default "" = OPENAI_BASE_URL
  VISION_API_KEY    default "" = OPENAI_API_KEY
  VISION_BRIDGE_MODE  auto (default) = bridge only when the main model
                    does not look vision-capable; always; off

Failures are honest and non-fatal: when the bridge is unconfigured or a
call fails, the injected note says exactly that — the agent relays the
limitation instead of pretending it saw the image.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

#: Upper bound on images analyzed per turn — bounds cost/latency; the
#: native content-block path (build_image_content_blocks) caps at 5 too.
MAX_IMAGES_PER_TURN = 3

#: Skip pathologically large images rather than burning the vision call.
_MAX_IMAGE_BYTES = 12 * 1024 * 1024

#: Substrings that mark a main model as vision-capable (it receives the
#: native image content blocks directly, no bridge needed). Conservative:
#: a false "capable" verdict silently blinds a text-only model, while a
#: false "not capable" just costs one extra structured analysis call.
_VISION_MODEL_MARKERS = (
    "gpt-4o", "gpt-4.1", "gpt-4-turbo", "chatgpt-4o", "o3", "o4-",
    "claude-3", "claude-sonnet", "claude-opus", "claude-haiku",
    "gemini", "qwen-vl", "qwenvl", "qvq", "glm-4v", "glmv", "pixtral",
    "llama-3.2-vision", "llama3.2-vision", "llama-4", "internvl",
    "minimax-vl", "abab", "moonshot-v1-vision", "kimi-v", "grok-vision",
    "doubao-1.5-vision", "doubao-vision", "ernie-v", "hunyuan-vision",
)

#: Versioned -vl names ("qwen2.5-vl-72b", "internvl2-8b") don't contain
#: the plain markers above — catch any <version>-vl pattern.
import re as _re

_VISION_VL_RE = _re.compile(r"[\w.](-vl|vl-|-vision|vision-)", _re.IGNORECASE)

_BRIDGE_PROMPT = """You are the eyes of a text-only assistant. Analyze this image so the \
assistant can answer the user's question: "{question}"

Produce, in this order:
1. EXACT TEXT — transcribe ALL text visible in the image verbatim, \
preserving numbers, codes, units and layout. If there is no text, say so.
2. CONTENT — describe what the image shows (chart type, tables, UI, \
diagram, photo…) and the key values or relationships in it, focused on \
what the question needs.
Be precise with every number. Do not speculate beyond what is visible."""


def model_looks_vision_capable(model_name: str | None) -> bool:
    """True when the model name suggests native vision support."""
    name = (model_name or "").lower()
    return (
        any(marker in name for marker in _VISION_MODEL_MARKERS)
        or bool(_VISION_VL_RE.search(name))
    )


def _vision_endpoint() -> tuple[str, str, str] | None:
    """(base_url, api_key, model) of the vision channel, or None when
    the bridge has nothing usable configured."""
    model = (settings.VISION_MODEL or "").strip()
    base = (settings.VISION_BASE_URL or "").strip() or (
        settings.OPENAI_BASE_URL or ""
    ).strip()
    key = (settings.VISION_API_KEY or "").strip() or (
        settings.OPENAI_API_KEY or ""
    ).strip()
    if not (model and base and key):
        return None
    return base.rstrip("/"), key, model


def should_bridge(model_name: str | None) -> bool:
    """Bridge decision for the resolved main model."""
    mode = (settings.VISION_BRIDGE_MODE or "auto").strip().lower()
    if mode == "off":
        return False
    if mode == "always":
        return True
    return not model_looks_vision_capable(model_name)


def _ext_to_mime(ext: str) -> str:
    return {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "webp": "image/webp",
        "bmp": "image/bmp",
        "tiff": "image/tiff",
        "tif": "image/tiff",
    }.get(ext, "image/png")


def _analyze_one(local: Path, question: str, endpoint: tuple[str, str, str]) -> dict:
    """One vision completion for one image. Returns an analysis dict."""
    base, key, model = endpoint
    data = local.read_bytes()
    if len(data) > _MAX_IMAGE_BYTES:
        return {"ok": False, "error": "image too large (>12MB)"}
    b64 = base64.b64encode(data).decode("ascii")
    mime = _ext_to_mime(local.suffix.lower().lstrip("."))
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _BRIDGE_PROMPT.format(
                        question=(question or "general understanding")[:500],
                    )},
                    {"type": "image_url",
                     "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ],
            }
        ],
        "max_tokens": 1500,
        "temperature": 0.1,
    }
    try:
        resp = httpx.post(
            f"{base}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            timeout=90,
        )
        resp.raise_for_status()
        choices = resp.json().get("choices") or []
        if not choices:
            return {"ok": False, "error": "vision model returned no choices"}
        text = (choices[0].get("message") or {}).get("content") or ""
        if not text.strip():
            return {"ok": False, "error": "vision model returned empty analysis"}
        return {"ok": True, "analysis": text.strip(), "model": model}
    except Exception as exc:  # noqa: BLE001 — non-fatal by contract
        logger.warning("vision bridge call failed: %s", exc)
        return {"ok": False, "error": f"vision call failed: {exc}"}


def analyze_images(file_urls: list[str], question: str = "") -> dict:
    """Run every attached image through the vision channel.

    Returns {"ran": bool, "configured": bool, "results": [
        {"file_url", "name", "ok", "analysis"|"error", "model"?}, …]}
    ``ran`` is False when the bridge is off/unconfigured or no images
    resolved — the caller then injects an honest note instead.
    """
    from app.services.document_ingestion.service import _resolve_local_path

    endpoint = _vision_endpoint()
    if endpoint is None:
        return {"ran": False, "configured": False, "results": []}

    results: list[dict] = []
    image_exts = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tiff", "tif"}
    for furl in (file_urls or [])[:MAX_IMAGES_PER_TURN]:
        if not isinstance(furl, str) or not furl:
            continue
        name = furl.rsplit("/", 1)[-1]
        local = _resolve_local_path(furl)
        if local is None or not local.exists():
            results.append({"file_url": furl, "name": name,
                            "ok": False, "error": "file not found on server"})
            continue
        ext = local.suffix.lower().lstrip(".")
        if ext not in image_exts:
            continue  # non-image attachments go the normal document path
        r = _analyze_one(local, question, endpoint)
        results.append({"file_url": furl, "name": name, **r})
    return {"ran": True, "configured": True, "results": results}


def build_bridge_prompt(file_urls: list[str], question: str) -> str | None:
    """Analyze the attached images and return the system-prompt block.

    Returns None when there is nothing to add (no images, bridge off).
    Always returns SOMETHING when images were attached — on failure the
    block states the limitation honestly so the agent relays it.
    """
    image_exts = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tiff", "tif"}
    image_urls = [
        u for u in (file_urls or [])
        if isinstance(u, str)
        and u.rsplit(".", 1)[-1].lower() in image_exts
    ]
    if not image_urls:
        return None

    outcome = analyze_images(image_urls, question)
    if not outcome["ran"]:
        return (
            "[Attached images] The user attached image(s), but this "
            "deployment has NO vision channel configured (set VISION_MODEL "
            "+ a vision-capable API key). You cannot see the images' "
            "content. If the question depends on the image, say so plainly "
            "and ask the user to configure a vision model or describe the "
            "image — do NOT guess what the images contain."
        )

    lines = ["[Attached image analysis — produced by the vision bridge, "
             "treat as ground truth about the images]"]
    for r in outcome["results"]:
        if r.get("ok"):
            lines.append(f"--- Image: {r['name']} ---\n{r['analysis']}")
        else:
            lines.append(
                f"--- Image: {r['name']} — analysis FAILED: {r.get('error', 'unknown')}. "
                "Tell the user this image could not be analyzed."
            )
    return "\n\n".join(lines)
