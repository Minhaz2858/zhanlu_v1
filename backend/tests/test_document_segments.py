"""P4 T2 — structured document extraction with location provenance.

`extract_segments(file_path, file_type)` returns [{text, location}] where
location is {"page": n} / {"sheet": name} / {"section": heading} /
{"row": n}. The existing `extract_text` behavior is unchanged.
"""

from __future__ import annotations

from app.services.document_ingestion import extractors


def _segments(extractor_result):
    assert isinstance(extractor_result, list)
    assert all(isinstance(s, dict) and "text" in s and "location" in s
               for s in extractor_result)
    assert all(s["text"].strip() for s in extractor_result)
    return extractor_result


def test_pdf_segments_carry_page_numbers(tmp_path):
    from pypdf import PdfWriter

    p = tmp_path / "two_pages.pdf"
    w = PdfWriter()
    for text in ("first page content", "second page content"):
        import io
        from reportlab.pdfgen import canvas

        buf = io.BytesIO()
        c = canvas.Canvas(buf)
        c.drawString(100, 700, text)
        c.save()
        w.append(io.BytesIO(buf.getvalue()))
    with open(p, "wb") as f:
        w.write(f)

    segs = _segments(extractors.extract_segments(str(p), "pdf"))
    pages = [s["location"]["page"] for s in segs]
    assert pages == sorted(pages) and len(set(pages)) >= 1
    assert any("first page" in s["text"] for s in segs)


def test_xlsx_segments_carry_sheet_names(tmp_path):
    from openpyxl import Workbook

    p = tmp_path / "two_sheets.xlsx"
    wb = Workbook()
    wb.remove(wb.active)
    ws1 = wb.create_sheet("Customers")
    ws1.append(["customer definition here"])
    ws2 = wb.create_sheet("Pricing Policy")
    ws2.append(["discount rules here"])
    wb.save(p)

    segs = _segments(extractors.extract_segments(str(p), "xlsx"))
    sheets = {s["location"]["sheet"] for s in segs}
    assert sheets == {"Customers", "Pricing Policy"}


def test_csv_segments_carry_row_numbers(tmp_path):
    p = tmp_path / "rows.csv"
    p.write_text("name,city\nAcme,Munich\nBravo,Leeds\n", encoding="utf-8")

    segs = _segments(extractors.extract_segments(str(p), "csv"))
    assert segs[0]["location"].get("row") == 1  # header row
    assert any("Acme" in s["text"] for s in segs)
    rows = [s["location"]["row"] for s in segs]
    assert rows == sorted(rows)


def test_txt_segments_are_single_unlocated(tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("plain text\nno locations\n", encoding="utf-8")

    segs = _segments(extractors.extract_segments(str(p), "txt"))
    assert len(segs) == 1
    assert segs[0]["location"] == {}


def test_extract_text_behavior_unchanged(tmp_path):
    """The existing RAG text path is byte-identical for a txt file."""
    p = tmp_path / "notes.txt"
    p.write_text("plain text\nno locations\n", encoding="utf-8")
    assert extractors.extract_text(str(p), "txt") == "plain text\nno locations\n"
