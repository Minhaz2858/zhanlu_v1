"""P0 pilot fix (2026-10-31): forecast discovery must generate dialect-correct
SQL. Previously _quote hardcoded MySQL backticks and _time_bucket_expr emitted
postgres DATE_TRUNC unconditionally — forecast discovery silently found 0
candidates on every postgres source (the execution gate correctly refused the
backtick SQL)."""
from app.services.forecasting.discovery import _quote, _time_bucket_expr


def test_quote_mysql_family_uses_backticks():
    assert _quote("month", "mysql") == "`month`"
    assert _quote("month", "mariadb") == "`month`"


def test_quote_postgres_family_uses_double_quotes():
    assert _quote("month", "postgresql") == '"month"'
    assert _quote("month", "postgres") == '"month"'
    assert _quote("month", "sqlite") == '"month"'
    assert _quote("month", "") == '"month"'  # unknown dialect → ANSI


def test_quote_is_idempotent_and_strips_foreign_quotes():
    assert _quote('"month"', "postgresql") == '"month"'
    assert _quote("`month`", "mysql") == "`month`"
    assert _quote("`month`", "postgresql") == '"month"', (
        "a MySQL-quoted identifier passed to a postgres KB must be requoted"
    )


def test_bucket_postgres_monthly_uses_date_trunc():
    expr = _time_bucket_expr('"month"', "monthly", "postgresql")
    assert "DATE_TRUNC('month'" in expr and '"' in expr
    assert "`" not in expr


def test_bucket_mysql_monthly_uses_date_format():
    expr = _time_bucket_expr("`month`", "monthly", "mysql")
    assert "DATE_FORMAT" in expr and "`" not in expr.replace("`month`", "")
    assert expr.count("`") == 2  # only the identifier itself


def test_bucket_mysql_daily_uses_date():
    assert _time_bucket_expr("`order_date`", "daily", "mysql") == "DATE(`order_date`)"


def test_bucket_postgres_daily_uses_date_trunc_day():
    expr = _time_bucket_expr('"order_date"', "daily", "postgresql")
    assert "DATE_TRUNC('day'" in expr


def test_bucket_sqlite_monthly_uses_start_of_month():
    expr = _time_bucket_expr('"month"', "monthly", "sqlite")
    assert "start of month" in expr
