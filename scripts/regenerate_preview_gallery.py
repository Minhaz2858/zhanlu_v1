"""Regenerate the PPT preview gallery (Phase 1B verification) with the
Fix #2 (empty insights slides dropped) and Fix #3 (chart/table cap at top-8
+ Other) applied.

Produces 5 decks and copies pptx/pdf + slide PNG thumbnails into the gallery:

  backend/data/uploads/ppt_preview/
      demo/                 <- final demo deck (generate -> edit -> add -> restyle)
      chatpath/             <- realistic sales deck (no empty insights)
      robustness/small/     <- ~5 rows
      robustness/big/       <- 200+ rows / 10+ cols (chart top-8 + Other, table top-8 + Other)
      robustness/messy/    <- missing values / long cols / no numeric -> chart skipped

Run with:
  docker exec zhanlu-backend bash -c "cd /app && PYTHONPATH=/app python scripts/regenerate_preview_gallery.py"
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("regenerate_gallery")

# Gallery root: inside the container this is /app/data/uploads/ppt_preview
# (the repo's backend/data/uploads). Allow override via env.
GALLERY = Path(os.environ.get("GALLERY_DIR", "/app/data/uploads/ppt_preview"))
GALLERY.mkdir(parents=True, exist_ok=True)

# ---- Enable the 5 flags (repo defaults stay False; in-process only) ----------
from app.config import settings  # noqa: E402

settings.DECK_EDIT_ROUTING_ENABLED = True
settings.PPT_DECK_PLANNER_ENABLED = True
settings.PPT_SMART_ROUTER_ENABLED = True
settings.PPT_LLM_POLISH_ENABLED = True
settings.PPT_AUDIT_ENABLED = True

from app.database import SessionLocal  # noqa: E402
from app.services.artifacts.deck_planner import build_deck_plan  # noqa: E402
from app.services.artifacts.render_dispatcher import render_pptx_from_plan_sync  # noqa: E402
from app.services.artifacts.exporters._common import ExportContext  # noqa: E402
from app.services.tool_handlers.deck_edit_tool import _run_deck_edit  # noqa: E402

USER_ID = "demo-user"
CONV_ID = "demo-conv-gallery"
AGENT_CTX = {"org_id": "default-org", "app_id": "default-app", "conversation_id": CONV_ID}


def render_thumbnails(data: bytes, label: str, dest_dir: Path) -> list[Path]:
    """soffice -> PDF -> PyMuPDF -> PNG into dest_dir; returns PNG paths."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = Path(tempfile.mkdtemp(prefix=f"gallery-{label}-"))
    work = out / "work"
    work.mkdir(parents=True, exist_ok=True)
    src = work / "input.pptx"
    src.write_bytes(data)
    profile = work / "lo-profile"
    profile.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["soffice", f"-env:UserInstallation=file://{profile}", "--headless",
         "--norestore", "--nolockcheck", "--nologo", "--convert-to", "pdf",
         "--outdir", str(work), str(src)],
        capture_output=True, text=True, timeout=180,
    )
    pdf = work / "input.pdf"
    if not pdf.exists():
        logger.warning("[%s] soffice did not produce a PDF", label)
        return []
    import fitz  # PyMuPDF
    doc = fitz.open(pdf)
    paths = []
    for i, page in enumerate(doc, start=1):
        pix = page.get_pixmap(dpi=110)
        p = dest_dir / f"slide-{i}.png"
        pix.save(str(p))
        paths.append(p)
    doc.close()
    logger.info("[%s] thumbnails (%d): %s", label, len(paths),
                ", ".join(p.name for p in paths))
    return paths


