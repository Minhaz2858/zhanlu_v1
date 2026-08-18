"""Regenerate the PPTX preview gallery.

Renders the canonical "chatpath" sample deck in every vendored theme so a
human can visually compare palettes, then (best-effort) converts each deck to
PDF + a PNG thumbnail via LibreOffice + PyMuPDF.  Always writes an ``INDEX.md``
and runs the 13-check deck audit per theme, failing loudly (non-zero exit) if
any theme produces a FAIL-level audit.

Themes are discovered from ``data/themes/*.json`` (see
``app.services.artifacts.exporters._theme.list_themes``).  Output lands in
``data/generated/preview_gallery/<theme>/``.

A second pass renders one deck *per deck profile* (Phase 4) using the same
sample data, so the structural differences between ``data_report`` /
``executive_brief`` / ``pitch_narrative`` / ``periodic_review`` are visually
comparable.  Those land in ``data/generated/preview_gallery/profiles/<profile>/``.

Run:
    docker exec zhanlu-backend bash -c "cd /app && python scripts/regenerate_preview_gallery.py"
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# Make the backend importable when run as a bare script inside the container.
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.services.artifacts.exporters._theme import list_themes  # noqa: E402
from app.services.artifacts.exporters._common import ExportContext  # noqa: E402
from app.services.artifacts.render_dispatcher import (  # noqa: E402
    render_pptx_from_plan_sync,
)
from app.services.artifacts.audits.audit_deck import audit  # noqa: E402
from app.services.artifacts.deck_profiles import ALL_PROFILES  # noqa: E402
from app.services.artifacts.deck_planner import _fallback_plan  # noqa: E402
from app.services.artifacts.data_profiler import profile_rows  # noqa: E402
from app.services.synexia.contracts import (  # noqa: E402
    ChartSpecInSlide,
    DeckPlan,
    KPISpecInSlide,
    SlidePlan,
)

_GALLERY_DIR = _BACKEND_ROOT / "data" / "generated" / "preview_gallery"
_SOFFICE = "soffice"


# --- Sample "chatpath" dataset -------------------------------------------------
def _sample_rows() -> list[dict]:
    return [
        {"material": "LLDPE", "price": 9120, "delta_pct": -4.2, "volume": 1820},
        {"material": "HDPE", "price": 8760, "delta_pct": 1.8, "volume": 1450},
        {"material": "PP", "price": 8050, "delta_pct": -1.1, "volume": 2110},
        {"material": "PVC", "price": 6320, "delta_pct": 3.4, "volume": 980},
        {"material": "ABS", "price": 12450, "delta_pct": 0.6, "volume": 760},
        {"material": "PET", "price": 7180, "delta_pct": -2.7, "volume": 1330},
        {"material": "PS", "price": 9450, "delta_pct": 2.2, "volume": 540},
        {"material": "PC", "price": 15800, "delta_pct": -0.9, "volume": 410},
    ]


def _chatpath_plan() -> DeckPlan:
    """A representative deck that exercises cover / KPI / chart / table / closing."""
    rows = _sample_rows()
    return DeckPlan(
        title="Polymer Price Chatpath — Weekly Review",
        theme_recommendation="zhanlu-blue",
        headline_style="assertion",
        summary="Resin prices softened across most grades this week.",
        slides=[
            SlidePlan(
                layout="cover",
                title="Polymer Price Chatpath",
                subtitle="Weekly resin spot review",
                bullets=[],
            ),
            SlidePlan(
                layout="kpi_grid",
                title="This Week at a Glance",
                bullets=[],
                kpi_specs=[
                    KPISpecInSlide(label="Avg price", value="9,390", caption="8 grades"),
                    KPISpecInSlide(label="Top mover", value="-4.2%", caption="LLDPE w/w"),
                    KPISpecInSlide(label="Total volume", value="10,400 t", caption="tracked"),
                    KPISpecInSlide(label="Grades up", value="3 / 8", caption="vs 5 down"),
                ],
            ),
            SlidePlan(
                layout="chart_full",
                title="Spot Price by Grade",
                bullets=["LLDPE and PET led the declines",
                         "ABS held firm at the top of the range"],
                chart_spec=ChartSpecInSlide(
                    chart_type="bar",
                    title="Spot price (¥/t) by grade",
                    x_key="material",
                    y_keys=["price"],
                ),
                chart_rows=rows,
            ),
            SlidePlan(
                layout="data_table",
                title="Full Price Table",
                bullets=[],
                table_cols=["Grade", "Price", "Δ%", "Volume"],
                table_rows=[
                    {c: v for c, v in zip(
                        ["Grade", "Price", "Δ%", "Volume"],
                        [r["material"], r["price"], f"{r['delta_pct']:+.1f}%", r["volume"]],
                    )}
                    for r in rows
                ],
            ),
            SlidePlan(
                layout="closing",
                title="Takeaways",
                bullets=[
                    "Broad softening — buyers retain pricing leverage",
                    "Watch LLDPE supply into next cycle",
                    "Volume steady; no demand shock signaled",
                ],
            ),
        ],
    )


# --- Rendering helpers ---------------------------------------------------------
def _render_theme(theme_name: str, plan: DeckPlan, rows: list[dict], out_dir: Path) -> dict:
    """Render one theme to .pptx (+ optional .pdf/.png). Returns an audit summary."""
    out_dir.mkdir(parents=True, exist_ok=True)
    pptx_path = out_dir / f"{theme_name}.pptx"

    ctx = ExportContext(
        source="preview-gallery",
        user_message="Polymer price chatpath weekly review",
        theme=theme_name,
        mode="dark" if "dark" in theme_name else "light",
        deck_type="data_report",
        deck_plan=plan,
    )
    blob, _report = render_pptx_from_plan_sync(
        plan, rows, ctx, user_message="Polymer price chatpath weekly review"
    )
    pptx_path.write_bytes(blob)

    # PDF + thumbnail (best effort — needs soffice + PyMuPDF).
    pdf_path = None
    png_path = None
    try:
        pdf_path = _to_pdf(pptx_path)
        if pdf_path:
            png_path = _pdf_first_page_png(pdf_path)
    except Exception as exc:  # gallery must still succeed without soffice
        print(f"  [warn] pdf/thumbnail skipped for {theme_name}: {exc}")

    # Run the 13-check audit.
    report = audit(str(pptx_path))
    summary = report.get("summary", {})
    return {
        "theme": theme_name,
        "pptx": str(pptx_path.relative_to(_BACKEND_ROOT)),
        "pdf": str(pdf_path.relative_to(_BACKEND_ROOT)) if pdf_path else None,
        "thumbnail": str(png_path.relative_to(_BACKEND_ROOT)) if png_path else None,
        "audit_status": report["status"],
        "audit_n_fail": summary.get("fail", 0),
        "audit_n_warn": summary.get("warn", 0),
    }


def _to_pdf(pptx_path: Path) -> Path | None:
    pdf_path = pptx_path.with_suffix(".pdf")
    proc = subprocess.run(
        [_SOFFICE, "--headless", "--convert-to", "pdf", "--outdir",
         str(pptx_path.parent), str(pptx_path)],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0 or not pdf_path.exists():
        return None
    return pdf_path


def _pdf_first_page_png(pdf_path: Path) -> Path | None:
    try:
        import fitz  # PyMuPDF
    except Exception:
        return None
    png_path = pdf_path.with_suffix(".png")
    doc = fitz.open(str(pdf_path))
    if doc.page_count == 0:
        return None
    pix = doc[0].get_pixmap(dpi=110)
    pix.save(str(png_path))
    return png_path


def _render_profile_deck(profile_name: str, out_dir: Path) -> dict:
    """Render one deck using the deterministic fallback for a given profile."""
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = _sample_rows()
    prof = ALL_PROFILES[profile_name]
    plan = _fallback_plan("Profile showcase", profile_rows(rows), rows,
                          profile_name=profile_name)
    # Stamp the profile name so the deck reads clearly in the gallery.
    plan.title = f"{prof.name.replace('_', ' ').title()} — showcase"
    pptx_path = out_dir / f"{profile_name}.pptx"
    ctx = ExportContext(
        source="preview-gallery-profile",
        theme=plan.theme_recommendation or "zhanlu-blue",
        mode="light",
        deck_plan=plan,
    )
    blob, _ = render_pptx_from_plan_sync(plan, rows, ctx, user_message="showcase")
    pptx_path.write_bytes(blob)
    report = audit(str(pptx_path))
    summary = report.get("summary", {})
    return {
        "profile": profile_name,
        "pptx": str(pptx_path.relative_to(_BACKEND_ROOT)),
        "audit_status": report["status"],
        "audit_n_fail": summary.get("fail", 0),
        "audit_n_warn": summary.get("warn", 0),
    }


# --- Main ----------------------------------------------------------------------
def main() -> int:
    _GALLERY_DIR.mkdir(parents=True, exist_ok=True)
    themes = [t["name"] for t in list_themes()]
    plan = _chatpath_plan()
    rows = _sample_rows()

    print(f"Regenerating preview gallery for {len(themes)} themes → {_GALLERY_DIR}")
    entries = []
    bad = []
    for name in themes:
        print(f"  • {name}")
        try:
            entry = _render_theme(name, plan, rows, _GALLERY_DIR / name)
        except Exception as exc:
            print(f"    [ERROR] render failed: {exc}")
            bad.append(name)
            continue
        entries.append(entry)
        if entry["audit_status"] == "FAIL":
            bad.append(name)

    # Write INDEX.md + manifest.json
    index = ["# PPTX Preview Gallery", "",
             "Rendered with the canonical *chatpath* sample deck. "
             "Each theme folder contains `<theme>.pptx` (+ `.pdf`/`.png` when "
             "LibreOffice is available) and a 13-check audit result.", ""]
    for e in entries:
        thumb = f" ![{e['theme']}]({e['theme']}/{Path(e['thumbnail']).name})" if e["thumbnail"] else ""
        index.append(
            f"- **{e['theme']}** — audit: {e['audit_status']} "
            f"(fail={e['audit_n_fail']}, warn={e['audit_n_warn']}){thumb}"
        )

    # Phase 4 — per-profile showcase.
    index.append("")
    index.append("## Deck Profiles")
    index.append("")
    index.append("One deck per intent-driven profile, rendered from the same "
                 "sample data so the structural differences are visible.")
    index.append("")
    profile_entries = []
    profile_dir = _GALLERY_DIR / "profiles"
    for pname in ALL_PROFILES:
        print(f"  • profile: {pname}")
        try:
            pentry = _render_profile_deck(pname, profile_dir / pname)
        except Exception as exc:
            print(f"    [ERROR] profile render failed: {exc}")
            bad.append(f"profile:{pname}")
            continue
        profile_entries.append(pentry)
        if pentry["audit_status"] == "FAIL":
            bad.append(f"profile:{pname}")
        index.append(
            f"- **{pname}** — audit: {pentry['audit_status']} "
            f"(fail={pentry['audit_n_fail']}, warn={pentry['audit_n_warn']})"
        )
    (_GALLERY_DIR / "INDEX.md").write_text("\n".join(index) + "\n")
    (_GALLERY_DIR / "manifest.json").write_text(
        json.dumps({"themes": entries, "profiles": profile_entries},
                   indent=2, ensure_ascii=False)
    )

    if bad:
        print(f"\n[FAIL] {len(bad)} item(s) produced a FAIL audit: {bad}", file=sys.stderr)
        return 1
    print(f"\n[OK] {len(entries)} themes + {len(profile_entries)} profiles rendered; all audits clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
