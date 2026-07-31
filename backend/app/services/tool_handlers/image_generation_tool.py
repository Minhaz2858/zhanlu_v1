"""image_generation tool — AI image creation via configurable provider.

Supports OpenAI DALL-E (reuses existing OPENAI_API_KEY) or FAL.ai.
Returns the image URL saved to the uploads directory.
"""

import logging
import os

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


async def _image_generation(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    prompt = args.get("prompt", "").strip()
    size = args.get("size", "1024x1024")
    quality = args.get("quality", "standard")

    if not prompt:
        return {"success": False, "error": "prompt is required"}

    if not settings.image_config_ok():
        return {
            "success": False,
            "error": "Image generation is not configured. Set OPENAI_API_KEY or IMAGE_API_KEY in .env.",
        }

    provider = settings.IMAGE_API_PROVIDER.lower()

    try:
        if provider == "fal":
            return await _generate_fal(prompt, size, quality)
        else:
            return await _generate_openai(prompt, size, quality)
    except Exception as e:
        logger.warning("image_generation failed: %s", e)
        return {"success": False, "error": str(e)}


async def _generate_openai(prompt: str, size: str, quality: str) -> dict:
    """Generate image using OpenAI DALL-E API."""
    payload = {
        "model": settings.IMAGE_MODEL,
        "prompt": prompt,
        "n": 1,
        "size": size,
        "quality": quality,
        "response_format": "url",
    }

    url = f"{settings.OPENAI_BASE_URL}/images/generations"
    headers = {
        "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()

    image_url = data["data"][0]["url"]
    return {
        "success": True,
        "image_url": image_url,
        "prompt": prompt,
        "model": settings.IMAGE_MODEL,
        "size": size,
    }


async def _generate_fal(prompt: str, size: str, quality: str) -> dict:
    """Generate image using FAL.ai API."""
    model = settings.IMAGE_MODEL or "fal-ai/flux/schnell"
    payload = {
        "prompt": prompt,
        "image_size": size,
    }

    url = f"https://fal.run/{model}"
    headers = {
        "Authorization": f"Key {settings.IMAGE_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()

    image_url = data.get("images", [{}])[0].get("url", "")
    if not image_url:
        return {"success": False, "error": "No image URL in response"}

    return {
        "success": True,
        "image_url": image_url,
        "prompt": prompt,
        "model": model,
        "size": size,
    }


# ---------------------------------------------------------------------------
# Schema & Registration
# ---------------------------------------------------------------------------

IMAGE_GENERATION_SCHEMA = {
    "type": "function",
    "function": {
        "name": "image_generation",
        "description": (
            "Generate an image from a text description using AI. "
            "Returns the image URL. Use descriptive prompts for best results."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "A detailed description of the image to generate",
                },
                "size": {
                    "type": "string",
                    "enum": ["256x256", "512x512", "1024x1024", "1792x1024", "1024x1792"],
                    "description": "Image dimensions (default 1024x1024)",
                    "default": "1024x1024",
                },
                "quality": {
                    "type": "string",
                    "enum": ["standard", "hd"],
                    "description": "Image quality (default standard, hd costs more)",
                    "default": "standard",
                },
            },
            "required": ["prompt"],
        },
    },
}

registry.register(
    name="image_generation",
    schema=IMAGE_GENERATION_SCHEMA,
    handler=_image_generation,
    category="media",
    enabled_by_default=True,
    requires_config=["OPENAI_API_KEY"],  # For OpenAI provider
    description="Generate an image from a text prompt using AI.",
)
