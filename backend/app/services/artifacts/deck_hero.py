"""Deck hero art — deterministic theme-aware SVG backgrounds + optional AI images.

Two tiers, both safe, a deck is NEVER blocked or slowed by this module:

1. **SVG hero art** (``DECK_HERO_ART_ENABLED``, default True): a deterministic
   abstract composition (gradient mesh + soft radial glows + geometric
   shapes + grain) generated from the theme's color tokens and a seed string
   (deck title / slide title). Zero external calls — always works, and
   identical for a given (theme, seed) so decks are consistent and
   reproducible. This is the reliable "custom, not template" wow factor.

2. **AI hero images** (``DECK_HERO_AI_IMAGES_ENABLED`` AND an image provider
   configured): calls the configured provider (OpenAI-compatible
   ``{OPENAI_BASE_URL}/images/generations`` or FAL.ai) for ONE cover image
   per deck. On ANY failure (unconfigured, timeout, HTTP error) returns
   ``None`` and the caller falls back to SVG art. Latency-bounded
   (``AI_HERO_TIMEOUT_S``) so a slow provider can never hang a deck.

The HTML renderer consumes the result via ``hero_background_css(theme, seed)``
which returns a ready-to-inline ``background-image`` declaration, or
``hero_background_url`` for the raw data-URI. Slides with an explicit
``hero_image`` URL (from the AI path) use that instead.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import random
import urllib.parse

import httpx

from app.config import settings
from app.services.artifacts.themes import ThemePreset

logger = logging.getLogger(__name__)

AI_HERO_TIMEOUT_S = 25.0
AI_HERO_SIZE = "1792x1024"  # landscape, matches 1920x1080 slide aspect
_HERO_CACHE: dict[str, str] = {}  # seed -> data URI (per-process, small)


# ---------------------------------------------------------------------------
# Deterministic SVG hero art
# ---------------------------------------------------------------------------


def _tokens(theme: ThemePreset) -> dict:
    """Resolve color tokens with sane fallbacks."""
    t = theme.color_tokens if theme else {}
    return {
        "bg_primary": t.get("bg_primary", "#0f172a"),
        "bg_secondary": t.get("bg_secondary", t.get("bg_primary", "#0f172a")),
        "primary": t.get("primary", "#2563EB"),
        "accent": t.get("accent", t.get("finding_accent", "#7C3AED")),
        "text_primary": t.get("text_primary", "#FFFFFF"),
        "muted": t.get("muted", "#64748B"),
    }


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return (15, 23, 42)


def _rgba(h: str, alpha: float) -> str:
    r, g, b = _hex_to_rgb(h)
    return f"rgba({r},{g},{b},{alpha:.2f})"


def _rng(seed: str) -> random.Random:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def build_hero_svg(theme: ThemePreset, seed: str, variant: str = "cover") -> str:
    """Build a 1920x1080 abstract hero SVG from theme tokens + seed.

    Deterministic: the same (theme, seed) always yields the same SVG.
    ``variant`` tweaks the composition: "cover" puts energy bottom-left
    (text sits bottom-left), "divider"/"closing" center the glow.
    """
    tok = _tokens(theme)
    rng = _rng(f"{seed}:{variant}")

    bg1 = tok["bg_primary"]
    bg2 = tok.get("bg_secondary") or bg1
    p1 = tok["primary"]
    p2 = tok["accent"]
    text = tok["text_primary"]

    # Gradient mesh: bg_primary -> bg_secondary with a primary-tinted push.
    grad_id = "g"
    glow1_id, glow2_id, glow3_id = "g1", "g2", "g3"

    # Geometric shapes (deterministic positions/sizes).
    shapes: list[str] = []
    for _ in range(5):
        cx = rng.randint(1400, 2100)
        cy = rng.randint(100, 520)
        r = rng.randint(180, 460)
        alpha = rng.choice([0.05, 0.07, 0.09, 0.12])
        shapes.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r}" '
            f'fill="{_rgba(p2, alpha)}"/>'
        )
    for _ in range(4):
        x = rng.randint(-200, 500)
        y = rng.randint(600, 1100)
        w = rng.randint(500, 1200)
        h = rng.randint(120, 320)
        alpha = rng.choice([0.04, 0.06, 0.08])
        shapes.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" '
            f'rx="160" fill="{_rgba(p1, alpha)}"/>'
        )
    # Fine grid lines (subtle texture).
    grid = ""
    for gx in range(0, 1921, 160):
        grid += f'<line x1="{gx}" y1="0" x2="{gx}" y2="1080" stroke="{_rgba(text, 0.02)}"/>'
    for gy in range(0, 1081, 160):
        grid += f'<line x1="0" y1="{gy}" x2="1920" y2="{gy}" stroke="{_rgba(text, 0.02)}"/>'

    # Variant-specific glow placement.
    if variant == "cover":
        glows = (
            f'<radialGradient id="{glow1_id}" cx="0.18" cy="0.82" r="0.85">'
            f'<stop offset="0%" stop-color="{_rgba(p1, 0.38)}"/>'
            f'<stop offset="100%" stop-color="{_rgba(p1, 0)}"/>'
            f"</radialGradient>"
            f'<radialGradient id="{glow2_id}" cx="0.85" cy="0.15" r="0.7">'
            f'<stop offset="0%" stop-color="{_rgba(p2, 0.30)}"/>'
            f'<stop offset="100%" stop-color="{_rgba(p2, 0)}"/>'
            f"</radialGradient>"
        )
        glow_rects = (
            f'<rect width="1920" height="1080" fill="url(#{glow1_id})"/>'
            f'<rect width="1920" height="1080" fill="url(#{glow2_id})"/>'
        )
    else:  # divider / closing — centered glow
        glows = (
            f'<radialGradient id="{glow1_id}" cx="0.5" cy="0.5" r="0.7">'
            f'<stop offset="0%" stop-color="{_rgba(p1, 0.30)}"/>'
            f'<stop offset="100%" stop-color="{_rgba(p1, 0)}"/>'
            f"</radialGradient>"
            f'<radialGradient id="{glow2_id}" cx="0.5" cy="0.5" r="0.45">'
            f'<stop offset="0%" stop-color="{_rgba(p2, 0.22)}"/>'
            f'<stop offset="100%" stop-color="{_rgba(p2, 0)}"/>'
            f"</radialGradient>"
        )
        glow_rects = (
            f'<rect width="1920" height="1080" fill="url(#{glow1_id})"/>'
            f'<rect width="1920" height="1080" fill="url(#{glow2_id})"/>'
        )

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1920" height="1080" viewBox="0 0 1920 1080">
<defs>
<linearGradient id="{grad_id}" x1="0" y1="0" x2="1" y2="1">
<stop offset="0%" stop-color="{bg1}"/>
<stop offset="100%" stop-color="{bg2}"/>
</linearGradient>
{glows}
</defs>
<rect width="1920" height="1080" fill="url(#{grad_id})"/>
{glow_rects}
{''.join(shapes)}
{grid}
</svg>"""
    return svg


