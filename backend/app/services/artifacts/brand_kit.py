"""Per-workspace brand kit — multi-tenant theming for generated artifacts.

A brand kit is a small JSON document stored per ``(org_id, app_id)`` in
the existing ``workspace_settings`` key/value store (key ``brand_kit``),
so it inherits the Layer-1 tenant isolation wall with zero schema
changes.  When a kit is present, the export pipeline resolves it to a
``DeckTheme`` and every artifact type (PPTX / DOCX / HTML report /
dashboard) renders in the customer's brand instead of the default
``zhanlu-blue`` palette.

Kit shape (all keys optional — sensible derivations fill the gaps)::

    {
      "name": "acme",
      "colors": {"primary": "#1a73e8", "secondary": "#174ea6",
                  "accent": "#fbbc04", "background": "#ffffff",
                  "surface": "#f8f9fa", "text": "#202124",
                  "text_muted": "#5f6368"},
      "fonts": {"heading": "Arial", "body": "Arial"},
      "logo_blob_uri": "inline://<id>"        # optional, for cover art
    }

Palette extraction from an uploaded logo image is provided by
``extract_palette_from_image`` (PIL color quantization — no extra deps).
"""

from __future__ import annotations

import io
import json
import logging
from typing import Any, Optional

from app.models.workspace_settings import WorkspaceSetting

logger = logging.getLogger(__name__)

BRAND_KIT_KEY = "brand_kit"

