import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")
from tests._dashboard_e2e_helpers import parse_psql_row, NOW_WIDGET, VERSION_WIDGET
from app.services.dashboard_query import validate_widget_sql


def test_parse_psql_row_valid():
    line = "bfab2d42-9985-4529-8a36-d6a66dbeb908|rm-x.mysql.rds.aliyuncs.com|3306|erpdb|erpuser|secret"
    cfg = parse_psql_row(line)
    assert cfg["kb_id"] == "bfab2d42-9985-4529-8a36-d6a66dbeb908"
    assert cfg["host"] == "rm-x.mysql.rds.aliyuncs.com"
    assert cfg["port"] == 3306
    assert cfg["database_name"] == "erpdb"
    assert cfg["username"] == "erpuser"
    assert cfg["password"] == "secret"


def test_parse_psql_row_bad():
    assert parse_psql_row("") is None
    assert parse_psql_row("only|three|cols") is None


def test_widgets_are_readonly_and_table_free():
    validate_widget_sql(NOW_WIDGET["sql"])      # no raise
    validate_widget_sql(VERSION_WIDGET["sql"])  # no raise
    assert "NOW(6)" in NOW_WIDGET["sql"]
    assert "VERSION()" in VERSION_WIDGET["sql"]
