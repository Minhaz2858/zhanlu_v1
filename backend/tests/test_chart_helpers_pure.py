"""Unit tests for ``app.services.artifacts.exporters._chart_helpers``.

The helpers were extracted from ``_common.py`` on 2026-08-19 so the sandbox
container can vendor them alongside ``layout_engine.py`` without pulling in
the heavy ``from app.services.synexia.contracts import …`` that lives at the
top of ``_common.py``. This file pins down the helpers' behaviour so the
extraction can't silently change semantics for either the sandbox or the
in-process renderers.
"""

from __future__ import annotations

import pytest

from app.services.artifacts.exporters import _chart_helpers
from app.services.artifacts.exporters._chart_helpers import (
    CHART_CATEGORY_CAP,
    OTHER_BUCKET_LABEL,
    cap_chart_categories,
    coerce_number,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_constants_have_canonical_values():
    """The cap + label are the sandbox-portable defaults. Other code in the
    deck pipeline keys off these (charts and tables must agree on the cap)."""
    assert CHART_CATEGORY_CAP == 8
    assert OTHER_BUCKET_LABEL == "Other"


# ---------------------------------------------------------------------------
# coerce_number
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw, expected", [
    # ints / floats / bools
    (0,         0.0),
    (1,         1.0),
    (-3,        -3.0),
    (1.5,       1.5),
    (True,      1.0),     # bool subclasses int
    (False,     0.0),
    # None and unparseable
    (None,      None),
    ("",        None),
    ("   ",     None),
    ("abc",     None),
    ({},        None),    # wrong type → None
    # Common formatting the LLM emits
    ("1,234.5", 1234.5),  # thousands sep
    ("1,234",   1234.0),
    ("-1,234",  -1234.0),
    ("100%",    100.0),   # trailing unit
    ("189.3M CNY", 189.3),
    ("$1,000",  1000.0),  # leading currency
    ("  42  ",  42.0),    # surrounding whitespace
    ("¥-50",    -50.0),   # leading symbol + negative
])
def test_coerce_number_parses_common_formats(raw, expected):
    assert coerce_number(raw) == expected


# ---------------------------------------------------------------------------
# cap_chart_categories
# ---------------------------------------------------------------------------


def test_cap_short_circuits_when_no_y_keys():
    """No y_keys → no aggregation possible → return rows unchanged. Guard
    against accidentally treating an empty chart as a 0-category chart."""
    rows = [{"x": "A", "v": 1}, {"x": "B", "v": 2}]
    assert cap_chart_categories(rows, "x", []) == rows
    assert cap_chart_categories(rows, "x", [], cap=3) == rows


def test_cap_short_circuits_when_no_rows():
    assert cap_chart_categories([], "x", ["v"]) == []


def test_cap_returns_one_row_per_distinct_category_when_under_cap():
    """Under ``cap`` distinct categories → aggregate raw rows into one row
    per category (sum y-values; first-seen value for non-y columns)."""
    rows = [
        {"x": "A", "v": 10, "note": "first"},
        {"x": "B", "v": 20},
        {"x": "A", "v": 5},                      # duplicate label — sum
        {"x": "C", "v": 1},
        {"x": "D", "v": 0.5},
        {"x": "E", "v": 0.5},
        {"x": "F", "v": 0.5},                    # 7 distinct, cap=8 → under cap
    ]
    out = cap_chart_categories(rows, "x", ["v"], cap=8)
    # All distinct categories survive (we're under the cap). Aggregated by
    # x-label; A sums to 15.
    assert {r["x"] for r in out} == {"A", "B", "C", "D", "E", "F"}
    by_label = {r["x"]: r["v"] for r in out}
    assert by_label["A"] == 15
    assert by_label["B"] == 20
    assert by_label["F"] == 0.5


def test_cap_folds_excess_into_other_bucket_ranked_by_sum():
    """Over ``cap`` → top ``cap`` categories kept; rest folded into one
    ``other_label`` row whose y-values are the summed remainder. Ranking is
    by the *sum* of numeric y-values per category."""
    rows = [
        {"x": "A", "v": 100},
        {"x": "B", "v": 50},
        {"x": "C", "v": 30},
        {"x": "D", "v": 20},
        {"x": "E", "v": 10},
        {"x": "F", "v": 5},     # would be 6th
        {"x": "G", "v": 1},     # would be 7th
    ]
    # cap=5 → top 5 (A,B,C,D,E) + Other(F+G=6)
    out = cap_chart_categories(rows, "x", ["v"], cap=5)
    labels = [r["x"] for r in out]
    assert labels[:5] == ["A", "B", "C", "D", "E"]
    assert labels[5] == OTHER_BUCKET_LABEL
    assert out[5]["v"] == 6.0   # F(5) + G(1)