# Valid hex color, permissive (#rgb, #rrggbb, with/without '#').
def _norm_hex(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    h = value.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return None
    try:
        int(h, 16)
    except ValueError:
        return None
    return f"#{h.lower()}"


def validate_brand_kit(kit: dict[str, Any]) -> dict[str, Any]:
    """Validate + normalize a brand kit dict. Raises ValueError on bad input."""
    if not isinstance(kit, dict):
        raise ValueError("brand kit must be a JSON object")
    out: dict[str, Any] = {}
    name = kit.get("name")
    if name is not None:
        name = str(name).strip()[:60]
        if name:
            out["name"] = name
    colors_in = kit.get("colors") or {}
    if not isinstance(colors_in, dict):
        raise ValueError("colors must be an object")
    colors: dict[str, str] = {}
    for key in ("primary", "secondary", "accent", "background", "surface",
                "text", "text_muted", "border", "success", "warning", "error", "info"):
        h = _norm_hex(colors_in.get(key))
        if h:
            colors[key] = h
    if not colors:
        raise ValueError("colors must contain at least one valid hex color")
    out["colors"] = colors
    fonts_in = kit.get("fonts") or {}
    if isinstance(fonts_in, dict):
        fonts = {}
        for key in ("heading", "body"):
            v = fonts_in.get(key)
            if isinstance(v, str) and v.strip():
                fonts[key] = v.strip()[:80]
        if fonts:
            out["fonts"] = fonts
    logo = kit.get("logo_blob_uri")
    if isinstance(logo, str) and logo.strip():
        out["logo_blob_uri"] = logo.strip()
    return out


def get_brand_kit(
    db, *, org_id: str = "default-org", app_id: str = "default-app"
) -> Optional[dict[str, Any]]:
    """Return the workspace's brand kit, or None if unset/unparseable."""
    row = (
        db.query(WorkspaceSetting)
        .filter(
            WorkspaceSetting.key == BRAND_KIT_KEY,
            WorkspaceSetting.org_id == org_id,
            WorkspaceSetting.app_id == app_id,
            WorkspaceSetting.is_deleted == False,
        )
        .first()
    )
    if not row or not row.value:
        return None
    try:
        kit = json.loads(row.value)
    except (json.JSONDecodeError, TypeError):
        logger.warning("brand_kit: stored kit for org=%s is not valid JSON", org_id)
        return None
    return kit if isinstance(kit, dict) else None


def set_brand_kit(
    db,
    kit: dict[str, Any],
    *,
    org_id: str = "default-org",
    app_id: str = "default-app",
    created_by_id: Optional[str] = None,
) -> dict[str, Any]:
    """Upsert the workspace's brand kit. Returns the normalized kit."""
    normalized = validate_brand_kit(kit)
    row = (
        db.query(WorkspaceSetting)
        .filter(
            WorkspaceSetting.key == BRAND_KIT_KEY,
            WorkspaceSetting.org_id == org_id,
            WorkspaceSetting.app_id == app_id,
            WorkspaceSetting.is_deleted == False,
        )
        .first()
    )
    if row is None:
        row = WorkspaceSetting(
            key=BRAND_KIT_KEY,
            org_id=org_id,
            app_id=app_id,
            created_by_id=created_by_id,
            value="",
        )
        db.add(row)
    row.value = json.dumps(normalized, ensure_ascii=False)
    db.commit()
    return normalized


def clear_brand_kit(
    db, *, org_id: str = "default-org", app_id: str = "default-app"
) -> bool:
    """Soft-delete the workspace's brand kit. Returns True if one existed."""
    row = (
        db.query(WorkspaceSetting)
        .filter(
            WorkspaceSetting.key == BRAND_KIT_KEY,
            WorkspaceSetting.org_id == org_id,
            WorkspaceSetting.app_id == app_id,
            WorkspaceSetting.is_deleted == False,
        )
        .first()
    )
    if row is None:
        return False
    row.is_deleted = True
    db.commit()
    return True


def extract_palette_from_image(data: bytes, *, max_colors: int = 6) -> dict[str, str]:
    """Extract a brand palette from a logo / reference image.

    Uses PIL adaptive quantization to find dominant colors, then maps the
    most saturated/darkest ones onto the brand-kit slots.  Returns a dict
    suitable for ``kit["colors"]``.  Raises ValueError if the image can't
    be decoded.
    """
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover
        raise ValueError(f"Pillow not available: {exc}")

    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception as exc:
        raise ValueError(f"could not decode image: {exc}")

    # Downscale for speed, then quantize to a small adaptive palette.
    img.thumbnail((128, 128))
    q = img.quantize(colors=max_colors, method=Image.MEDIANCUT).convert("RGB")
    counts = sorted(q.getcolors(maxcolors=128 * 128) or [], reverse=True)
    if not counts:
        raise ValueError("image has no extractable colors")

    def hexof(rgb: tuple[int, int, int]) -> str:
        return "#%02x%02x%02x" % rgb

    def saturation(rgb: tuple[int, int, int]) -> float:
        r, g, b = (c / 255.0 for c in rgb)
        mx, mn = max(r, g, b), min(r, g, b)
        return 0.0 if mx == 0 else (mx - mn) / mx

    def luminance(rgb: tuple[int, int, int]) -> float:
        r, g, b = rgb
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    colors = [rgb for _count, rgb in counts]
    # Primary = most frequent *saturated* color (fallback: most frequent).
    saturated = [c for c in colors if saturation(c) > 0.25]
    primary = (saturated or colors)[0]
    # Secondary = darkest remaining saturated-ish color.
    rest = [c for c in colors if c != primary]
    secondary = min(rest, key=luminance) if rest else primary
    # Text = darkest overall; surface = lightest overall.
    text = min(colors, key=luminance)
    light = max(colors, key=luminance)

    return {
        "primary": hexof(primary),
        "secondary": hexof(secondary),
        "text": hexof(text),
        "background": hexof(light),
        "surface": hexof(light),
    }


def brand_kit_to_theme_tokens(kit: dict[str, Any]) -> dict[str, Any]:
    """Convert a validated brand kit into the flat hex-token dict consumed
    by ``_theme.theme_from_hex_dict`` (and thereby every renderer)."""
    colors = (kit or {}).get("colors") or {}
    fonts = (kit or {}).get("fonts") or {}
    primary = colors.get("primary", "#2563eb")
    secondary = colors.get("secondary") or colors.get("accent") or "#1d4ed8"
    tokens: dict[str, Any] = {
        "name": (kit or {}).get("name") or "brand",
        "primary": primary,
        "primary_dark": secondary,
        "text": colors.get("text", "#0f172a"),
        "muted": colors.get("text_muted", "#64748b"),
        "border": colors.get("border", "#e2e8f0"),
        "slide_bg": colors.get("background", "#ffffff"),
        "surface": colors.get("surface", "#f1f5f9"),
        "band_bg": colors.get("surface", "#f8fafc"),
        "kpi_bg": colors.get("surface", "#f1f5f9"),
        "chart_palette": [
            primary,
            secondary,
            colors.get("accent") or secondary,
        ],
    }
    if colors.get("success"):
        tokens["delta_up"] = colors["success"]
    if colors.get("error"):
        tokens["delta_down"] = colors["error"]
    if fonts:
        tokens["fonts"] = fonts
    return tokens


__all__ = [
    "BRAND_KIT_KEY",
    "get_brand_kit",
    "set_brand_kit",
    "clear_brand_kit",
    "validate_brand_kit",
    "extract_palette_from_image",
    "brand_kit_to_theme_tokens",
]
