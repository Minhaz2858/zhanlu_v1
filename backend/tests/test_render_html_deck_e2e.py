"""End-to-end smoke test for the HTML design pipeline.

Renders a 3-slide fixture through the full render_html_deck path and
asserts the PPTX is valid + slide count is correct.
"""
import io
import os
import subprocess
import tempfile
from pathlib import Path
import pytest
from pptx import Presentation


def _fixture_plan():
    from app.services.synexia.contracts import DeckPlan, SlidePlan, KPISpecInSlide, ChartSpecInSlide
    return DeckPlan(
        title="Q3 Sales Recap",
        deck_type="data_report",
        theme_recommendation="electric_studio",
        slides=[
            SlidePlan(layout="cover", title="Q3 Sales Recap", subtitle="Up 8% QoQ"),
            SlidePlan(layout="kpi_grid", title="Key Metrics", kpi_specs=[
                KPISpecInSlide(label="Revenue", value="$2.4M", delta="+12%"),
                KPISpecInSlide(label="Orders", value="1,847", delta="+5%"),
            ]),
            SlidePlan(layout="chart_with_bullets", title="Revenue Trend",
                      bullets=["Q3 revenue up 12% QoQ", "Pipeline strong"],
                      chart_spec=ChartSpecInSlide(chart_type="bar"),
                      chart_rows=[
                          {"label": "Q1", "value": 1.8},
                          {"label": "Q2", "value": 2.1},
                          {"label": "Q3", "value": 2.4},
                      ]),
        ],
    )


def _fixture_ctx():
    from app.services.artifacts.exporters._common import ExportContext
    return ExportContext(source="test", user_message="quarterly recap")


@pytest.fixture
def skip_if_unavailable():
    from app.services.artifacts.render_html_deck import html_design_available
    if not html_design_available():
        pytest.skip("browser missing — install firefox or chromium")


class TestE2E:
    def test_full_pipeline_three_slides(self, skip_if_unavailable):
        from app.services.artifacts.render_html_deck import render_html_deck
        data = render_html_deck(_fixture_plan(), _fixture_ctx())
        assert data[:4] == b"PK\x03\x04"
        pres = Presentation(io.BytesIO(data))
        assert len(pres.slides) == 3
        # 16:9
        ratio = pres.slide_width / pres.slide_height
        assert 1.7 < ratio < 1.8

    def test_thumbnail_creation_works(self, skip_if_unavailable):
        """Round-trip the produced PPTX back through soffice to PNG
        (verifies it can be opened by Office tools)."""
        from app.services.artifacts.render_html_deck import render_html_deck
        data = render_html_deck(_fixture_plan(), _fixture_ctx())
        with tempfile.TemporaryDirectory() as td:
            workdir = Path(td)
            pptx_path = workdir / "out.pptx"
            pptx_path.write_bytes(data)
            proc = subprocess.run([
                "soffice", "--headless", "--convert-to", "pdf",
                "--outdir", str(workdir), str(pptx_path),
            ], capture_output=True, text=True, timeout=60,
               env={**os.environ, "HOME": str(workdir)})
            assert proc.returncode == 0, f"soffice failed: {proc.stderr[:500]}"
            pdf_path = workdir / "out.pdf"
            assert pdf_path.exists()
            proc2 = subprocess.run([
                "pdftoppm", "-png", "-r", "96",
                str(pdf_path), str(workdir / "page"),
            ], capture_output=True, text=True, timeout=30)
            assert proc2.returncode == 0
            pngs = list(workdir.glob("page-*.png"))
            assert len(pngs) == 3
