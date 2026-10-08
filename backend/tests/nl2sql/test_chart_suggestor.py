"""Chart suggestor — OCI visualization stage (P2-7) unit tests.

Pins: deterministic shape grammar (line / bar / stacked_bar / table), no
exceptions on odd data, None on empty input, and the suggestion never copies
row payloads.
"""

from __future__ import annotations

from app.services.db.chart_suggestor import suggest_chart


def test_time_plus_measure_is_line():
    rows = [
        {"month": "2026-08", "revenue": 100},
        {"month": "2026-09", "revenue": 150},
    ]
    viz = suggest_chart(rows)
    assert viz["chart_type"] == "line"
    assert viz["x"] == "month"
    assert viz["y"] == ["revenue"]


def test_explicit_columns_override_inference():
    rows = [{"a": 1, "b": 2}]
    viz = suggest_chart(rows, columns=["a", "b"])
    assert viz is not None


def test_category_plus_measure_is_bar():
    rows = [
        {"region": "north", "sales": 10},
        {"region": "south", "sales": 20},
    ]
    viz = suggest_chart(rows)
    assert viz["chart_type"] == "bar"
    assert viz["x"] == "region"
    assert viz["y"] == ["sales"]


def test_two_dimensions_one_measure_is_stacked_bar():
    rows = [
        {"region": "north", "product": "A", "sales": 10},
        {"region": "north", "product": "B", "sales": 5},
        {"region": "south", "product": "A", "sales": 7},
    ]
    viz = suggest_chart(rows)
    assert viz["chart_type"] == "stacked_bar"
    assert viz["x"] == "region"
    assert viz["series"] == "product"
    assert viz["y"] == ["sales"]


def test_no_numeric_measure_is_table():
    rows = [{"name": "x"}, {"name": "y"}]
    viz = suggest_chart(rows)
    assert viz["chart_type"] == "table"


def test_too_many_measures_degrades_to_table():
    rows = [
        {
            "region": "north",
            "m1": 1,
            "m2": 2,
            "m3": 3,
            "m4": 4,
            "m5": 5,
            "m6": 6,
            "m7": 7,
        }
    ]
    viz = suggest_chart(rows)
    assert viz["chart_type"] == "table"


def test_empty_and_malformed_inputs_return_none():
    assert suggest_chart([], ["a"]) is None
    assert suggest_chart([], []) is None
    assert suggest_chart(["not-a-dict"], ["a"]) is None
    assert suggest_chart(None, None) is None


def test_numeric_strings_are_recognized():
    rows = [
        {"region": "north", "sales": "1,234.5"},
        {"region": "south", "sales": "10"},
    ]
    viz = suggest_chart(rows)
    assert viz["chart_type"] == "bar"
    assert viz["y"] == ["sales"]


def test_none_values_abstain_from_voting():
    rows = [
        {"month": None, "revenue": None},
        {"month": "2026-09-01", "revenue": 5},
    ]
    viz = suggest_chart(rows)
    assert viz["chart_type"] == "line"


def test_suggestion_never_copies_rows():
    rows = [{"month": "2026-08", "revenue": 100}] * 50
    viz = suggest_chart(rows)
    assert "rows" not in viz
    assert viz["row_count"] == 50
