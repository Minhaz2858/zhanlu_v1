#!/usr/bin/env python3
"""PHASE 1 — Visual diff acceptance harness.

Renders the same realistic ReportCardPayload through:

  * the OLD hard-coded ``sandbox_runner.generate_pptx`` (vendored in
    ``scripts/baseline_pptx.py``),
  * the NEW DeckPlan-driven ``layout_engine.render``,

then runs LibreOffice headless -> PyMuPDF to emit per-slide PNGs for
both decks, and composites them side-by-side into a single image.

The composite lives at::

    docs/superpowers/specs/2026-08-17-phase1-visual-diff/<payload_id>/side_by_side.png

with sub-folders ``before/`` and ``after/`` containing the per-slide
PNGs from each renderer.

RUN INSIDE THE SANDBOX CONTAINER (``zhanlu-sandbox-pptx:latest``) — that
container has LibreOffice + PyMuPDF + matplotlib + python-pptx pre-installed.
On the host you can run the script for a smoke check; the libreoffice
step will fail.

USAGE::

    docker run --rm \
      -v /home/ysk2025/zhanlu_7_30:/work \
      -w /work/backend \
      zhanlu-sandbox-pptx:latest \
      python scripts/render_visual_diff.py \
        --payload-id sample_q3_q4_sales_overview \
        --out-base /work/docs/superpowers/specs/2026-08-17-phase1-visual-diff

For a quick host smoke (no libreoffice):

    docker run --rm -v $(pwd):/work -w /work/backend \
      zhanlu-sandbox-pptx:latest \
      python scripts/render_visual_diff.py --skip-png-render
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image


THIS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = THIS_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))


# ----------------------------------------------------------------------------
# Fixture payload — exercises all 12 layout types
# ----------------------------------------------------------------------------


def build_fixture_payload():
    """Return a ``ReportCardPayload``-shaped dict."""
    return {
        "title": "Q3 → Q4 Sales Overview: Resin Pricing Drives a 14% Margin Lift",
        "source": "sales_orders, 2026-08-15 snapshot",
        "generated_at": "2026-08-15T10:00:00Z",
        "summary": (
            "Resin product revenue grew 18% quarter-over-quarter while unit "
            "shipment volumes stayed flat, driven primarily by a 12% price "
            "lift in LDPE grades. PC grades underperformed against plan by "
            "9% on softening export demand, partially offset by stronger "
            "PVC pricing in the domestic market."
        ),
        "methodology": (
            "Aggregated from sales_orders (fact table joined to "
            "dim_product). Filters: FY26 Q3+Q4, sales org = Z01, currency = "
            "RMB. Top-8 by shipment_volume; rebalanced for grade-level "
            "margin to surface the resin / PC divergence."
        ),
        "kpis": [
            {"label": "Revenue", "value": "¥1.84B", "delta": "+18%",  "caption": "QoQ"},
            {"label": "Volume",   "value": "212 kt", "delta": "-1.2%", "caption": "QoQ"},
            {"label": "Margin",   "value": "21.4%",  "delta": "+2.8pt", "caption": "QoQ"},
            {"label": "Active SKUs", "value": "126", "delta": "+5",   "caption": "vs. plan"},
            {"label": "Backlog",  "value": "¥312M", "delta": "+7%",   "caption": "vs. last quarter"},
            {"label": "OTIF",     "value": "94.1%",  "delta": "+1.3pt", "caption": "Plan target 95%"},
        ],
        "insights": [
            "Resin grades (LDPE + LLDPE) drove 71% of margin expansion, even though they represent only 38% of volume.",
            "PVC domestic pricing benefited from tighter spot-market supply in July; sustaining it is the upside risk.",
            "PC export softness tracks softening EU appliance demand — not likely to reverse before CY27.",
            "Top-customer concentration rose to 41% (vs. 38% prior quarter); worth a diversification play.",
            "Freight cost per ton fell 6% on renegotiated rail contracts; an extra ¥22M of cushion.",
        ],
        "key_findings": [
            {"text": "Resin pricing added ¥112M of gross margin vs. flat volumes, masking weaker PC grades."},
            {"text": "PC grade export volume fell 14% QoQ; softer EU demand is the proximate cause."},
            {"text": "Top-3 SKUs (LDPE Film, LLDPE Rotation, PVC Pipe) account for 28% of revenue; concentration is up."},
            {"text": "Freight cost re-negotiation adds an estimated ¥22M of FY26 cushion."},
            {"text": "OTIF misses plan by 0.9pt; root cause is concentrated at 2 plants (Shanghai, Tianjin)."},
            {"text": "Backlog at ¥312M (+7% QoQ) gives some forward-cover against the PC softness."},
        ],
        "recommendations": [
            "Re-weight Oct–Dec plan toward resin grades to capture pricing upside; pause PC export push.",
            "Diversify top-customer exposure — start renewal talks with #6–#10 accounts now.",
            "Rebid the Shanghai + Tianjin OTIF root-cause (joint with S&OP) by end of September.",
            "Lock in freight cost savings as a recurring line in the FY27 budget.",
        ],
        "sections": [
            {"title": "Revenue & Margin", "content": "Resin lift is the headline.", "bullets": []},
            {"title": "Volume Picture",   "content": "Volumes are flat, but skewed.", "bullets": []},
            {"title": "Customer Concentration", "content": "Top-customer share up.", "bullets": []},
            {"title": "Forward Look",     "content": "Backlog supports next quarter.", "bullets": []},
        ],
        "next_step": (
            "Endorse the resin-tilted Oct–Dec plan; bring OTIF root-cause "
            "to S&OP by end-September; sign off on the freight re-negotiation."
        ),
        "warnings": [
            "Spot resin prices may roll over in Q4 if inventory rebuilds faster than demand.",
        ],
        "chart": {
            "type": "bar",
            "title": "Revenue by Product Grade (¥M)",
            "x_key": "grade",
            "y_keys": ["revenue"],
            "data": [
                {"grade": "LDPE Film",   "revenue": 412},
                {"grade": "LLDPE Rot.",  "revenue": 308},
                {"grade": "PVC Pipe",    "revenue": 254},
                {"grade": "PC Clear",    "revenue": 198},
                {"grade": "PPH Inj.",    "revenue": 162},
                {"grade": "HDPE Blow",   "revenue": 142},
                {"grade": "ABS Inj.",    "revenue": 121},
                {"grade": "EPS Block",   "revenue":  96},
            ],
        },
        "sql": (
            "SELECT grade, SUM(revenue_rmb)/1e6 AS revenue\n"
            "FROM sales_orders\n"
            "WHERE fyear IN ('2026-Q3','2026-Q4')\n"
            "GROUP BY grade ORDER BY revenue DESC LIMIT 8;"
        ),
    }


def build_fixture_rows(n: int = 24):
    """24 realistic grade × quarter data rows for the data table."""
    grades = [
        ("LDPE Film",      24,  412,  86),
        ("LLDPE Rot.",     20,  308,  72),
        ("PVC Pipe",       16,  254,  55),
        ("PC Clear",       18,  198,  48),
        ("PPH Inj.",       22,  162,  41),
        ("HDPE Blow",      28,  142,  36),
        ("ABS Inj.",       12,  121,  29),
        ("EPS Block",       8,   96,  22),
        ("PMMA Opt.",       6,   72,  18),
        ("PBT GF30",        5,   64,  17),
        ("PA6 Inj.",        7,   58,  15),
        ("POM Copo.",       4,   49,  13),
        ("EVA Foam",        9,   41,  11),
        ("SAN Inj.",        3,   38,  10),
        ("ASA HI",          3,   33,   9),
        ("TPU Extr.",       5,   29,   8),
        ("PVDF Coat.",      2,   24,   7),
        ("PPS GF40",        2,   21,   6),
        ("PEEK Nat.",       1,   18,   5),
        ("ETFE Film",       2,   15,   4),
        ("FEP Wire",        2,   13,   4),
        ("PFA Tube",        1,   11,   3),
        ("PCTFE Sheet",     1,    8,   2),
        ("ECTFE Lining",    1,    6,   2),
    ]
    return [
        {"grade": g, "volume_kt": v, "revenue_m_rmb": r, "margin_m_rmb": m}
        for g, v, r, m in grades[:n]
    ]


# ----------------------------------------------------------------------------
# NEW pipeline — DeckPlan exercising every layout
# ----------------------------------------------------------------------------


def build_fixture_deck_plan(payload):
    """Convert the fixture payload into a DeckPlan dict that exercises
    every layout type in ``layout_engine``."""
    return {
        "title": payload["title"],
        "deck_type": "data_report",
        "theme_recommendation": "zhanlu-blue",
        "summary": payload["summary"],
        "methodology": payload["methodology"],
        "slides": [
            # 1. Cover
            {
                "layout": "cover",
                "title": payload["title"],
                "subtitle": f"Source: {payload['source']}",
            },
            # 2. Agenda
            {
                "layout": "agenda",
                "title": "Agenda",
                "bullets": [s["title"] for s in payload["sections"]],
            },
            # 3. KPI grid
            {
                "layout": "kpi_grid",
                "title": "Headline numbers moved as we expected",
                "kpi_specs": payload["kpis"],
            },
            # 4. Chart full
            {
                "layout": "chart_full",
                "title": "Resin grades captured 70%+ of the revenue",
                "chart_spec": {
                    "chart_type": payload["chart"]["type"],
                    "x_key": payload["chart"]["x_key"],
                    "y_keys": payload["chart"]["y_keys"],
                    "title": payload["chart"]["title"],
                },
                "chart_rows": payload["chart"]["data"],
            },
            # 5. Section divider (mid-deck)
            {
                "layout": "section_divider",
                "title": "Behind the numbers",
                "subtitle": "Findings, insights, and the customer picture.",
            },
            # 6. Findings cards
            {
                "layout": "findings_cards",
                "title": "Resin pricing masked weaker PC grades",
                "bullets": [f["text"] for f in payload["key_findings"]],
            },
            # 7. Chart + bullets (use the revenue-by-grade chart on the
            #    left, takeaways on the right)
            {
                "layout": "chart_with_bullets",
                "title": "What the chart actually says",
                "chart_spec": {
                    "chart_type": payload["chart"]["type"],
                    "x_key": payload["chart"]["x_key"],
                    "y_keys": payload["chart"]["y_keys"],
                },
                "chart_rows": payload["chart"]["data"],
                "bullets": [
                    "Resin grades = 70%+ of revenue",
                    "PC grades are the laggard",
                    "Top-3 SKUs drive 28% of total",
                ],
            },
            # 8. Insights bullets
            {
                "layout": "insights_bullets",
                "title": "Five things worth your attention",
                "bullets": payload["insights"],
            },
            # 9. Recommendations
            {
                "layout": "recommendations",
                "title": "Decisions we want from S&OP this week",
                "bullets": payload["recommendations"],
            },
            # 10. Data table (top-8 with top-3 highlight baked in)
            {
                "layout": "data_table",
                "title": "Top-8 grades by revenue",
                "table_cols": ["grade", "volume_kt", "revenue_m_rmb", "margin_m_rmb"],
                "table_rows": [
                    {
                        "grade": r["grade"], "volume_kt": r["volume_kt"],
                        "revenue_m_rmb": r["revenue_m_rmb"],
                        "margin_m_rmb": r["margin_m_rmb"],
                    }
                    for r in build_fixture_rows(8)
                ],
            },
            # 11. Methodology
            {
                "layout": "methodology",
                "title": "Methodology",
                "notes": payload["methodology"],
            },
            # 12. Closing
            {
                "layout": "closing",
                "title": "Next Step",
                "subtitle": payload["next_step"],
                "notes": payload["next_step"],
            },
        ],
    }


# ----------------------------------------------------------------------------
# Render: OLD vs NEW
# ----------------------------------------------------------------------------


def render_old(payload, rows, theme_tokens):
    """Render with the VENDORED OLD generator (baseline_pptx)."""
    from scripts.baseline_pptx import render_baseline_pptx
    return render_baseline_pptx(
        payload=payload, rows=rows, theme_tokens=theme_tokens, style_recipe="sharp",
    )


def render_new(plan_dict, rows, theme_tokens):
    """Render with the NEW layout engine."""
    from app.services.artifacts.layout_engine import render
    return render(plan=plan_dict, rows=rows, ctx={"theme_tokens": theme_tokens})


# ----------------------------------------------------------------------------
# LibreOffice headless -> per-slide PNGs (PyMuPDF)
# ----------------------------------------------------------------------------


def render_pngs(pptx_bytes: bytes, out_dir: Path, dpi: int = 150) -> list[Path]:
    """Convert .pptx bytes -> PDF via libreoffice -> per-slide PNGs via
    PyMuPDF. Returns the ordered list of PNG paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        pptx_path = td_path / "deck.pptx"
        pptx_path.write_bytes(pptx_bytes)
        # Step 1: pptx -> pdf
        result = subprocess.run(
            [
                "libreoffice", "--headless",
                "--convert-to", "pdf",
                "--outdir", str(td_path),
                str(pptx_path),
            ],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"libreoffice failed (rc={result.returncode}): {result.stderr}\n"
                f"stdout: {result.stdout}"
            )
        pdf_path = td_path / "deck.pdf"
        if not pdf_path.exists():
            raise RuntimeError(f"libreoffice did not produce PDF; found: {list(td_path.iterdir())}")
        # Step 2: pdf -> per-page png via PyMuPDF
        try:
            import fitz
        except ImportError as e:
            raise RuntimeError("PyMuPDF (fitz) is not installed in this container") from e
        doc = fitz.open(str(pdf_path))
        png_paths: list[Path] = []
        for i, page in enumerate(doc, start=1):
            pix = page.get_pixmap(dpi=dpi)
            png_path = out_dir / f"slide-{i:02d}.png"
            png_path.write_bytes(pix.tobytes("png"))
            png_paths.append(png_path)
        return png_paths


