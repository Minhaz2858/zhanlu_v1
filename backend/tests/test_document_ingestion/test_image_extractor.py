"""Tests for the hardened image extractor (``_extract_image``).

The key contract: the image extractor ALWAYS emits an ``[Image attached:
<name>]`` marker so the LLM knows the file exists, even when OCR is disabled
or Tesseract is unavailable.
"""
from __future__ import annotations

from unittest.mock import patch

from app.config import settings
from app.services.document_ingestion.extractors import _extract_image


def test_extract_image_returns_marker_when_ocr_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "IMAGE_OCR_ENABLED", False)

    p = tmp_path / "chart.png"
    p.write_bytes(b"not-a-real-png")

    result = _extract_image(str(p))

    assert "[Image attached: chart.png]" in result
    assert "OCR disabled" in result


def test_extract_image_returns_marker_when_ocr_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "IMAGE_OCR_ENABLED", True)

    p = tmp_path / "chart.png"
    p.write_bytes(b"not-a-real-png")

    # Force ``import pytesseract`` inside _extract_image to raise ImportError,
    # simulating an environment where Tesseract is not installed.
    with patch.dict("sys.modules", {"pytesseract": None}):
        result = _extract_image(str(p))

    assert "[Image attached: chart.png]" in result
    assert "OCR unavailable" in result
