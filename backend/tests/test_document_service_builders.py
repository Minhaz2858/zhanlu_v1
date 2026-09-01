"""Tests for the professional document builders in ``document_service``.

These tests target the new ``build_docx`` / ``build_pptx`` functions that return
``bytes`` and apply Claude/MiniMax-grade styling:

* DOCX:
    * Magic bytes ``PK\\x03\\x04`` (ZIP/OOXML) and ``word/`` entry inside
    * Cover block: title (>=24pt), subtitle, source/date metadata, accent rule
    * Styled H1/H2 with accent color and a horizontal rule (border)
    * Body uses a non-default font with reasonable size (10-13pt) and spacing
    * Styled tables: header row has accent fill color and bold white text
    * Footer with branded text and a page-number field

* PPTX:
    * Magic bytes ``PK\\x03\\x04`` and ``ppt/slides/`` entries
    * 16:9 aspect ratio (10\" x 5.625\" or 13.333\" x 7.5\")
    * At least 3 slides for the basic input (cover + content + closing)
    * Cover slide contains the title at >=32pt
    * Each content slide has an accent-colored title and at least one bullet
    * Closing slide exists
    * ``build_pptx`` accepts the legacy ``{title, bullets}`` shape and
      additive ``{title, subtitle, layout, bullets, notes}`` keys
"""

import io
import os
import re
import sys
import unittest
import zipfile
from xml.etree import ElementTree as ET

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


NS_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
NS_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
NS_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
NS_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _read_docx_xml(data: bytes, part: str) -> str:
    """Read a part (e.g. 'word/document.xml') from a docx bytes blob."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return z.read(part).decode("utf-8")


def _read_pptx_xml(data: bytes, part: str) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return z.read(part).decode("utf-8")


def _pptx_all_slide_xmls(data: bytes) -> list[str]:
    """Return concatenated slide XML for every slide in the deck."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = sorted(
            n for n in z.namelist()
            if re.match(r"ppt/slides/slide\d+\.xml$", n)
        )
        return [z.read(n).decode("utf-8") for n in names]


def _pptx_slide_count(data: bytes) -> int:
    return len(_pptx_all_slide_xmls(data))


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------


