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


# ── Redesign assertions ────────────────────────────────────────────────────

def _run_ea_typefaces(prs):
    """Collect every East Asian typeface written via ``<a:ea>``."""
    from pptx.oxml.ns import qn
    found = set()
    for slide in prs.slides:
        for shape in slide.shapes:
            if not shape.has_text_frame:
                continue
            for para in shape.text_frame.paragraphs:
                for run in para.runs:
                    # <a:ea> sits inside <a:rPr>, so search descendants.
                    for ea in run._r.findall(".//" + qn("a:ea")):
                        found.add(ea.get("typeface"))
    return found


def test_cjk_typeface_applied_to_runs():
    prs = _build(_FakeDB({
        "WeeklyReportCard": [_card()],
        "WeeklyReportProduct": [_product()],
    }))
    typefaces = _run_ea_typefaces(prs)
    assert "Microsoft YaHei" in typefaces


def test_markdown_bullets_render_structured():
    prs = _build(_FakeDB({
        "WeeklyReportCard": [_card()],
        "WeeklyReportProduct": [_product(
            market_analysis="**供给**偏紧。\n- 开工率下降\n- 库存低位\n需求回暖。",
        )],
    }))
    texts = _slide_texts(prs)
    # bullets become "•", bold markers stripped, content preserved
    assert any("• 开工率下降" in t for t in texts)
    assert any("• 库存低位" in t for t in texts)
    assert all("**" not in t for t in texts)


def test_analysis_text_frame_has_autofit():
    """Every markdown text frame on the product slide has normAutofit so
    PowerPoint auto-shrinks text that exceeds the box height."""
    body = "供给端偏紧叠加下游补库需求，价格重心逐步上移，短期支撑较强。" * 4
    prs = _build(_FakeDB({
        "WeeklyReportCard": [_card()],
        "WeeklyReportProduct": [_product(market_analysis=body)],
    }))
    from pptx.oxml.ns import qn
    found_autofit = 0
    for slide in prs.slides:
        for shape in slide.shapes:
            if not shape.has_text_frame:
                continue
            bodyPr = shape.text_frame._txBody.find(qn("a:bodyPr"))
            if bodyPr is not None and bodyPr.find(qn("a:normAutofit")) is not None:
                found_autofit += 1
    assert found_autofit >= 1, "no normAutofit found on any text frame"
    # a substantial prefix survives (no aggressive [:N] hard cut)
    texts = _slide_texts(prs)
    assert any(body[:40] in t for t in texts)


def test_cjk_height_estimate_accurate():
    """CJK text height estimate is meaningfully larger than the old Latin
    assumption (so overflow no longer goes undetected)."""
    import math
    body = "中文测试段落" * 20  # 120 CJK chars
    builder = WeeklyDigestPptxBuilder(_FakeDB({
        "WeeklyReportCard": [_card()],
        "WeeklyReportProduct": [_product(market_analysis=body)],
    }))
    est = builder._est_text_height(body, size=11, width_in=5.0)
    # At 11pt, 5in wide, CJK chars are ~1.0em → ~120*11pt / (5*72/11/1.0) lines.
    assert est >= 0.6, f"CJK estimate too low: {est:.2f}in (expected >= 0.6)"
    assert est <= 2.0, f"CJK estimate too high: {est:.2f}in (expected <= 2.0)"
    # Old estimate (0.52× Latin assumption) would yield ~0.5in — verify we're
    # significantly above it so the overflow bug is actually fixed.
    old_est = (math.ceil(120 / 62) * 11 * 1.35) / 72.0  # chars_per_line=62
    assert est > old_est * 1.5, f"CJK estimate not improved: {est:.2f} vs old {old_est:.2f}"


def test_very_long_cjk_analysis_has_autofit():
    """A 600-char CJK analysis must render inside a text frame with normAutofit
    so PowerPoint can shrink it rather than leaking it onto other sections."""
    body = "供给端偏紧叠加下游补库需求，价格重心逐步上移，短期支撑较强。" * 20
    prs = _build(_FakeDB({
        "WeeklyReportCard": [_card()],
        "WeeklyReportProduct": [_product(market_analysis=body)],
    }))
    from pptx.oxml.ns import qn
    found_autofit = 0
    for slide in prs.slides:
        for shape in slide.shapes:
            if not shape.has_text_frame:
                continue
            bodyPr = shape.text_frame._txBody.find(qn("a:bodyPr"))
            if bodyPr is not None and bodyPr.find(qn("a:normAutofit")) is not None:
                found_autofit += 1
    assert found_autofit >= 1, "no normAutofit found on any text frame"


def test_kpi_change_chip_red_for_up():
    from pptx.dml.color import RGBColor
    prs = _build(_FakeDB({
        "WeeklyReportCard": [_card()],
        "WeeklyReportProduct": [_product(change_pct_7d=1.25)],
    }))
    red = RGBColor(0xDC, 0x26, 0x26)
    green = RGBColor(0x05, 0x96, 0x69)
    found_up_red = False
    for slide in prs.slides:
        for shape in slide.shapes:
            if not shape.has_text_frame:
                continue
            for para in shape.text_frame.paragraphs:
                for run in para.runs:
                    if run.text == "+1.25%" and run.font.color and run.font.color.rgb == red:
                        found_up_red = True
    assert found_up_red, "涨 must render red (Chinese convention)"


