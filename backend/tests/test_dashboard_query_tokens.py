"""Token render layer for widget SQL (date window, cross-widget filters, drill).

These are pure-function unit tests — no DB. The render layer is the safety
foundation for date-range selection, cross-widget filtering, and drill-down.
"""
import datetime as dt
import pytest
from app.services.dashboard_query import render_widget_sql, _resolve_window


def test_from_to_date_tokens_become_iso_literals():
    params = {"from": "2026-07-01T00:00:00", "to": "2026-07-29T00:00:00"}
    out = render_widget_sql(
        "SELECT * FROM t WHERE created_at >= :from AND created_at < :to AND d <= :date",
        params, [],
    )
    assert "'2026-07-01 00:00:00'" in out
    assert "'2026-07-29 00:00:00'" in out
    assert "'2026-07-29'" in out
    assert ":from" not in out and ":to" not in out and ":date" not in out


def test_unset_dim_token_becomes_noop():
    out = render_widget_sql(
        "SELECT * FROM t WHERE :dim_region", {},
        [{"token": "region", "column": "region"}],
    )
    assert out == "SELECT * FROM t WHERE 1=1"


def test_set_dim_token_becomes_column_equals_literal():
    out = render_widget_sql(
        "SELECT * FROM t WHERE :dim_region",
        {"filters": {"region": "Asia"}},
        [{"token": "region", "column": "region"}],
    )
    assert out == "SELECT * FROM t WHERE region = 'Asia'"


def test_dim_value_single_quotes_escaped():
    out = render_widget_sql(
        "SELECT * FROM t WHERE :dim_name",
        {"filters": {"name": "O'Brien"}},
        [{"token": "name", "column": "full_name"}],
    )
    assert "full_name = 'O''Brien'" in out


def test_drill_value_token():
    out = render_widget_sql(
        "SELECT * FROM t WHERE cat = :drill_value", {"drill_value": "Books"}, []
    )
    assert "cat = 'Books'" in out


def test_unknown_dim_token_raises():
    with pytest.raises(ValueError, match="Unknown dimension token"):
        render_widget_sql("WHERE :dim_region", {}, [])  # no dimensions declared


def test_unknown_token_shape_raises():
    with pytest.raises(ValueError, match="Unknown token"):
        render_widget_sql("SELECT * FROM t WHERE x = :foobar", {}, [])


def test_default_window_last_30_days_when_absent():
    frm, to = _resolve_window({})
    assert (to - frm).days == 30


def test_default_window_explicit_dates():
    frm, to = _resolve_window({"from": "2026-01-01T00:00:00", "to": "2026-02-01T00:00:00"})
    assert frm == dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def test_render_revalidates_rejects_injection_via_drill():
    # drill_value is escaped to a literal, but the payload contains a ';'
    # followed by non-whitespace. The post-render validate_widget_sql (single-
    # statement guard) catches it and rejects the whole render — safe behavior.
    with pytest.raises(ValueError):
        render_widget_sql(
            "SELECT * FROM t WHERE x = :drill_value",
            {"drill_value": "a'; DROP TABLE t; --"}, [],
        )
