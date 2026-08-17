"""WeeklyDigestPptxBuilder — deck assembly tests.

Uses the ``_FakeDB`` pattern (same as ``test_forecast_unified_service``) so the
builder can be exercised without a live Postgres/schema. The builder only
reads attributes off the returned rows, so ``SimpleNamespace`` stand-ins are
sufficient.
"""
from __future__ import annotations

import io
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from pptx import Presentation

from app.services.weekly_digest.pptx_exporter import WeeklyDigestPptxBuilder


class _FakeQuery:
    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, *a):
        return self

    def order_by(self, *a):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, by_model=None):
        self._by_model = by_model or {}

    def query(self, model):
        name = getattr(model, "__name__", str(model))
        return _FakeQuery(self._by_model.get(name, []))


def _card(**overrides):
    base = dict(
        id="card-1",
        org_id="default-org",
        week_start=date(2026, 8, 10),
        week_end=date(2026, 8, 16),
        title_zh="周度总结及预测",
        title_en="Weekly Summary & Forecast",
        product_count=2,
        status="ready",
        error_msg=None,
        market_overview="本周市场整体平稳。",
        executive_summary="**总结**：价格窄幅震荡。",
        generated_at=datetime(2026, 8, 16, 8, 0),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _product(**overrides):
    base = dict(
        id="prod-1",
        report_id="card-1",
        product_key="naphtha",
        product_name_zh="石脑油",
        product_name_en="Naphtha",
        report_order=0,
        current_price=6500.0,
        change_pct_7d=1.25,
        price_12mo=[{"date": "2026-01-01", "price": 6400.0},
                    {"date": "2026-02-01", "price": 6500.0}],
        price_30d=[{"date": "2026-08-01", "price": 6450.0},
                   {"date": "2026-08-10", "price": 6500.0}],
        market_analysis="**供给**偏紧，价格上行。",
        ai_decision="建议逢低买入。",
        forecast_7d={"base": 6550.0, "bull": 6700.0, "bear": 6400.0, "confidence": 0.85},
        forecast_30d={"base": 6600.0, "bull": 6900.0, "bear": 6200.0, "confidence": 0.65},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _build(db) -> Presentation:
    buf = WeeklyDigestPptxBuilder(db).build("card-1")
    assert isinstance(buf, io.BytesIO)
    buf.seek(0)
    return Presentation(buf)


def _slide_texts(prs):
    out = []
    for slide in prs.slides:
        chunks = []
        for shape in slide.shapes:
            if shape.has_table:
                for row in shape.table.rows:
                    for cell in row.cells:
                        chunks.append(cell.text)
            elif shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    chunks.append("".join(r.text for r in para.runs))
        out.append(" ".join(chunks))
    return out


def test_cover_contains_bilingual_title():
    prs = _build(_FakeDB({"WeeklyReportCard": [_card()],
                          "WeeklyReportProduct": []}))
    texts = _slide_texts(prs)
    assert any("周度总结及预测" in t for t in texts)
    assert any("Weekly Summary & Forecast" in t for t in texts)


def test_market_overview_kpi_values_present():
    prs = _build(_FakeDB({
        "WeeklyReportCard": [_card()],
        "WeeklyReportProduct": [_product(), _product(id="prod-2",
                                                      report_order=1,
                                                      current_price=None,
                                                      change_pct_7d=-2.0)],
    }))
    texts = _slide_texts(prs)
    # 2 products, 1 priced, 1 up, 1 down
    overview = next(t for t in texts if "市场概况" in t)
    assert "2" in overview
    assert "1" in overview


def test_product_slide_contains_forecast_table_and_chart():
    prs = _build(_FakeDB({
        "WeeklyReportCard": [_card()],
        "WeeklyReportProduct": [_product()],
    }))
    # forecast values render on the product slide
    texts = _slide_texts(prs)
    assert any("石脑油" in t for t in texts)
    assert any("6,550" in t for t in texts)  # 7d base
    # a native chart shape exists on at least one slide
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    chart_count = sum(
        1
        for slide in prs.slides
        for shape in slide.shapes
        if shape.shape_type == MSO_SHAPE_TYPE.CHART
    )
    assert chart_count >= 2  # 12mo + 30d


def test_error_msg_emits_warning_slide():
    prs = _build(_FakeDB({
        "WeeklyReportCard": [_card(error_msg="数据源不可达")],
        "WeeklyReportProduct": [_product()],
    }))
    texts = _slide_texts(prs)
    assert any("数据源不可达" in t for t in texts)
    assert any("告警" in t for t in texts)


def test_empty_products_emits_no_data_slide():
    prs = _build(_FakeDB({"WeeklyReportCard": [_card()],
                          "WeeklyReportProduct": []}))
    texts = _slide_texts(prs)
    assert any("暂无产品数据" in t for t in texts)


def test_missing_card_raises_lookup_error():
    db = _FakeDB({"WeeklyReportCard": [], "WeeklyReportProduct": []})
    with pytest.raises(LookupError):
        WeeklyDigestPptxBuilder(db).build("card-1")


def test_executive_summary_strips_markdown_bold():
    prs = _build(_FakeDB({
        "WeeklyReportCard": [_card()],
        "WeeklyReportProduct": [_product()],
    }))
    texts = _slide_texts(prs)
    # "**总结**" must render as "总结" (markers stripped)
    assert any("总结" in t and "**" not in t for t in texts)
