"""Pure unit tests for locked-analytics hardening (2026-09-02).

Covers: dimension-token contract fix, delta override (no stale/authored
numbers), per-row-set value-column isolation (multi-measure rows), top-item
share totals, and sign-safe chronological series deltas. Pure functions —
no DB.
"""
import pytest

from app.services.dashboard_app import analytics as A


# ── Dimension token contract ───────────────────────────────────────────

def test_dimensions_from_options_emits_token_column_shape():
    options = {"filters": [{"key": "region", "label": "Region", "column": "region_col"}]}
    assert A._dimensions_from_options(options) == [{"token": "region", "column": "region_col"}]


def test_dimensions_from_options_skips_malformed_entries():
    options = {"filters": ["region", {"key": "cat", "column": "category"}]}
    assert A._dimensions_from_options(options) == [{"token": "cat", "column": "category"}]


def test_dimensions_from_options_skips_missing_column():
    assert A._dimensions_from_options({"filters": [{"key": "region"}]}) == []


# ── Value-column isolation ─────────────────────────────────────────────

def test_value_and_label_keys_frontend_convention():
    row = {"product": "X", "revenue": 100, "target": 120}
    label, value = A._value_and_label_keys(row)
    assert label == "product"
    assert value == "revenue"  # first numeric AFTER the leading label column


def test_value_and_label_keys_single_numeric_column():
    row = {"total": 42}
    label, value = A._value_and_label_keys(row)
    assert value == "total"
    assert label is None


def test_value_and_label_keys_declared_value_wins():
    row = {"region": "EU", "revenue": 100, "target": 120}
    label, value = A._value_and_label_keys(row, declared_value_key="target")
    assert value == "target"


def test_value_and_label_keys_numeric_first_column_is_dimension():
    # SELECT month_no, revenue — the month number is the X, not the measure.
    row = {"month_no": 7, "revenue": 500}
    label, value = A._value_and_label_keys(row)
    assert label == "month_no"
    assert value == "revenue"


def test_top_item_share_total_only_sums_value_column():
    rows = [
        {"product": "A", "revenue": 100, "target": 1000},
        {"product": "B", "revenue": 300, "target": 900},
        {"product": "C", "revenue": 600, "target": 100},
    ]
    top = A.compute_top_item(rows, ("product",))
    assert top["label"] == "C"
    assert top["value"] == 600
    # Old behavior summed every numeric column (revenue + target = 3000) and
    # reported a nonsense 20% share; the isolated total is revenue only (1000).
    assert top["share_pct"] == 60.0


def test_top_item_multi_measure_label_still_found():
    rows = [
        {"product": "A", "revenue": 10, "units": 5},
        {"product": "B", "revenue": 90, "units": 95},
    ]
    top = A.compute_top_item(rows)
    assert top["label"] == "B"
    assert top["value"] == 90


def test_top_item_negative_values_total_handled():
    rows = [
        {"customer": "X", "net": -50},
        {"customer": "Y", "net": 200},
    ]
    top = A.compute_top_item(rows, ("customer",))
    assert top["label"] == "Y"
    assert top["value"] == 200


# ── Series delta: sign + stale override ────────────────────────────────

def test_series_delta_ascending_chronological():
    rows = [
        {"day": "2026-07-01", "revenue": 100},
        {"day": "2026-07-02", "revenue": 150},
    ]
    d = A.compute_series_delta(rows)
    assert d["delta_pct"] == 50.0
    assert d["current"] == 150.0
    assert d["previous"] == 100.0


def test_series_delta_descending_date_order_is_normalized_sign_safe():
    # ORDER BY day DESC — the naive last-vs-prev would report -33.3%.
    rows = [
        {"day": "2026-07-02", "revenue": 150},
        {"day": "2026-07-01", "revenue": 100},
    ]
    d = A.compute_series_delta(rows)
    assert d["delta_pct"] == 50.0


def test_series_delta_descending_with_multiple_measure_columns():
    rows = [
        {"day": "2026-07-03", "revenue": 300, "target": 300},
        {"day": "2026-07-02", "revenue": 200, "target": 250},
        {"day": "2026-07-01", "revenue": 100, "target": 200},
    ]
    d = A.compute_series_delta(rows)
    assert d["delta_pct"] == 50.0  # 300 vs 200, using revenue column only
    assert d["previous"] == 200.0


def test_series_delta_iso_datetime_strings():
    rows = [
        {"ts": "2026-07-01T00:00:00Z", "value": 10},
        {"ts": "2026-07-02T00:00:00Z", "value": 8},
    ]
    d = A.compute_series_delta(rows)
    assert d["delta_pct"] == -20.0


def test_series_delta_no_date_column_leaves_order_alone():
    # No parseable date → cannot guess; assumes SQL contract (ascending).
    rows = [
        {"bucket": "a", "value": 100},
        {"bucket": "b", "value": 90},
    ]
    d = A.compute_series_delta(rows)
    assert d["delta_pct"] == -10.0


def test_series_delta_declining_ascending_series_not_flipped():
    # Real downward trend in ASC order must stay negative.
    rows = [
        {"day": "2026-07-01", "value": 100},
        {"day": "2026-07-02", "value": 80},
    ]
    d = A.compute_series_delta(rows)
    assert d["delta_pct"] == -20.0


def test_series_delta_previous_zero_returns_none():
    rows = [
        {"day": "2026-07-01", "value": 0},
        {"day": "2026-07-02", "value": 10},
    ]
    assert A.compute_series_delta(rows) is None