def test_cap_with_unparseable_y_values_still_aggregates():
    """When y-values can't be parsed (e.g. LLM emits ``"?"`` placeholders),
    ``coerce_number`` returns None → the bucket accumulates zeros. The cap
    still folds the long tail into ``Other``. This matches the long-standing
    behaviour carried over from ``_common.cap_chart_categories``; the goal
    of this refactor was sandbox portability, not semantic change."""
    rows = [
        {"x": "A", "v": "?"},
        {"x": "B", "v": "!"},
        {"x": "C", "v": "?"},
        {"x": "D", "v": "!"},
        {"x": "E", "v": "?"},
        {"x": "F", "v": "!"},
        {"x": "G", "v": "?"},
        {"x": "H", "v": "!"},
        {"x": "I", "v": "?"},
        {"x": "J", "v": "!"},
    ]
    out = cap_chart_categories(rows, "x", ["v"], cap=3)
    # 10 distinct labels folded into 3 top + Other.
    assert {r["x"] for r in out} <= {"A", "B", "C", "D", "E", "F", "G", "H", "I", "J", OTHER_BUCKET_LABEL}
    assert OTHER_BUCKET_LABEL in {r["x"] for r in out}
    # Unparseable → bucket sums to 0; the function still completes.
    for r in out:
        assert r["v"] == 0


def test_cap_sums_multi_y_keys():
    """With multiple y_keys, the cap ranks by the *sum* of all numeric
    y-values per category. NOTE: the existing implementation sets every
    y_key in the ``Other`` row to the SAME ``other_sum`` (the sum of
    category-level totals), not the per-key sum. That long-standing
    behaviour is preserved by this refactor — fixing it is out of scope
    (would change semantics for in-process renderers too)."""
    rows = [
        {"x": "A", "rev": 100, "qty": 5},
        {"x": "B", "rev": 50,  "qty": 1},
        {"x": "C", "rev": 30,  "qty": 4},
        {"x": "D", "rev": 20,  "qty": 2},
        {"x": "E", "rev": 10,  "qty": 0.5},
        {"x": "F", "rev": 5,   "qty": 0.5},     # would be 6th
    ]
    # cap=5 → top 5 + Other(F).
    out = cap_chart_categories(rows, "x", ["rev", "qty"], cap=5)
    other = out[-1]
    assert other["x"] == OTHER_BUCKET_LABEL
    # Category F's total = 5 + 0.5 = 5.5; both y_keys in Other get that
    # sum (existing behaviour).
    assert other["rev"] == 5.5
    assert other["qty"] == 5.5


def test_cap_handles_string_numbers():
    """coerce_number handles strings, so the cap should too (the LLM emits
    ``"1,234.5"`` etc. in chart rows)."""
    rows = [
        {"x": "A", "v": "1,000"},
        {"x": "B", "v": "500"},
        {"x": "C", "v": "100"},
        {"x": "D", "v": "50"},
        {"x": "E", "v": "10"},
        {"x": "F", "v": "5"},   # → Other
    ]
    out = cap_chart_categories(rows, "x", ["v"], cap=5)
    assert out[-1]["x"] == OTHER_BUCKET_LABEL
    assert out[-1]["v"] == 5.0


def test_cap_does_not_mutate_input_rows():
    """Pure function — must not mutate the caller's rows."""
    rows = [
        {"x": "A", "v": 10},
        {"x": "B", "v": 20},
        {"x": "A", "v": 5},
        {"x": "C", "v": 1},
        {"x": "D", "v": 0.5},
        {"x": "E", "v": 0.5},
        {"x": "F", "v": 0.5},
    ]
    snapshot = [dict(r) for r in rows]
    cap_chart_categories(rows, "x", ["v"], cap=3)
    assert rows == snapshot


# ---------------------------------------------------------------------------
# Module hygiene
# ---------------------------------------------------------------------------


def test_chart_helpers_exports_expected_public_api():
    """``__all__`` defines the public surface; anything outside it should be
    considered private (e.g. local helpers we may add later)."""
    assert set(_chart_helpers.__all__) == {
        "CHART_CATEGORY_CAP",
        "OTHER_BUCKET_LABEL",
        "coerce_number",
        "cap_chart_categories",
    }