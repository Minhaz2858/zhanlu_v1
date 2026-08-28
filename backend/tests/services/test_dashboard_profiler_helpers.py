import pytest

from app.services.dashboard_profiler import (
    classify_column_type,
    infer_shape,
    looks_like_iso_date,
)


def test_classify_date_types_union():
    for t in ("date", "datetime", "timestamp", "timestamp without time zone",
              "datetime(6)", "timestamp(3)"):
        assert classify_column_type(t) == "date", t


def test_classify_numeric_types():
    for t in ("int", "bigint", "decimal(18,2)", "numeric(10,4)", "float", "double"):
        assert classify_column_type(t) == "number", t


def test_classify_text_fallback():
    assert classify_column_type("varchar(255)") == "text"
    assert classify_column_type("nvarchar") == "text"
    assert classify_column_type("TEXT") == "text"


def test_classify_unknown_defaults_text():
    assert classify_column_type("geography") == "unknown"
    assert classify_column_type(None) == "unknown"


def test_iso_date_strings():
    assert looks_like_iso_date("2026-08-26")
    assert looks_like_iso_date("2026-08-26T18:19:04")
    assert looks_like_iso_date("2026/08/26")
    assert not looks_like_iso_date("cracked_c5")
    assert not looks_like_iso_date("not-a-date")
    assert not looks_like_iso_date(None)


def test_shape_empty_when_no_rows():
    assert infer_shape("date", 0, 0, 1.0, 10) == "empty"


def test_shape_sparse_when_null_heavy():
    assert infer_shape("text", 100, 3, 0.9, 100) == "sparse"


def test_shape_time_series_for_date():
    assert infer_shape("date", 100, 50, 0.0, 100) == "time_series"


def test_shape_category_low_cardinality():
    assert infer_shape("text", 100, 3, 0.0, 100) == "category"


def test_shape_continuous_high_cardinality_numeric():
    assert infer_shape("number", 100, 90, 0.0, 100) == "continuous"


def test_shape_numeric_low_cardinality_is_category():
    assert infer_shape("number", 100, 10, 0.0, 100) == "category"


def test_shape_high_cardinality_text_is_category():
    assert infer_shape("text", 1000, 500, 0.0, 1000) == "category"


def test_build_profile_queries_uses_quote_ident():
    from app.services.dashboard_profiler import build_profile_queries
    qs = build_profile_queries("sales_orders", ["product", "amount", "created_at"])
    joined = "\n".join(qs)
    assert "`sales_orders`" in joined or '"sales_orders"' in joined
    assert "COUNT(DISTINCT `product`)" in joined or 'COUNT(DISTINCT "product")' in joined
    assert "MIN(`amount`)" in joined or 'MIN("amount")' in joined
    assert "MAX(`created_at`)" in joined or 'MAX("created_at")' in joined


def test_build_profile_queries_has_row_count():
    from app.services.dashboard_profiler import build_profile_queries
    qs = build_profile_queries("t", ["a"])
    assert any(q.strip().upper().startswith("SELECT COUNT(*)") for q in qs)


def test_build_profile_queries_rejects_unsafe_identifiers():
    from app.services.dashboard_profiler import build_profile_queries
    with pytest.raises(ValueError):
        build_profile_queries("t; DROP TABLE x", ["a"])
    with pytest.raises(ValueError):
        build_profile_queries("t", ["a; DELETE FROM t"])


def test_profiler_source_has_no_demo_identifiers():
    """HARD RULE: profiler must be DB-agnostic — no demo table/column names."""
    import pathlib
    src = pathlib.Path(__file__).parents[2] / "app" / "services" / "dashboard_profiler.py"
    text = src.read_text()
    # The module docstring is allowed to NAME the rule's examples; strip the
    # docstring region before scanning so a naive scan passes only if the
    # identifiers are never USED as values in code.
    body = text.split('"""', 2)[-1]  # everything after the module docstring
    for bad in ("erp_v_sale_orderentry", "PLANDATE", "FALLAMOUNT", "FDATE", "forgid"):
        assert bad not in body, f"hardcoded identifier leaked into profiler code: {bad}"


def test_prompt_mentions_profile_data():
    """The build prompt must instruct calling profile_data before building."""
    import pathlib
    src = pathlib.Path(__file__).parents[2] / "app" / "services" / "agent_prompts.py"
    text = src.read_text()
    assert "profile_data" in text
