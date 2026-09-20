"""The artifact format a turn delivers must be the format the user asked for.

Measured live on 2026-09-20, request 惠州伊斯科本周销售情况，用图表展示:

  task_spec.artifact_intents = [sales_chart, sales_report, data_visualization]
  plan node  sandbox / output_artifact_type = "dataframe"
  artifact   report.xlsx   (sheet1 both columns: 'note' | 'See generated
                            report for details'; the paired html PREVIEW was a
                            Plotly template with no series)
  answer     claimed the chart and KPI had been generated from the query

Three things were wrong at once and only one of them was the format: the prose
looked right, so the artifact had to be read to find it. These pin the format
half — that a SHAPE word on the step cannot outrank the delivery intent for the
deliverable the user receives.
"""

from __future__ import annotations

import pytest

from app.services.synexia.capability_router import resolve_deliverable_format
from app.services.tool_handlers.sandbox_tool import (
    _IMAGE_BY_FORMAT,
    normalize_format,
)


# ── normalize_format: one mapping, used by the sandbox AND the planner ──────

@pytest.mark.parametrize(
    "named,expected",
    [
        # Real generator formats pass through untouched.
        ("xlsx", "xlsx"), ("pptx", "pptx"), ("html", "html"),
        ("pdf", "pdf"), ("docx", "docx"), ("md", "md"),
        ("HTML", "html"), (" xlsx ", "xlsx"),
        # Explicit user/planner synonyms.
        ("chart", "html"), ("dashboard", "html"), ("visualization", "html"),
        ("table", "xlsx"), ("dataframe", "xlsx"), ("deck", "pptx"),
        # Shape words the planner invents — each one used to hard-fail the step.
        ("metrics_table", "xlsx"), ("aggregated_table", "xlsx"),
        ("sales_chart", "html"), ("data_visualization", "html"),
        ("kpi_summary_table", "xlsx"),
        # Nothing recognisable stays as-is; the caller decides if that is fatal.
        ("", ""), ("quux", "quux"),
    ],
)
def test_normalize_format_maps_names_to_generator_formats(named, expected):
    assert normalize_format(named) == expected


def test_every_normalized_result_is_a_real_generator_format():
    """The mapping can only ever land on a format the sandbox can generate."""
    for named in ("chart", "dashboard", "table", "dataframe", "metrics_table",
                  "sales_chart", "aggregated_table", "ppt", "word", "markdown"):
        assert normalize_format(named) in _IMAGE_BY_FORMAT


# ── the reconciliation rule ────────────────────────────────────────────────

CHART_INTENTS = ["sales_chart", "sales_report", "data_visualization"]


def test_the_delivery_intent_overrides_a_shape_word_on_the_terminal_step():
    """The live defect, at the decision level."""
    fmt, overrode = resolve_deliverable_format(
        "dataframe", is_terminal=True, intents=CHART_INTENTS,
    )
    assert fmt == "html", fmt
    assert overrode == "sales_chart"


def test_an_intermediate_step_keeps_its_own_type():
    """A dataframe feeding a later chart step is legitimate, not a mistake."""
    fmt, overrode = resolve_deliverable_format(
        "dataframe", is_terminal=False, intents=CHART_INTENTS,
    )
    assert fmt == "xlsx", fmt
    assert overrode is None


def test_an_explicit_step_type_the_intent_agrees_with_is_untouched():
    fmt, overrode = resolve_deliverable_format(
        "html", is_terminal=True, intents=CHART_INTENTS,
    )
    assert fmt == "html", fmt
    assert overrode is None


def test_no_intents_means_no_override():
    """A data-only question has no delivery contract to honour."""
    fmt, overrode = resolve_deliverable_format(
        "dataframe", is_terminal=True, intents=[],
    )
    assert fmt == "xlsx", fmt
    assert overrode is None
    assert resolve_deliverable_format(
        "dataframe", is_terminal=True, intents=None,
    ) == ("xlsx", None)


def test_an_unusable_intent_does_not_clobber_a_valid_step_type():
    """An intent that is not a generator format must not replace a real one."""
    fmt, overrode = resolve_deliverable_format(
        "pptx", is_terminal=True, intents=["quux"],
    )
    assert fmt == "pptx", fmt
    assert overrode is None


def test_a_missing_step_type_falls_back_to_docx():
    """The pre-existing default (sandbox_tool's ``or "docx"``) is preserved."""
    assert resolve_deliverable_format(
        None, is_terminal=True, intents=[],
    ) == ("docx", None)


def test_the_intent_also_wins_for_a_spreadsheet_ask():
    """The rule is the delivery contract, not a chart special case."""
    fmt, overrode = resolve_deliverable_format(
        "chart", is_terminal=True, intents=["xlsx"],
    )
    assert fmt == "xlsx", fmt
    assert overrode == "xlsx"
