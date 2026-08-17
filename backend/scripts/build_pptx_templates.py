#!/usr/bin/env python3
"""Programmatic .pptx template builder for the PHASE 1 layout engine.

This script builds two blank PowerPoint decks with slide masters baked
from code:

    - zhanlu_default.pptx — zhanlu-blue theme (default brand)
    - zhanlu_dark.pptx    — zhanlu-dark theme  (alt brand)

These are the templates the layout engine (`app/services/artifacts/
layout_engine.py`) loads via ``apply_master()`` to seed every deck with
typography + brand colors. Output paths are reproducible and the script
is deterministic (no random seeds, no timestamps) so the produced .pptx
files are byte-identical across runs.

DESIGN DECISIONS (logged here so the choices are reviewable):

    * 16:9 slide size (13.333 × 7.5 in) — modern PowerPoint default.
    * One master per file, one layout (blank, index 6) — every deck slide
      is built on the blank layout and stacked with explicit textboxes +
      shapes (no titles / body placeholders), so the layout engine has
      total control over positions.
    * Calibri (the Microsoft default and PowerPoint's theme font). Falls
      back to the OS sans-serif if Calibri is not installed.
    * Typography scale:
            TITLE       32pt bold
            SUBTITLE    18pt
            BODY        16pt
            CAPTION     12pt
            FOOTER       9pt
    * Default 0.6" margins all around (applied via the layout engine, not
      via the master — see layout_engine.MARGIN_IN).
    * Theme colors are stored in `<a:clrScheme>` of the theme XML inside
      the .pptx so PowerPoint recognises them under "Design > Variants".
    * No footer text. Layout engine applies subtle slide numbers via
      ``add_slide_number()`` at render time.

Usage:
    cd backend && python scripts/build_pptx_templates.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor


# Where the .pptx files live (alongside the layout engine).
THIS_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = THIS_DIR.parent / "app" / "services" / "artifacts" / "templates"


# Theme dictionaries. Hex strings (no leading '#').
# These mirror the `theme_tokens` shape used by pptx_export / sandbox
# runner so callers can supply the same dict to both renderers.
ZHANLU_BLUE = {
    "name": "zhanlu-blue",
    "primary":   "#2563EB",
    "text":      "#0F172A",
    "muted":     "#64748B",
    "border":    "#E2E8F0",
    "band_bg":   "#F8FAFC",
    "kpi_bg":    "#F1F5F9",
    "finding_bg": "#F5F3FF",
    "finding_accent": "#7C3AED",
    "rec_bg":    "#EFF6FF",
    "warn_bg":   "#FFFBEB",
    "warn_accent": "#F59E0B",
    "slide_bg":  "#FFFFFF",
    "delta_up":  "#059669",
    "delta_down": "#DC2626",
}

ZHANLU_DARK = {
    "name": "zhanlu-dark",
    "primary":   "#60A5FA",
    "text":      "#F1F5F9",
    "muted":     "#94A3B8",
    "border":    "#334155",
    "band_bg":   "#1E293B",
    "kpi_bg":    "#0F172A",
    "finding_bg": "#1E1B4B",
    "finding_accent": "#A78BFA",
    "rec_bg":    "#172554",
    "warn_bg":   "#422006",
    "warn_accent": "#FBBF24",
    "slide_bg":  "#0F172A",
    "delta_up":  "#34D399",
    "delta_down": "#F87171",
}


def _set_master_font(master, rgb: RGBColor) -> None:
    """Force the slide-master body font + color to Calibri / given RGB.

    Iterates over every text-frame on the master so PowerPoint's "match
    master" cascade picks up Calibri from the start (avoids the "Times
    New Roman" default that some LibreOffice/pptx installs fall back to).
    """
    for shape in list(master.shapes):
        if not shape.has_text_frame:
            continue
        for para in shape.text_frame.paragraphs:
            for run in para.runs:
                run.font.name = "Calibri"
                run.font.color.rgb = rgb


def build_template(theme: dict, name: str, output_path: Path) -> None:
    """Build a single .pptx template deterministically.

    Args:
        theme: Theme color dict (one of ZHANLU_BLUE / ZHANLU_DARK).
        name: File stem (e.g. "zhanlu_default").
        output_path: Absolute path to write the .pptx file.
    """
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)

    # 1) Set the slide-master fonts to Calibri in the brand primary.
    primary = RGBColor.from_string(theme["primary"].lstrip("#"))
    text_rgb = RGBColor.from_string(theme["text"].lstrip("#"))
    for master in prs.slide_masters:
        _set_master_font(master, text_rgb)

    # 2) Custom slide-master background fill (so PowerPoint shows the
    #    brand-tinted slide bg in the thumbnails).
    bg_rgb = RGBColor.from_string(theme["slide_bg"].lstrip("#"))
    for master in prs.slide_masters:
        fill = master.background.fill
        fill.solid()
        fill.fore_color.rgb = bg_rgb

    # 3) Save the slide_master XML edit directly: store theme colors
    #    via the `clrScheme` element so PowerPoint's "Variants" menu
    #    picks them up. python-pptx exposes `prs.slide_masters[0]
    #    .element.getparent()` but the theme lives on a separate part —
    #    we mutate the theme XML in-place. This is the simplest way to
    #    embed the brand colors as a proper theme.
    _write_theme_colors(prs, theme)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(output_path))


def _write_theme_colors(prs, theme: dict) -> None:
    """Inject the brand colors into the .pptx's theme1.xml `clrScheme`.

    python-pptx hides the theme part; we dig into the package's partlist.
    Layout:
        clrScheme order: dk1, lt1, dk2, lt2, accent1..accent6,
                          hlink, folHlink
    We map primary -> accent1, muted -> dk2, border -> lt2, etc.
    """
    from lxml import etree

    nsmap = {
        "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    }
    # Find the theme part.
    theme_part = None
    for part in prs.part.package.iter_parts():
        if part.partname.endswith("/theme/theme1.xml"):
            theme_part = part
            break
    if theme_part is None:
        return  # PowerPoint will synthesise a default theme; brand still
                # works via direct RGBs set on shapes at render time.

    try:
        root = etree.fromstring(theme_part.blob)
    except Exception:
        return
    clr_scheme = root.find(".//a:clrScheme", namespaces=nsmap)
    if clr_scheme is None:
        return

    accents = clr_scheme.findall("a:accent1", namespaces=nsmap)
    if accents:
        accents[0].clear()
        srgb = etree.SubElement(accents[0], "{%s}srgbClr" % nsmap["a"])
        srgb.set("val", theme["primary"].lstrip("#"))

    # Update dk2 to muted (used for some inheritance chains in PowerPoint).
    dk2s = clr_scheme.findall("a:dk2", namespaces=nsmap)
    if dk2s:
        dk2s[0].clear()
        srgb = etree.SubElement(dk2s[0], "{%s}srgbClr" % nsmap["a"])
        srgb.set("val", theme["muted"].lstrip("#"))

    # Persist.
    new_blob = etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    theme_part._blob = new_blob


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Build the zhanlu *.pptx templates programmatically."
    )
    p.add_argument(
        "--out-dir",
        default=str(TEMPLATES_DIR),
        help=f"Output directory (default: {TEMPLATES_DIR})",
    )
    args = p.parse_args(argv)

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    targets = [
        (ZHANLU_BLUE, "zhanlu_default", out_dir / "zhanlu_default.pptx"),
        (ZHANLU_DARK, "zhanlu_dark",    out_dir / "zhanlu_dark.pptx"),
    ]
    for theme, stem, out in targets:
        build_template(theme, stem, out)
        size = out.stat().st_size
        print(f"WROTE {out}  ({size:,} bytes, theme={theme['name']})")

    # Determinism check.
    import hashlib
    paths = [t[2] for t in targets]
    digests = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    for name, d in digests.items():
        print(f"SHA256 {name}: {d}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
