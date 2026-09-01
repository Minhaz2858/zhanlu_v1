"""Tests for discovery._quote — must use MySQL-safe identifier quoting."""

from app.services.forecasting.discovery import _quote


def test_quote_uses_backticks_for_mysql():
    """_quote must produce backtick-quoted identifiers, not double-quoted.
    MySQL uses backticks; double-quoted identifiers are invalid by default."""
    assert _quote("actual_price") == "`actual_price`"


def test_quote_handles_weird_column_names():
    """Real warehouse has columns like 'biz_date(业务日期)'. The quote function
    must handle them without error and produce a valid MySQL identifier."""
    out = _quote("biz_date(业务日期)")
    assert out.startswith("`") and out.endswith("`")
    # Should not contain unescaped backticks inside
    assert out.count("`") == 2


def test_quote_handles_already_quoted_input():
    """If the name is already backtick-quoted, don't double-wrap."""
    out = _quote("`actual_price`")
    assert out == "`actual_price`", "Already-quoted names should not be re-wrapped"
