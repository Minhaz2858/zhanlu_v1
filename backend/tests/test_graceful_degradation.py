"""Tests for graceful degradation — every HTML design failure must
fall back to the structured renderer without raising."""
import io
import pytest
from unittest.mock import patch
from pptx import Presentation


def _plan():
    from app.services.synexia.contracts import DeckPlan, SlidePlan
    return DeckPlan(
        title="T", deck_type="data_report", theme_recommendation="electric_studio",
        slides=[
            SlidePlan(layout="cover", title="T"),
            SlidePlan(layout="unknown_layout", title="Will be skipped"),
            SlidePlan(layout="closing", title="Q"),
        ],
    )


@pytest.fixture(autouse=True)
def skip_if_unavailable():
    from app.services.artifacts.render_html_deck import html_design_available
    if not html_design_available():
        pytest.skip("html_design not available (browser missing)")


class TestRenderHtmlDeckGracefulDegradation:
    def test_unknown_layout_skipped_not_crashed(self):
        from app.services.artifacts.render_html_deck import render_html_deck
        from app.services.artifacts.exporters._common import ExportContext
        ctx = ExportContext(source="t", user_message="")
        data = render_html_deck(_plan(), ctx)
        pres = Presentation(io.BytesIO(data))
        # 2 slides (cover + closing; unknown_layout skipped)
        assert len(pres.slides) == 2

    def test_no_slides_raises(self):
        from app.services.artifacts.render_html_deck import render_html_deck, RenderError
        from app.services.synexia.contracts import DeckPlan, SlidePlan
        from app.services.artifacts.exporters._common import ExportContext
        plan = DeckPlan(
            title="empty", deck_type="data_report", theme_recommendation="x",
            slides=[SlidePlan(layout="nonexistent_a", title="a"),
                    SlidePlan(layout="nonexistent_b", title="b")],
        )
        ctx = ExportContext(source="t", user_message="")
        with pytest.raises(RenderError, match="no slides"):
            render_html_deck(plan, ctx)

    def test_render_error_includes_root_cause(self):
        from app.services.artifacts.render_html_deck import render_html_deck, RenderError
        from app.services.synexia.contracts import DeckPlan, SlidePlan
        from app.services.artifacts.exporters._common import ExportContext
        plan = DeckPlan(
            title="T", deck_type="data_report", theme_recommendation="x",
            slides=[SlidePlan(layout="cover", title="T")],
        )
        ctx = ExportContext(source="t", user_message="")
        with patch(
            "app.services.artifacts.render_html_deck.render_image_fill",
            side_effect=Exception("soffice exploded"),
        ):
            with pytest.raises(RenderError, match="image_fill pipeline failed"):
                render_html_deck(plan, ctx)