def test_kpi_change_chip_green_for_down():
    from pptx.dml.color import RGBColor
    prs = _build(_FakeDB({
        "WeeklyReportCard": [_card()],
        "WeeklyReportProduct": [_product(change_pct_7d=-2.0)],
    }))
    green = RGBColor(0x05, 0x96, 0x69)
    found_down_green = False
    for slide in prs.slides:
        for shape in slide.shapes:
            if not shape.has_text_frame:
                continue
            for para in shape.text_frame.paragraphs:
                for run in para.runs:
                    if run.text == "-2.00%" and run.font.color and run.font.color.rgb == green:
                        found_down_green = True
    assert found_down_green, "跌 must render green (Chinese convention)"


def test_deck_chrome_slides_present():
    prs = _build(_FakeDB({
        "WeeklyReportCard": [_card()],
        "WeeklyReportProduct": [_product()],
    }))
    texts = _slide_texts(prs)
    assert any("目录" in t for t in texts)          # agenda
    assert any("产品明细" in t for t in texts)      # section divider
    assert any("免责声明" in t for t in texts)      # disclaimer
    assert any("本报告由 AI 自动生成" in t for t in texts)


def _product_slide(prs):
    """Return the product slide (the one carrying the KPI chip label 现价)."""
    for slide in prs.slides:
        texts = []
        for sh in slide.shapes:
            if sh.has_text_frame:
                texts.append("".join(r.text for p in sh.text_frame.paragraphs
                                     for r in p.runs))
        joined = " ".join(texts)
        # The Agenda slide also mentions "石脑油"; the product slide is the one
        # that additionally carries the KPI chip label "现价".
        if "石脑油" in joined and "现价" in joined:
            return slide
    return None


def test_product_slide_has_table():
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    prs = _build(_FakeDB({
        "WeeklyReportCard": [_card()],
        "WeeklyReportProduct": [_product()],
    }))
    target = _product_slide(prs)
    assert target is not None
    assert any(sh.has_table for sh in target.shapes)
    rects = [sh for sh in target.shapes
             if sh.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE]
    # Only the KPI chips (plus the title accent) remain — range bars removed.
    assert len(rects) >= 3


def test_analysis_column_no_range_bars():
    """The 7天/30天 scenario range bars are gone from the product slide."""
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    from pptx.util import Inches
    prs = _build(_FakeDB({
        "WeeklyReportCard": [_card()],
        "WeeklyReportProduct": [_product()],
    }))
    target = _product_slide(prs)
    assert target is not None
    thin_wide = []
    for sh in target.shapes:
        if sh.shape_type != MSO_SHAPE_TYPE.AUTO_SHAPE:
            continue
        if sh.width is None or sh.height is None:
            continue
        # Range-bar tracks/segments were ~0.14" tall and up to ~4.15" wide;
        # KPI chips (0.85" tall) and the title accent (1.2" wide) never match.
        if sh.height <= Inches(0.2) and sh.width >= Inches(1.5):
            thin_wide.append(sh)
    assert len(thin_wide) == 0, "range-bar thin/wide rects should be removed"


def test_long_ai_decision_text_preserved():
    """Full AI 决策 text survives (no truncation) — font auto-shrinks instead."""
    long_decision = (
        "销售方以9900元/吨现价优先出货获利，采购方暂缓追高，等待7日预测基准8917元/吨"
        "或看跌下限8075元/吨附近分批采购。依据：当前价格9900元/吨，7日预测基准8917元/吨"
        "较现价明显回落，看跌下限8075元/吨。关键风险：原油价格反弹可能推高成本。"
    )
    prs = _build(_FakeDB({
        "WeeklyReportCard": [_card()],
        "WeeklyReportProduct": [_product(ai_decision=long_decision)],
    }))
    target = _product_slide(prs)
    assert target is not None
    # The tail marker "关键风险" must be present in the slide (no clipping) and
    # the text must NOT carry a "…" hard-truncation marker.
    decision_text = ""
    for sh in target.shapes:
        if not sh.has_text_frame:
            continue
        txt = "".join(r.text for p in sh.text_frame.paragraphs for r in p.runs)
        if "销售方" in txt:
            decision_text = txt
            break
    assert decision_text, "AI 决策 body not found"
    assert "关键风险" in decision_text, "AI 决策 tail text was truncated/clipped"
    assert "…" not in decision_text, "AI 决策 was hard-truncated with ellipsis"


def test_fit_text_size_never_truncates():
    """_fit_text_size returns the full text and shrinks the font to fit."""
    builder = WeeklyDigestPptxBuilder(_FakeDB({}))
    short = "建议逢低买入。"
    text, size = builder._fit_text_size(short, 5.0, 1.0, start=11, floor=7)
    assert text == short and size == 11  # fits → no shrink

    long_text = "销售方优先出货。" * 60  # ~480 chars
    text, size = builder._fit_text_size(long_text, 5.0, 1.7, start=11, floor=7)
    assert text == long_text, "full text must be preserved (no truncation)"
    assert "…" not in text
    assert size < 11, "font must shrink to fit the long text"


def test_chart_value_axis_fitted_and_formatted():
    prs = _build(_FakeDB({
        "WeeklyReportCard": [_card()],
        "WeeklyReportProduct": [_product()],
    }))
    checked = 0
    for slide in prs.slides:
        for shape in slide.shapes:
            if not shape.has_chart:
                continue
            va = shape.chart.value_axis
            assert va.tick_labels.number_format == "#,##0"
            assert va.minimum_scale is not None and va.maximum_scale is not None
            # fitted to the data (30d data spans ~6450..6500)
            assert va.minimum_scale <= 6450
            assert va.maximum_scale >= 6500
            checked += 1
    assert checked >= 2  # 30d + 12mo charts