def write_artifacts(label: str, data: bytes, out_dir: Path) -> None:
    """Persist deck.pptx + preview.pdf into out_dir and render thumbnails.

    The target dir may be owned by a different uid (left over from a prior
    container run), so we recreate it to guarantee writability.
    """
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "deck.pptx").write_bytes(data)
    # Reuse the rendered PDF as the preview artifact.
    out = Path(tempfile.mkdtemp(prefix=f"pdf-{label}-"))
    work = out / "work"
    work.mkdir(parents=True, exist_ok=True)
    src = work / "input.pptx"
    src.write_bytes(data)
    profile = work / "lo-profile"
    profile.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["soffice", f"-env:UserInstallation=file://{profile}", "--headless",
         "--norestore", "--nolockcheck", "--nologo", "--convert-to", "pdf",
         "--outdir", str(work), str(src)],
        capture_output=True, text=True, timeout=180,
    )
    pdf = work / "input.pdf"
    if pdf.exists():
        shutil.copy(pdf, out_dir / "preview.pdf")
        (out_dir / "deck.pdf").write_bytes(pdf.read_bytes())
    render_thumbnails(data, label, out_dir)


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------

def demo_rows():
    # 15-category revenue so Fix #3 chart cap is exercised.
    return [{"category": f"Product {i:02d}", "revenue": (15 - i) * 1000 + 500}
            for i in range(15)]


def chatpath_rows():
    # Realistic materials dataset (mirrors the original chat-path deck values).
    materials = [
        ("碳五石油树脂", 22.1e6), ("碳九石油树脂", 18.4e6), ("DCPD石油树脂", 11.2e6),
        ("氢化石油树脂", 9.8e6), ("C5/C9共聚树脂", 7.6e6), ("松香季戊四醇酯", 5.4e6),
        ("萜烯树脂", 4.1e6), ("古马隆树脂", 3.2e6), ("二甲苯甲醛树脂", 2.6e6),
        ("不饱和聚酯树脂", 2.1e6), ("醇酸树脂", 1.8e6), ("丙烯酸树脂", 1.5e6),
        ("环氧大豆油", 1.2e6), ("PVC复合稳定剂", 0.9e6), ("其他特种树脂", 0.7e6),
    ]
    return [{"material": m, "revenue": v, "volume": int(v / 1000)}
            for m, v in materials]


def small_rows():
    return [
        {"region": "East", "revenue": 4200},
        {"region": "South", "revenue": 3100},
        {"region": "North", "revenue": 2700},
        {"region": "West", "revenue": 1900},
        {"region": "Central", "revenue": 1500},
    ]


def big_rows():
    # 200+ rows across 12 product families (cardinality 12 -> suggest_charts
    # picks `product` as the chart x-axis). The cap aggregates 228 raw rows into
    # 12 families, then top-8 + Other -> 9 chart categories. Also proves the
    # table path truncates to 8 + Other.
    families = [
        "Carbon Resin", "Hydrogenated Resin", "C9 Resin", "DCPD Resin",
        "Rosin Ester", "Terpene Resin", "Coumarone Resin", "Xylenol Resin",
        "Polyester Resin", "Alkyd Resin", "Acrylic Resin", "Epoxy Plasticizer",
    ]
    rows = []
    for i in range(228):
        fam = families[i % len(families)]
        rows.append({
            "product": fam,
            "revenue": (228 - i) // len(families) * 1500 + (i % 7) * 100 + 250,
        })
    return rows


def messy_rows():
    return [
        {"customer name (long label)": "Acme Corp", "region": "East", "sales": "n/a"},
        {"customer name (long label)": "Globex", "region": "West", "sales": ""},
        {"customer name (long label)": "Initech", "region": "", "sales": None},
        {"customer name (long label)": "Umbrella", "region": "North", "sales": "—"},
        {"customer name (long label)": "Soylent", "region": "South", "sales": "TBD"},
    ]


# ---------------------------------------------------------------------------
# Deck builders
# ---------------------------------------------------------------------------