def test_series_delta_numeric_strings_and_thousands():
    rows = [
        {"day": "2026-07-01", "value": "1,000"},
        {"day": "2026-07-02", "value": "1,500"},
    ]
    d = A.compute_series_delta(rows)
    assert d["delta_pct"] == 50.0


def test_series_delta_single_row_none():
    assert A.compute_series_delta([{"day": "2026-07-01", "value": 1}]) is None


# ── enrich_metric override semantics ───────────────────────────────────

class _FakeDb:
    pass


def _patch_run_sql(monkeypatch, rows):
    monkeypatch.setattr(A, "_run_metric_sql", lambda db, kb, sql, dims: rows)


def test_enrich_metric_overrides_authored_delta(monkeypatch):
    # An LLM-authored estimate (options.delta=12.3) must NEVER survive when a
    # real locked number computes — the old "only when None" gate preserved it.
    _patch_run_sql(monkeypatch, [
        {"day": "2026-07-01", "value": 100},
        {"day": "2026-07-02", "value": 200},
    ])
    metric = {
        "id": "m1", "type": "kpi", "title": "Revenue",
        "sql": "SELECT day, value FROM t",
        "options": {"delta": 12.3, "deltaLabel": "authored guess"},
    }
    out = A.enrich_metric(_FakeDb(), "kb-1", metric)
    assert out["options"]["delta"] == 100.0  # locked 200-vs-100, not 12.3
    assert out["options"]["_locked"]["current"] == 200.0
    assert out["options"]["_locked"]["previous"] == 100.0


def test_enrich_metric_overrides_stale_locked_analytics(monkeypatch):
    # Re-enrich on a changed metric: old delta + old _locked must both go.
    _patch_run_sql(monkeypatch, [
        {"day": "2026-07-01", "value": 500},
        {"day": "2026-07-02", "value": 550},
    ])
    metric = {
        "id": "m1", "type": "line", "title": "Sales",
        "sql": "SELECT day, value FROM t",
        "options": {"delta": 3.0, "_locked": {"current": 103.0, "previous": 100.0}},
    }
    out = A.enrich_metric(_FakeDb(), "kb-1", metric)
    assert out["options"]["delta"] == 10.0
    assert out["options"]["_locked"] == {"current": 550.0, "previous": 500.0}


def test_enrich_metric_no_rows_leaves_metric_untouched(monkeypatch):
    _patch_run_sql(monkeypatch, [])
    metric = {"id": "m1", "type": "kpi", "title": "X", "sql": "SELECT 1", "options": {}}
    out = A.enrich_metric(_FakeDb(), "kb-1", metric)
    assert out == metric


def test_enrich_metric_no_sql_returns_unchanged():
    metric = {"id": "m1", "type": "kpi", "title": "X"}
    assert A.enrich_metric(_FakeDb(), "kb-1", metric) == metric


def test_enrich_metric_bar_attaches_top_item_with_isolated_share(monkeypatch):
    _patch_run_sql(monkeypatch, [
        {"product": "A", "revenue": 200, "units": 2},
        {"product": "B", "revenue": 800, "units": 98},
    ])
    metric = {"id": "m2", "type": "bar", "title": "By product",
              "sql": "SELECT product, revenue FROM t", "options": {}}
    out = A.enrich_metric(_FakeDb(), "kb-1", metric)
    top = out["options"]["topItem"]
    assert top["label"] == "B"
    assert top["value"] == 800
    assert top["share_pct"] == 80.0  # revenue-only total (1000), NOT 1000+100


def test_enrich_metric_passes_declared_dimension_tokens(monkeypatch):
    # Regression: filtered metrics used to crash the render ({"key"} dims) and
    # silently lose analytics. The render must receive {"token"} dims.
    seen = {}

    def fake_run(db, kb, sql, dims):
        seen["dims"] = dims
        return [{"region": "EU", "value": 100}, {"region": "EU", "value": 110}]

    monkeypatch.setattr(A, "_run_metric_sql", fake_run)
    metric = {
        "id": "m3", "type": "kpi", "title": "EU Sales",
        "sql": "SELECT region, value FROM t WHERE :dim_region",
        "options": {"filters": [{"key": "region", "label": "Region", "column": "region"}]},
    }
    out = A.enrich_metric(_FakeDb(), "kb-1", metric)
    assert seen["dims"] == [{"token": "region", "column": "region"}]
    assert out["options"]["delta"] == 10.0


# ── Insights builder sanity ────────────────────────────────────────────

def test_build_locked_insights_only_from_computed():
    metrics = [
        {"id": "a", "title": "Up", "options": {"delta": 5.0}},
        {"id": "b", "title": "Top", "options": {"topItem": {"label": "Z", "value": 50.0, "share_pct": 25.0}}},
        {"id": "c", "title": "Bare", "options": {}},
    ]
    insights = A.build_locked_insights(metrics)
    titles = [i["title"] for i in insights]
    assert any("Up" in t for t in titles)
    assert any("Top: Z" in t for t in titles)
    assert not any("Bare" in t for t in titles)


def test_enrich_spec_never_raises(monkeypatch):
    monkeypatch.setattr(A, "_run_metric_sql",
                        lambda db, kb, sql, dims: (_ for _ in ()).throw(RuntimeError("boom")))
    spec = {"metrics": [{"id": "m", "type": "kpi", "sql": "SELECT 1", "options": {}}]}
    assert A.enrich_spec(_FakeDb(), "kb-1", spec) is spec
