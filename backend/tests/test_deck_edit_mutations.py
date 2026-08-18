"""Tests for PHASE 2 — deterministic per-slide DeckPlan mutation functions.

Covers the pure-function edit layer in ``app/services/artifacts/editors/deck_edit.py``:

* edit_slide   — whitelist patch of title/subtitle/bullets/notes/kpi_specs
* add_slide    — insert a new SlidePlan (validates layout; default before closing)
* restyle_deck — update theme/headline_style/summary/methodology
* update_chart — update a slide's chart_spec
* remove_slide — deletion; refuses index 0 (cover) and last index (closing)
* reorder_slide — move slides; cover stays 0 / closing stays last

Every function is PURE: it returns a NEW DeckPlan and never mutates the input.
"""

from __future__ import annotations

import pytest


def _sample_plan():
    from app.services.synexia.contracts import (
        ChartSpecInSlide,
        DeckPlan,
        KPISpecInSlide,
        SlidePlan,
    )

    return DeckPlan(
        title="Q3 Sales Review",
        deck_type="data_report",
        theme_recommendation="zhanlu-blue",
        headline_style="topic",
        summary="Quarterly sales summary",
        methodology="Data from ERP",
        slides=[
            SlidePlan(layout="cover", title="Q3 Sales Review", subtitle="Board deck"),
            SlidePlan(
                layout="chart_with_bullets",
                title="Revenue Trend",
                bullets=["Up 8%", "Strong APAC"],
                chart_spec=ChartSpecInSlide(
                    chart_type="bar", x_key="month", y_keys=["revenue"], title="Revenue"
                ),
                kpi_specs=[KPISpecInSlide(label="Revenue", value="1.2M", delta="+8%")],
            ),
            SlidePlan(layout="insights_bullets", title="Key Insights", bullets=["A", "B"]),
            SlidePlan(layout="closing", title="Thank You", subtitle="Q&A"),
        ],
    )


class TestEditSlide:
    def test_patches_title_and_subtitle(self):
        from app.services.artifacts.editors.deck_edit import edit_slide

        plan = _sample_plan()
        out = edit_slide(plan, 1, {"title": "New Revenue Trend", "subtitle": "Edited"})
        assert out.slides[1].title == "New Revenue Trend"
        assert out.slides[1].subtitle == "Edited"
        # Input unchanged (purity)
        assert plan.slides[1].title == "Revenue Trend"
        # Other slides untouched
        assert out.slides[0].layout == "cover"
        assert out.slides[3].layout == "closing"

    def test_patches_bullets(self):
        from app.services.artifacts.editors.deck_edit import edit_slide

        out = edit_slide(_sample_plan(), 2, {"bullets": ["X", "Y", "Z"]})
        assert out.slides[2].bullets == ["X", "Y", "Z"]

    def test_patches_notes(self):
        from app.services.artifacts.editors.deck_edit import edit_slide

        out = edit_slide(_sample_plan(), 1, {"notes": "Speaker note here"})
        assert out.slides[1].notes == "Speaker note here"

    def test_patches_kpi_specs(self):
        from app.services.artifacts.editors.deck_edit import edit_slide
        from app.services.synexia.contracts import KPISpecInSlide

        out = edit_slide(
            _sample_plan(),
            1,
            {"kpi_specs": [{"label": "New KPI", "value": "99", "delta": "-1%"}]},
        )
        assert len(out.slides[1].kpi_specs) == 1
        assert isinstance(out.slides[1].kpi_specs[0], KPISpecInSlide)
        assert out.slides[1].kpi_specs[0].label == "New KPI"

    def test_rejects_unknown_field(self):
        from app.services.artifacts.editors.deck_edit import DeckEditError, edit_slide

        with pytest.raises(DeckEditError):
            edit_slide(_sample_plan(), 1, {"layout": "cover"})  # layout is not editable

    def test_rejects_out_of_range_index(self):
        from app.services.artifacts.editors.deck_edit import DeckEditError, edit_slide

        with pytest.raises(DeckEditError):
            edit_slide(_sample_plan(), 99, {"title": "x"})
        with pytest.raises(DeckEditError):
            edit_slide(_sample_plan(), -1, {"title": "x"})


class TestAddSlide:
    def test_inserts_at_index(self):
        from app.services.artifacts.editors.deck_edit import add_slide

        out = add_slide(
            _sample_plan(),
            2,
            {"layout": "data_table", "title": "Region Table", "bullets": ["r1"]},
        )
        assert len(out.slides) == 5
        assert out.slides[2].layout == "data_table"
        assert out.slides[2].title == "Region Table"
        assert out.slides[3].title == "Key Insights"  # shifted down

    def test_default_inserts_before_closing(self):
        from app.services.artifacts.editors.deck_edit import add_slide

        out = add_slide(_sample_plan(), None, {"layout": "agenda", "title": "Agenda"})
        # closing stays last
        assert out.slides[-1].layout == "closing"
        assert out.slides[-2].layout == "agenda"
        assert len(out.slides) == 5

    def test_rejects_invalid_layout(self):
        from app.services.artifacts.editors.deck_edit import DeckEditError, add_slide

        with pytest.raises(DeckEditError):
            add_slide(_sample_plan(), 1, {"layout": "not_a_layout", "title": "X"})

    def test_purity_input_unchanged(self):
        from app.services.artifacts.editors.deck_edit import add_slide

        plan = _sample_plan()
        add_slide(plan, 1, {"layout": "agenda", "title": "Agenda"})
        assert len(plan.slides) == 4  # input not mutated


