"""HTML → DOCX renderer — converts standalone HTML bytes to a .docx file.

Two-stage strategy:
1. **pandoc** (preferred) — pipe HTML in, get .docx out.  Handles CSS,
   tables, images far better than python-docx.
2. **python-docx + BeautifulSoup** (fallback) — parse HTML structure and
   rebuild it with python-docx styles.  Best-effort; complex layouts will
   be simplified.

Public entry point: ``render_html_to_docx(html_bytes) -> bytes``

**Disk-write audit (2026-07-15):**  The pandoc path writes HTML and DOCX
to ``NamedTemporaryFile`` (``delete=False``), cleaned in a ``finally`` block.
The python-docx+bs4 path is fully in-memory (``BytesIO``).  No persistent
disk writes remain.
"""

from __future__ import annotations

import io
import logging
import os
import subprocess
import tempfile

logger = logging.getLogger(__name__)

MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
EXT = ".docx"


def render_html_to_docx(html_bytes: bytes) -> bytes:
    """Convert HTML content to a .docx file.

    Tries pandoc first; falls back to python-docx + BeautifulSoup if
    pandoc is unavailable or fails.
    """
    # Try pandoc first — far richer output
    try:
        return _via_pandoc(html_bytes)
    except (FileNotFoundError, subprocess.CalledProcessError, OSError) as exc:
        logger.info("pandoc HTML→DOCX failed (%s), falling back to python-docx", exc)

    # Fallback: python-docx + BeautifulSoup
    try:
        return _via_python_docx(html_bytes)
    except ImportError as exc:
        logger.error("Neither pandoc nor python-docx+bs4 is available: %s", exc)
        raise RuntimeError("Cannot render HTML to DOCX — missing dependencies") from exc
    except Exception as exc:
        logger.error("python-docx HTML→DOCX fallback failed: %s", exc)
        raise


# ── Pandoc path ────────────────────────────────────────────────────────────


def _via_pandoc(html_bytes: bytes) -> bytes:
    """Pipe HTML bytes through pandoc to produce .docx."""
    # Write HTML to a temp file (pandoc reads from stdin but .docx needs a file)
    with tempfile.NamedTemporaryFile(
        mode="wb", suffix=".html", delete=False
    ) as hf:
        hf.write(html_bytes)
        html_path = hf.name

    docx_path = html_path.replace(".html", ".docx")
    try:
        subprocess.run(
            [
                "pandoc",
                "-f", "html",
                "-t", "docx",
                "--standalone",
                "-o", docx_path,
                html_path,
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        with open(docx_path, "rb") as df:
            result = df.read()
        logger.info("HTML→DOCX via pandoc: %d bytes", len(result))
        return result
    finally:
        for p in (html_path, docx_path):
            try:
                os.unlink(p)
            except OSError:
                pass


# ── python-docx + BeautifulSoup fallback ───────────────────────────────────


def _via_python_docx(html_bytes: bytes) -> bytes:
    """Build a .docx by parsing HTML structure with BeautifulSoup and
    translating semantic elements (h1–h6, p, ul, ol, table, pre, blockquote)
    into python-docx styles.
    """
    from bs4 import BeautifulSoup
    from docx import Document
    from docx.shared import Pt, Inches, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    soup = BeautifulSoup(html_bytes, "html.parser")
    doc = Document()

    # Normal style
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)

    # Mapping: tag → (heading_level, bold)
    HEADING_MAP = {
        "h1": 0, "h2": 1, "h3": 2, "h4": 3, "h5": 4, "h6": 5,
    }

    def _add_paragraph(text: str, style: str | None = None):
        """Add a paragraph with optional style name."""
        if not text.strip():
            return
        if style and style in [s.name for s in doc.styles]:
            p = doc.add_paragraph(text, style=style)
        else:
            p = doc.add_paragraph(text)

    def _process_element(el):
        tag = (el.name or "").lower() if hasattr(el, "name") else ""

        if tag in HEADING_MAP:
            level = HEADING_MAP[tag]
            text = el.get_text(strip=True)
            if text:
                doc.add_heading(text, level=level)

        elif tag == "p":
            text = el.get_text(strip=True)
            _add_paragraph(text)

        elif tag in ("ul", "ol"):
            for li in el.find_all("li", recursive=False):
                text = li.get_text(strip=True)
                if text:
                    p = doc.add_paragraph(text, style="List Bullet" if tag == "ul" else "List Number")

        elif tag == "table":
            rows = el.find_all("tr")
            if not rows:
                return
            # Determine column count from headers or first row
            header_cells = rows[0].find_all(["th", "td"])
            cols = len(header_cells) if header_cells else 1
            if cols == 0:
                cols = 1
            table = doc.add_table(rows=len(rows), cols=cols, style="Light Grid Accent 1")
            for i, row in enumerate(rows):
                cells = row.find_all(["th", "td"])
                for j, cell in enumerate(cells):
                    if j < cols:
                        table.rows[i].cells[j].text = cell.get_text(strip=True)

        elif tag == "pre":
            text = el.get_text()
            if text.strip():
                p = doc.add_paragraph()
                run = p.add_run(text)
                run.font.name = "Courier New"
                run.font.size = Pt(9)

        elif tag == "blockquote":
            text = el.get_text(strip=True)
            if text:
                p = doc.add_paragraph(text, style="Quote")

        elif tag == "hr":
            doc.add_paragraph("_" * 60)

        else:
            # Recurse into children for unknown containers (body, div, section, etc.)
            if hasattr(el, "children"):
                for child in el.children:
                    if hasattr(child, "name"):
                        _process_element(child)

    # Walk the body or top-level elements
    body = soup.find("body")
    if body:
        for child in body.children:
            if hasattr(child, "name"):
                _process_element(child)
    else:
        for child in soup.children:
            if hasattr(child, "name"):
                _process_element(child)

    # Add footer
    doc.add_paragraph()
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = p.add_run("Generated by Zhanlu AI")
    run.font.size = Pt(8)
    run.font.color.rgb = RGBColor(0x99, 0x99, 0x99)

    buf = io.BytesIO()
    doc.save(buf)
    result = buf.getvalue()
    logger.info("HTML→DOCX via python-docx+bs4: %d bytes", len(result))
    return result


def render(payload, ctx=None):
    """Adapter: ``render(payload, ctx) -> (bytes, mime, ext)`` so this module
    can be registered as an ExportService renderer alongside existing ones.

    ``payload`` is expected to be raw HTML bytes (not a ReportCardPayload).
    The caller (ExportService) wraps this when ``canonical_format=="html"``.
    """
    if isinstance(payload, bytes):
        html = payload
    elif isinstance(payload, str):
        html = payload.encode("utf-8")
    else:
        raise TypeError(f"html_docx render expects bytes or str, got {type(payload)}")
    return render_html_to_docx(html), MIME, EXT