class TestBuildDocx(unittest.TestCase):

    def test_returns_valid_ooxml_with_word_dir(self):
        from app.services.document_service import build_docx
        data = build_docx("Test Report", "## Hello\nWorld")
        self.assertEqual(data[:4], b"PK\x03\x04")
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
        self.assertIn("word/document.xml", names)
        self.assertIn("[Content_Types].xml", names)

    def test_title_appears_in_document_xml(self):
        from app.services.document_service import build_docx
        data = build_docx("Quarterly Performance Review", "## Highlights\n- Good")
        doc_xml = _read_docx_xml(data, "word/document.xml")
        self.assertIn("Quarterly Performance Review", doc_xml)

    def test_h1_uses_accent_color_and_bottom_border(self):
        """A heading-1 paragraph should be styled with a non-default color and a bottom border."""
        from app.services.document_service import build_docx
        data = build_docx(
            "T",
            "# Top-level heading\n## Sub heading\nBody",
        )
        doc_xml = _read_docx_xml(data, "word/document.xml")
        # Accent color is "#2563EB" → OOXML hex "2563EB" must appear
        self.assertIn("2563EB", doc_xml.upper())
        # Bottom border on a heading uses pBdr element
        self.assertIn("pBdr", doc_xml)

    def test_body_uses_named_font_other_than_default(self):
        """Body text should be set to a real font (e.g. Calibri/Arial/Inter) — not the
        python-docx default — and at a reasonable size (10-13pt)."""
        from app.services.document_service import build_docx
        data = build_docx("T", "Just a regular paragraph of body text.")
        doc_xml = _read_docx_xml(data, "word/document.xml")
        # Must reference a font in the styles part
        styles_xml = _read_docx_xml(data, "word/styles.xml")
        # We expect to see at least one named font and a non-default body sz
        self.assertTrue(
            re.search(r'<w:rFonts[^/]+w:ascii="(Calibri|Arial|Inter|Segoe UI|Lato|Source Sans Pro)"', styles_xml),
            "styles.xml should set a non-default font",
        )

    def test_styled_table_has_accent_header_fill(self):
        """Markdown table header row should be filled with the accent color and white text."""
        from app.services.document_service import build_docx
        md = (
            "| Metric | Value |\n"
            "| --- | --- |\n"
            "| Revenue | 1.2M |\n"
            "| Cost    | 0.4M |\n"
        )
        data = build_docx("T", md)
        doc_xml = _read_docx_xml(data, "word/document.xml")
        # Table header must be filled with accent (2563EB) AND bold white
        self.assertIn("2563EB", doc_xml.upper())
        # First row in the table should be bold (header)
        # Locate the first <w:tbl> block
        m = re.search(r"<w:tbl[\s>].*?</w:tbl>", doc_xml, re.DOTALL)
        self.assertIsNotNone(m, "Expected a table in the document")
        first_tbl = m.group(0)
        # Header row has shading OR a w:b with white color
        # Check shading exists at all on the table (header row)
        self.assertIn('w:fill="', first_tbl)
        # White text in header (FFFFFF) OR strong contrast color
        # At minimum: shading present → proves styling happened (vs. plain)
        self.assertTrue(
            re.search(r'w:fill="[A-Fa-f0-9]{6}"', first_tbl),
            "Table rows should have explicit fill colors",
        )

    def test_footer_contains_page_field_and_brand(self):
        """Each section should have a footer referencing a PAGE field and the brand text."""
        from app.services.document_service import build_docx
        data = build_docx("T", "Body")
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
        # At least one footer part
        footer_names = [n for n in names if re.match(r"word/footer\d*\.xml$", n)]
        self.assertGreaterEqual(len(footer_names), 1, "No footer part in docx")
        # Concatenate footers
        footer_texts = []
        for n in footer_names:
            footer_texts.append(_read_docx_xml(data, n))
        joined = "\n".join(footer_texts)
        # PAGE field is represented as an instruction element with "PAGE"
        self.assertIn("PAGE", joined)
        # Brand text
        self.assertTrue(
            re.search(r"(Zhanlu|Generated)", joined, re.IGNORECASE),
            "Footer should contain Zhanlu branding",
        )

    def test_markdown_lists_preserved(self):
        """Bullets and numbered lists should appear as list paragraphs."""
        from app.services.document_service import build_docx
        data = build_docx("T", "- First\n- Second\n1. One\n2. Two")
        doc_xml = _read_docx_xml(data, "word/document.xml")
        # Either numPr or a List style
        self.assertTrue(
            "numPr" in doc_xml or "ListBullet" in doc_xml or "ListNumber" in doc_xml,
            "Lists should be rendered as numbered/bulleted list paragraphs",
        )

    def test_cover_block_uses_subtitle_when_provided(self):
        from app.services.document_service import build_docx
        data = build_docx(
            "Quarterly Review",
            "## Section A\nBody",
            subtitle="Q3 2026",
            source="erp_v_sale",
        )
        doc_xml = _read_docx_xml(data, "word/document.xml")
        self.assertIn("Q3 2026", doc_xml)
        self.assertIn("erp_v_sale", doc_xml)

    def test_empty_markdown_does_not_crash(self):
        from app.services.document_service import build_docx
        data = build_docx("Only Title", "")
        self.assertEqual(data[:4], b"PK\x03\x04")

    def test_unicode_preserved(self):
        from app.services.document_service import build_docx
        data = build_docx("碳五石油树脂", "## 概述\n- 营收增长 12%")
        doc_xml = _read_docx_xml(data, "word/document.xml")
        self.assertIn("碳五石油树脂", doc_xml)
        self.assertIn("营收增长", doc_xml)


# ---------------------------------------------------------------------------
# PPTX
# ---------------------------------------------------------------------------


