"""Part 1 live demo driver for the deck-edit PPT pipeline.

Runs the REAL code path with the 5 flags enabled:
  1. generate  -> "make a PPT with my data"
  2. edit_slide(slide 3 shorter) -> "make slide 3 shorter"
  3. add_slide(risks) -> "add a slide about risks"
  4. restyle_deck(dark) -> "switch to dark theme"

For each step it prints:
  - the exact agent reply string
  - get_versions() output (version_number / changelog / produced_by_skill)
  - slide thumbnail PNG paths (one per slide)

Run with: docker exec -it zhanlu-backend bash -c "cd /app && venv/bin/python scripts/demo_deck_pipeline.py"
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("demo_deck")


def render_thumbnails(data: bytes, label: str) -> list[Path]:
    """Render slide thumbnails for a pptx via soffice -> PDF -> PyMuPDF -> PNG.

    Container has soffice but no poppler (pdftoppm), so we split the PDF with
    PyMuPDF instead of the production render_page_thumbnails path.
    """
    from app.services.artifacts.artifact_service import ArtifactService
    from app.database import SessionLocal

    out_dir = Path(tempfile.mkdtemp(prefix=f"deck-thumbs-{label}-"))
    work = out_dir / "work"
    work.mkdir(parents=True, exist_ok=True)
    src = work / "input.pptx"
    src.write_bytes(data)
    profile = work / "lo-profile"
    profile.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["soffice", f"-env:UserInstallation=file://{profile}", "--headless",
         "--norestore", "--nolockcheck", "--nologo", "--convert-to", "pdf",
         "--outdir", str(work), str(src)],
        capture_output=True, text=True, timeout=120,
    )
    pdf = work / "input.pdf"
    if not pdf.exists():
        logger.warning("thumbnails: soffice did not produce a PDF")
        return []
    import fitz  # PyMuPDF
    doc = fitz.open(pdf)
    paths = []
    for i, page in enumerate(doc, start=1):
        pix = page.get_pixmap(dpi=110)
        p = out_dir / f"slide-{i}.png"
        pix.save(str(p))
        paths.append(p)
    doc.close()
    logger.info("thumbnails (%d): %s", len(paths), ", ".join(str(p) for p in paths))
    return paths

# ---- Enable the 5 flags (repo defaults stay False; this is in-process only) --
from app.config import settings  # noqa: E402

settings.DECK_EDIT_ROUTING_ENABLED = True
settings.PPT_DECK_PLANNER_ENABLED = True
settings.PPT_SMART_ROUTER_ENABLED = True
settings.PPT_LLM_POLISH_ENABLED = True
settings.PPT_AUDIT_ENABLED = True

from app.database import SessionLocal  # noqa: E402
from app.services.artifacts.deck_planner import build_deck_plan  # noqa: E402
from app.services.artifacts.render_dispatcher import render_pptx_from_plan_sync  # noqa: E402
from app.services.artifacts.artifact_service import ArtifactService  # noqa: E402
from app.services.artifacts.exporters._common import ExportContext  # noqa: E402
from app.services.tool_handlers.deck_edit_tool import _run_deck_edit  # noqa: E402

USER_ID = "demo-user"
CONV_ID = "demo-conv-deck"
# Agent-loop context (dict) expected by _run_deck_edit's tenant checks.
AGENT_CTX = {
    "org_id": "default-org",
    "app_id": "default-app",
    "conversation_id": CONV_ID,
}

# A realistic 15-category dataset so the chart/table cap (Fix #3) is exercised.
ROWS = [
    {"category": f"Product {i:02d}", "revenue": (15 - i) * 1000 + 500}
    for i in range(15)
]
USER_MESSAGE = "Make a sales review PPT with my data"


def _print_versions(svc: ArtifactService, artifact_id: str, label: str) -> None:
    versions = svc.get_versions(artifact_id)
    logger.info("=== get_versions after: %s ===", label)
    for v in versions:
        logger.info(
            "  v%-2d | skill=%-20s | changelog=%s",
            v.version_number, v.produced_by_skill, v.changelog,
        )


def main() -> None:
    db = SessionLocal()
    svc = ArtifactService(db)
    ctx = ExportContext(source="demo", conversation_id=CONV_ID, user_message=USER_MESSAGE)

    # ---- STEP 1: GENERATE -----------------------------------------------------
    logger.info("\n##### STEP 1/4: GENERATE -> %r", USER_MESSAGE)
    import asyncio

    plan, _profile = asyncio.run(build_deck_plan(USER_MESSAGE, ROWS))
    ctx.deck_plan = plan
    data, _report = render_pptx_from_plan_sync(plan, ROWS, ctx)
    assert data, "render produced no bytes"

    artifact = svc.create_artifact(
        artifact_type="pptx", title="Sales Review Q3", conversation_id=CONV_ID
    )
    version = svc.create_version(
        artifact.id,
        changelog="Initial generation",
        source_json={"deck_plan": plan.model_dump()},
        produced_by_skill="pptx-export",
    )
    svc.store_blob(version.id, "original", "deck.pptx",
                  "application/vnd.openxmlformats-officedocument.presentationml.presentation", data)
    svc.store_blob(version.id, "preview", "preview.pdf", "application/pdf", data)
    svc.mark_version_built(version.id)
    _print_versions(svc, artifact.id, "generate")
    render_thumbnails(data, "generate")

    artifact_id = artifact.id

    # ---- STEP 2: EDIT SLIDE 3 (shorter) ---------------------------------------
    logger.info("\n##### STEP 2/4: EDIT SLIDE 3 -> 'make slide 3 shorter'")
    res2 = asyncio.run(_run_deck_edit(
        "edit_slide",
        {"artifact_id": artifact_id, "slide_index": 2,
         "changes": {"bullets": ["Q3 revenue up across all regions.", "East remains the top contributor."]}},
        db, USER_ID, AGENT_CTX,
    ))
    logger.info("REPLY: %s", res2.get("message"))
    _print_versions(svc, artifact_id, "edit_slide")
    render_thumbnails(svc.get_blob_data(svc.get_original_blob(artifact_id)), "edit_slide")

    # ---- STEP 3: ADD RISKS SLIDE ----------------------------------------------
    logger.info("\n##### STEP 3/4: ADD SLIDE -> 'add a slide about risks'")
    res3 = asyncio.run(_run_deck_edit(
        "add_slide",
        {"artifact_id": artifact_id,
         "slide": {"layout": "insights_bullets", "title": "Key Risks",
                   "bullets": ["Supply-chain lead times rising", "Price volatility in raw materials",
                               "Concentration in top-3 customers"]}},
        db, USER_ID, AGENT_CTX,
    ))
    logger.info("REPLY: %s", res3.get("message"))
    _print_versions(svc, artifact_id, "add_slide")
    render_thumbnails(svc.get_blob_data(svc.get_original_blob(artifact_id)), "add_slide")

    # ---- STEP 4: DARK THEME ---------------------------------------------------
    logger.info("\n##### STEP 4/4: RESTYLE -> 'switch to dark theme'")
    res4 = asyncio.run(_run_deck_edit(
        "restyle_deck",
        {"artifact_id": artifact_id, "theme": "midnight", "mode": "dark"},
        db, USER_ID, AGENT_CTX,
    ))
    logger.info("REPLY: %s", res4.get("message"))
    _print_versions(svc, artifact_id, "restyle_deck")
    render_thumbnails(svc.get_blob_data(svc.get_original_blob(artifact_id)), "restyle_deck")

    logger.info("\nDONE. Artifact id=%s", artifact_id)
    db.close()


if __name__ == "__main__":
    main()