def build_basic(rows, title, user_message, deck_type="data_report", use_fallback=False):
    prev = settings.PPT_DECK_PLANNER_ENABLED
    if use_fallback:
        settings.PPT_DECK_PLANNER_ENABLED = False
    try:
        plan, _profile = asyncio.run(build_deck_plan(user_message, rows))
    finally:
        if use_fallback:
            settings.PPT_DECK_PLANNER_ENABLED = prev
    ctx = ExportContext(source="gallery", conversation_id=CONV_ID,
                        user_message=user_message, deck_type=deck_type)
    ctx.deck_plan = plan
    data, _report = render_pptx_from_plan_sync(plan, rows, ctx)
    assert data, f"{title}: render produced no bytes"
    return data


def build_demo_final():
    """Run the full 4-step demo pipeline and return the final pptx bytes."""
    db = SessionLocal()
    svc = None
    try:
        from app.services.artifacts.artifact_service import ArtifactService
        svc = ArtifactService(db)
        rows = demo_rows()
        plan, _profile = asyncio.run(build_deck_plan("Make a sales review PPT", rows))
        ctx = ExportContext(source="gallery", conversation_id=CONV_ID,
                            user_message="Make a sales review PPT")
        ctx.deck_plan = plan
        data, _report = render_pptx_from_plan_sync(plan, rows, ctx)

        artifact = svc.create_artifact(artifact_type="pptx",
                                       title="Sales Review Q3",
                                       conversation_id=CONV_ID)
        version = svc.create_version(
            artifact.id, changelog="Initial generation",
            source_json={"deck_plan": plan.model_dump()},
            produced_by_skill="pptx-export",
        )
        svc.store_blob(version.id, "original", "deck.pptx",
                       "application/vnd.openxmlformats-officedocument.presentationml.presentation", data)
        svc.mark_version_built(version.id)
        aid = artifact.id

        asyncio.run(_run_deck_edit(
            "edit_slide",
            {"artifact_id": aid, "slide_index": 2,
             "changes": {"bullets": ["Q3 revenue up across all regions.",
                                     "East remains the top contributor."]}},
            db, USER_ID, AGENT_CTX))
        asyncio.run(_run_deck_edit(
            "add_slide",
            {"artifact_id": aid,
             "slide": {"layout": "insights_bullets", "title": "Key Risks",
                       "bullets": ["Supply-chain lead times rising",
                                   "Price volatility in raw materials",
                                   "Concentration in top-3 customers"]}},
            db, USER_ID, AGENT_CTX))
        asyncio.run(_run_deck_edit(
            "restyle_deck",
            {"artifact_id": aid, "theme": "midnight", "mode": "dark"},
            db, USER_ID, AGENT_CTX))
        return svc.get_blob_data(svc.get_original_blob(aid))
    finally:
        db.close()


def main() -> None:
    # 1. Demo final -> demo/
    logger.info("=== Building demo final deck ===")
    demo_data = build_demo_final()
    write_artifacts("demo", demo_data, GALLERY / "demo")

    # 2. chatpath
    logger.info("=== Building chatpath deck ===")
    cp_data = build_basic(chatpath_rows(), "Chat-path sales deck",
                          "Summarize our resin sales by material")
    write_artifacts("chatpath", cp_data, GALLERY / "chatpath")

    # 3. small
    logger.info("=== Building small deck ===")
    sm_data = build_basic(small_rows(), "Small dataset deck",
                          "Show my regional revenue")
    write_artifacts("small", sm_data, GALLERY / "robustness" / "small")

    # 4. big (deterministic fallback -> guarantees a native chart + capped table)
    logger.info("=== Building big deck ===")
    bg_data = build_basic(big_rows(), "Big dataset deck",
                          "Show top SKUs by revenue", deck_type="data_report",
                          use_fallback=True)
    write_artifacts("big", bg_data, GALLERY / "robustness" / "big")

    # 5. messy
    logger.info("=== Building messy deck ===")
    my_data = build_basic(messy_rows(), "Messy dataset deck",
                          "Summarize these customer rows")
    write_artifacts("messy", my_data, GALLERY / "robustness" / "messy")

    logger.info("DONE. Gallery written to %s", GALLERY.resolve())


if __name__ == "__main__":
    main()