class TestRestyleDeck:
    def test_updates_theme_and_style(self):
        from app.services.artifacts.editors.deck_edit import restyle_deck

        out = restyle_deck(
            _sample_plan(),
            {"theme_recommendation": "sunset", "headline_style": "assertion"},
        )
        # "sunset" is an alias → resolved to the canonical file name.
        assert out.theme_recommendation == "sunset-orange"
        assert out.headline_style == "assertion"
        assert out.summary == "Quarterly sales summary"  # untouched

    def test_updates_summary_and_methodology(self):
        from app.services.artifacts.editors.deck_edit import restyle_deck

        out = restyle_deck(
            _sample_plan(),
            {"summary": "New summary", "methodology": "New method"},
        )
        assert out.summary == "New summary"
        assert out.methodology == "New method"

    def test_rejects_unknown_field(self):
        from app.services.artifacts.editors.deck_edit import DeckEditError, restyle_deck

        with pytest.raises(DeckEditError):
            restyle_deck(_sample_plan(), {"title": "cannot-change-title"})

    def test_purity(self):
        from app.services.artifacts.editors.deck_edit import restyle_deck

        plan = _sample_plan()
        restyle_deck(plan, {"theme_recommendation": "sunset"})
        assert plan.theme_recommendation == "zhanlu-blue"

    def test_resolves_theme_alias(self):
        from app.services.artifacts.editors.deck_edit import restyle_deck

        # Aliases (e.g. "dark", "navy") must resolve to a canonical file name.
        out = restyle_deck(_sample_plan(), {"theme_recommendation": "navy"})
        assert out.theme_recommendation == "midnight-navy"

    def test_rejects_unknown_theme_with_available_list(self):
        from app.services.artifacts.editors.deck_edit import (
            DeckEditError,
            restyle_deck,
        )

        with pytest.raises(DeckEditError) as exc:
            restyle_deck(_sample_plan(), {"theme_recommendation": "not-a-theme"})
        msg = str(exc.value)
        assert "not-a-theme" in msg
        assert "zhanlu-blue" in msg  # lists available themes


class TestUpdateChart:
    def test_updates_chart_type_and_keys(self):
        from app.services.artifacts.editors.deck_edit import update_chart

        out = update_chart(
            _sample_plan(),
            1,
            {"chart_type": "line", "y_keys": ["revenue", "cost"], "title": "New chart title"},
        )
        chart = out.slides[1].chart_spec
        assert chart.chart_type == "line"
        assert chart.y_keys == ["revenue", "cost"]
        assert chart.title == "New chart title"
        assert chart.x_key == "month"  # untouched

    def test_slide_without_chart_gets_one(self):
        from app.services.artifacts.editors.deck_edit import update_chart

        out = update_chart(_sample_plan(), 2, {"chart_type": "pie", "x_key": "region"})
        assert out.slides[2].chart_spec is not None
        assert out.slides[2].chart_spec.chart_type == "pie"

    def test_rejects_unknown_chart_field(self):
        from app.services.artifacts.editors.deck_edit import DeckEditError, update_chart

        with pytest.raises(DeckEditError):
            update_chart(_sample_plan(), 1, {"data": [{"a": 1}]})


class TestRemoveSlide:
    def test_removes_middle_slide(self):
        from app.services.artifacts.editors.deck_edit import remove_slide

        out = remove_slide(_sample_plan(), 1)
        assert len(out.slides) == 3
        assert [s.title for s in out.slides] == [
            "Q3 Sales Review", "Key Insights", "Thank You",
        ]

    def test_refuses_cover_removal(self):
        from app.services.artifacts.editors.deck_edit import DeckEditError, remove_slide

        with pytest.raises(DeckEditError, match="cover"):
            remove_slide(_sample_plan(), 0)

    def test_refuses_closing_removal(self):
        from app.services.artifacts.editors.deck_edit import DeckEditError, remove_slide

        with pytest.raises(DeckEditError, match="closing"):
            remove_slide(_sample_plan(), 3)


class TestReorderSlide:
    def test_moves_slide_forward(self):
        from app.services.artifacts.editors.deck_edit import reorder_slide

        out = reorder_slide(_sample_plan(), 2, 1)  # move insights to position 1
        assert [s.title for s in out.slides] == [
            "Q3 Sales Review", "Key Insights", "Revenue Trend", "Thank You",
        ]

    def test_moves_slide_backward(self):
        from app.services.artifacts.editors.deck_edit import reorder_slide

        out = reorder_slide(_sample_plan(), 1, 2)
        assert [s.title for s in out.slides] == [
            "Q3 Sales Review", "Key Insights", "Revenue Trend", "Thank You",
        ]

    def test_cover_stays_first(self):
        from app.services.artifacts.editors.deck_edit import DeckEditError, reorder_slide

        with pytest.raises(DeckEditError):
            reorder_slide(_sample_plan(), 0, 1)
        with pytest.raises(DeckEditError):
            reorder_slide(_sample_plan(), 1, 0)

    def test_closing_stays_last(self):
        from app.services.artifacts.editors.deck_edit import DeckEditError, reorder_slide

        with pytest.raises(DeckEditError):
            reorder_slide(_sample_plan(), 3, 1)
        with pytest.raises(DeckEditError):
            reorder_slide(_sample_plan(), 1, 3)