# ----------------------------------------------------------------------------
# Composite side-by-side
# ----------------------------------------------------------------------------


def composite_side_by_side(before_pngs: list[Path], after_pngs: list[Path], out_path: Path) -> None:
    """Compose before/after PNGs into a single side-by-side image.

    Each side panel is normalised to the same width; rows are stacked
    vertically (one row per slide index), with the OLD on the left and
    the NEW on the right. Title labels (BEFORE / AFTER) are painted at
    the top.
    """
    target_w = 1200
    panels = []
    for pngs, label in [(before_pngs, "BEFORE  (old hard-coded generate_pptx)"),
                        (after_pngs,  "AFTER   (new layout_engine)")]:
        side_imgs = []
        for p in pngs:
            img = Image.open(p).convert("RGB")
            w, h = img.size
            scale = target_w / w
            new = img.resize((target_w, int(h * scale)), Image.LANCZOS)
            side_imgs.append(new)
        panels.append((label, side_imgs))

    max_rows = max(len(p[1]) for p in panels)
    padded = []
    for label, imgs in panels:
        if len(imgs) < max_rows:
            blank = Image.new("RGB", (target_w, imgs[0].height), (245, 245, 245))
            imgs = imgs + [blank] * (max_rows - len(imgs))
        padded.append((label, imgs))

    # Header band (60px)
    title_h = 60
    slide_label_h = 28
    total_w = target_w * 2 + 20
    total_h = title_h + sum(
        im.height + slide_label_h for _, imgs in padded for im in imgs
    )
    canvas = Image.new("RGB", (total_w, total_h), (255, 255, 255))

    # Title bar
    from PIL import ImageDraw, ImageFont
    draw = ImageDraw.Draw(canvas)
    try:
        font_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 24)
        font_label = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
        font_slide = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 12)
    except OSError:
        font_title = font_label = font_slide = ImageFont.load_default()

    draw.rectangle((0, 0, total_w, title_h), fill=(15, 23, 42))
    draw.text((20, 14), "PHASE 1 — Visual Diff Acceptance", fill=(255, 255, 255), font=font_title)
    draw.text((20, 40), "Same ReportCardPayload, rendered BEFORE and AFTER the layout-engine refactor",
              fill=(148, 163, 184), font=font_label)

    y = title_h
    for side_idx, (label, imgs) in enumerate(padded):
        x0 = side_idx * (target_w + 20)
        draw.rectangle((x0, y, x0 + target_w, y + 24), fill=(241, 245, 249))
        draw.text((x0 + 12, y + 4), label, fill=(15, 23, 42), font=font_label)
        y += 24
        for slide_idx, im in enumerate(imgs, start=1):
            canvas.paste(im, (x0, y))
            draw.rectangle((x0, y, x0 + target_w, y + slide_label_h), fill=(15, 23, 42))
            draw.text((x0 + 8, y + 6), f"slide {slide_idx}", fill=(255, 255, 255), font=font_slide)
            y += slide_label_h + im.height

    canvas.save(out_path, "PNG", optimize=True)
    print(f"WROTE side_by_side.png  ({out_path.stat().st_size:,} bytes)")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="PHASE 1 visual diff.")
    p.add_argument("--payload-id", default="sample_q3_q4_sales_overview")
    p.add_argument(
        "--out-base",
        default=str(BACKEND_DIR.parent / "docs" / "superpowers" / "specs" /
                    "2026-08-17-phase1-visual-diff"),
    )
    p.add_argument("--theme", default="zhanlu-blue",
                   help="Theme name (zhanlu-blue | zhanlu-dark)")
    p.add_argument("--dpi", type=int, default=110)
    p.add_argument("--skip-png-render", action="store_true",
                   help="Skip libreoffice/PyMuPDF PNG render (host smoke)")
    args = p.parse_args(argv)

    out_base = Path(args.out_base).resolve()
    out_dir = out_base / args.payload_id
    out_dir.mkdir(parents=True, exist_ok=True)
    before_dir = out_dir / "before"
    after_dir = out_dir / "after"

    # Theme tokens (matched to scripts/build_pptx_templates.py).
    if args.theme == "zhanlu-dark":
        theme_tokens = {
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
    else:
        theme_tokens = {
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

    payload = build_fixture_payload()
    rows = build_fixture_rows(8)

    # Write the payload + plan to disk so this script's output is self-
    # contained and reviewable.
    (out_dir / "payload.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False)
    )
    plan_dict = build_fixture_deck_plan(payload)
    (out_dir / "deck_plan.json").write_text(
        json.dumps(plan_dict, indent=2, ensure_ascii=False)
    )

    # Render OLD.
    print("[OLD] rendering with baseline_pptx…")
    t0 = time.monotonic()
    old_bytes = render_old(payload, rows, theme_tokens)
    print(f"[OLD] {len(old_bytes):,} bytes in {time.monotonic()-t0:.2f}s")
    (out_dir / "before.pptx").write_bytes(old_bytes)

    # Render NEW.
    print("[NEW] rendering with layout_engine…")
    t0 = time.monotonic()
    new_bytes = render_new(plan_dict, rows, theme_tokens)
    print(f"[NEW] {len(new_bytes):,} bytes in {time.monotonic()-t0:.2f}s")
    (out_dir / "after.pptx").write_bytes(new_bytes)

    # PNG render (needs libreoffice inside the sandbox container).
    if args.skip_png_render:
        print("--skip-png-render set; skipping PDF/PNG render")
        print(f"  before.pptx → {out_dir/'before.pptx'}")
        print(f"  after.pptx  → {out_dir/'after.pptx'}")
        print(f"  payload.json → {out_dir/'payload.json'}")
        print(f"  deck_plan.json → {out_dir/'deck_plan.json'}")
        return 0

    if not shutil.which("libreoffice"):
        print("ERROR: libreoffice not on PATH; run inside the sandbox container.")
        return 2

    print("[PNG] rendering OLD deck → PNGs…")
    before_pngs = render_pngs(old_bytes, before_dir, dpi=args.dpi)
    print(f"[PNG] BEFORE: {len(before_pngs)} slides")
    print("[PNG] rendering NEW deck → PNGs…")
    after_pngs = render_pngs(new_bytes, after_dir, dpi=args.dpi)
    print(f"[PNG] AFTER:  {len(after_pngs)} slides")

    print("[COMPOSITE] building side_by_side.png…")
    composite_side_by_side(before_pngs, after_pngs, out_dir / "side_by_side.png")
    print(f"DONE: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