def hero_svg_data_uri(theme: ThemePreset, seed: str, variant: str = "cover") -> str:
    """Return a ``data:image/svg+xml;base64,...`` URI for the hero SVG."""
    cache_key = f"{seed}:{variant}"
    cached = _HERO_CACHE.get(cache_key)
    if cached:
        return cached
    svg = build_hero_svg(theme, seed, variant)
    b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    uri = f"data:image/svg+xml;base64,{b64}"
    _HERO_CACHE[cache_key] = uri
    return uri


def hero_background_css(theme: ThemePreset, seed: str, variant: str = "cover") -> str:
    """Return a CSS ``background-image: url(...);`` declaration for the hero.

    Includes a bottom scrim for cover text readability and a subtle overlay
    so light themes don't wash out. Safe to inline into any slide's ``<style>``.
    """
    uri = hero_svg_data_uri(theme, seed, variant)
    return (
        f"background-image: url('{uri}'), "
        f"linear-gradient(180deg, rgba(0,0,0,0) 55%, rgba(0,0,0,0.35) 100%);"
    )


# ---------------------------------------------------------------------------
# Optional AI hero image (best-effort)
# ---------------------------------------------------------------------------


def ai_hero_available() -> bool:
    """True when the AI image path is enabled AND a provider is configured."""
    return bool(settings.DECK_HERO_AI_IMAGES_ENABLED and settings.image_config_ok())


def _generate_ai_hero_sync(prompt: str) -> str | None:
    """Call the configured image provider synchronously (bounded timeout).

    Returns the image URL on success, ``None`` on any failure. Mirrors
    ``image_generation_tool`` provider logic but with a hard timeout so a
    slow/unreachable provider can never hang a deck render.
    """
    provider = (settings.IMAGE_API_PROVIDER or "openai").lower()
    try:
        if provider == "fal":
            model = settings.IMAGE_MODEL or "fal-ai/flux/schnell"
            payload = {"prompt": prompt, "image_size": AI_HERO_SIZE}
            with httpx.Client(timeout=AI_HERO_TIMEOUT_S) as client:
                resp = client.post(
                    f"https://fal.run/{model}",
                    headers={
                        "Authorization": f"Key {settings.IMAGE_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                return data.get("images", [{}])[0].get("url") or None
        else:
            payload = {
                "model": settings.IMAGE_MODEL or "dall-e-3",
                "prompt": prompt,
                "n": 1,
                "size": AI_HERO_SIZE,
                "quality": "standard",
                "response_format": "url",
            }
            with httpx.Client(timeout=AI_HERO_TIMEOUT_S) as client:
                resp = client.post(
                    f"{settings.OPENAI_BASE_URL}/images/generations",
                    headers={
                        "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                return data["data"][0]["url"]
    except Exception as exc:  # noqa: BLE001 — best-effort by design
        logger.warning("ai_hero generation failed, falling back to SVG: %s", exc)
        return None


def ai_hero_for_deck(title: str, subtitle: str = "", variant: str = "cover") -> str | None:
    """Best-effort AI hero image URL for a deck cover.

    Returns ``None`` when disabled/unconfigured/failed. Only ever called
    when ``ai_hero_available()`` is True (config gate + provider gate), so
    the deterministic SVG path is the default for every deployment.
    """
    if not ai_hero_available():
        return None
    prompt = (
        f"Professional executive presentation hero image, wide landscape, "
        f"abstract premium background, {variant} visual, deep color palette, "
        f"subtle geometric shapes, soft gradients, no text. "
        f"Deck topic: {title}. {subtitle}".strip()
    )
    return _generate_ai_hero_sync(prompt)


__all__ = [
    "build_hero_svg",
    "hero_svg_data_uri",
    "hero_background_css",
    "ai_hero_available",
    "ai_hero_for_deck",
]