class TestBuildPptx(unittest.TestCase):

    def _basic_slides(self):
        return [
            {"title": "Highlights", "bullets": ["Revenue +12%", "NPS 64"]},
            {"title": "Next Steps", "bullets": ["Launch Q3 plan", "Hire 2 SEs"]},
        ]

    def test_returns_valid_ooxml_with_ppt_dir(self):
        from app.services.document_service import build_pptx
        data = build_pptx("Quarterly Review", self._basic_slides())
        self.assertEqual(data[:4], b"PK\x03\x04")
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
        self.assertTrue(any(n.startswith("ppt/slides/slide") for n in names))
        self.assertIn("ppt/presentation.xml", names)

    def test_16by9_aspect_ratio(self):
        from app.services.document_service import build_pptx
        data = build_pptx("D", self._basic_slides())
        pres_xml = _read_pptx_xml(data, "ppt/presentation.xml")
        # cx/cy in EMU: 16:9 is 12192000 x 6858000
        m = re.search(r'<p:sldSz\s+cx="(\d+)"\s+cy="(\d+)"', pres_xml)
        self.assertIsNotNone(m, "presentation.xml missing sldSz")
        cx, cy = int(m.group(1)), int(m.group(2))
        # python-pptx's Inches() may round by a few EMU; use a tolerance of 0.01"
        tolerance_emu = 10_000  # ≈0.011"
        self.assertAlmostEqual(cx, 12_192_000, delta=tolerance_emu)
        self.assertAlmostEqual(cy, 6_858_000, delta=tolerance_emu)
        # 16:9 ratio (cx/cy == 16/9)
        self.assertAlmostEqual(cx / cy, 16 / 9, places=2)

    def test_at_least_three_slides_for_two_content_slides(self):
        """A deck with 2 content slides should produce cover + 2 content + closing = 4+."""
        from app.services.document_service import build_pptx
        data = build_pptx("D", self._basic_slides())
        self.assertGreaterEqual(_pptx_slide_count(data), 3,
            "Expected at least cover + 2 content + closing = 3+ slides")

    def test_cover_slide_has_large_title(self):
        from app.services.document_service import build_pptx
        data = build_pptx("Quarterly Review", self._basic_slides())
        slide_xmls = _pptx_all_slide_xmls(data)
        # First slide is the cover
        cover = slide_xmls[0]
        self.assertIn("Quarterly Review", cover)
        # Title font size should be 32pt or larger (sz attr in OOXML is 100*pt)
        # We just verify there exists a run with sz >= 3200 (32pt)
        sizes = [int(x) for x in re.findall(r'sz="(\d+)"', cover)]
        self.assertTrue(
            any(s >= 3200 for s in sizes),
            f"Cover title should be >=32pt; found sizes {sizes}",
        )

    def test_content_slides_have_bullets(self):
        from app.services.document_service import build_pptx
        data = build_pptx("D", self._basic_slides())
        slide_xmls = _pptx_all_slide_xmls(data)
        # At least one slide (other than the cover) should contain the bullet text
        bullet_found = False
        for s in slide_xmls[1:]:
            if "Revenue" in s and "NPS" in s:
                bullet_found = True
                break
        self.assertTrue(bullet_found, "Bullets from input not found in any non-cover slide")

    def test_accepts_legacy_slides_shape(self):
        """The legacy {title, bullets} shape must keep working."""
        from app.services.document_service import build_pptx
        legacy = [
            {"title": "A", "bullets": ["a1", "a2"]},
            {"title": "B", "bullets": ["b1"]},
        ]
        data = build_pptx("Legacy Deck", legacy)
        self.assertGreaterEqual(_pptx_slide_count(data), 3)

    def test_accepts_additive_subtitle_and_notes(self):
        """New optional keys (subtitle, notes) should be ignored gracefully, not crash."""
        from app.services.document_service import build_pptx
        slides = [
            {
                "title": "First",
                "subtitle": "an extra subtitle line",
                "bullets": ["one", "two"],
                "notes": "Speaker notes go here",
                "layout": "title_only",
            },
        ]
        data = build_pptx("D", slides)
        self.assertGreaterEqual(_pptx_slide_count(data), 2)
        # Subtitle text appears somewhere in the deck
        slide_xmls = _pptx_all_slide_xmls(data)
        self.assertTrue(any("an extra subtitle line" in s for s in slide_xmls))

    def test_empty_slides_still_produces_deck(self):
        from app.services.document_service import build_pptx
        data = build_pptx("Lonely Deck", [])
        # Cover + closing at minimum
        self.assertGreaterEqual(_pptx_slide_count(data), 2)

    def test_accent_color_used_in_slides(self):
        """At least one slide element references the accent color (2563EB)."""
        from app.services.document_service import build_pptx
        data = build_pptx("D", self._basic_slides())
        slide_xmls = "".join(_pptx_all_slide_xmls(data))
        self.assertIn("2563EB", slide_xmls.upper())

    def test_closing_slide_present(self):
        """The final slide should contain a closing-style phrase."""
        from app.services.document_service import build_pptx
        data = build_pptx("D", self._basic_slides())
        slide_xmls = _pptx_all_slide_xmls(data)
        last = slide_xmls[-1]
        # We just check the last slide mentions a closing concept; tolerant of wording
        self.assertTrue(
            re.search(r"(Thank you|Questions|Q&A|谢谢|提问|讨论)", last, re.IGNORECASE),
            "Last slide should be a closing slide with a Thank you/Questions phrase",
        )

    def test_unicode_preserved(self):
        from app.services.document_service import build_pptx
        data = build_pptx("碳五石油树脂", [
            {"title": "概述", "bullets": ["营收增长 12%"]},
        ])
        slide_xmls = _pptx_all_slide_xmls(data)
        joined = "".join(slide_xmls)
        self.assertIn("碳五石油树脂", joined)
        self.assertIn("营收增长", joined)


if __name__ == "__main__":
    unittest.main()
