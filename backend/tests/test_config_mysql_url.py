"""MYSQL_URL config — empty by default, populated from env."""
import importlib
import sys


def _reload_settings():
    """Re-instantiate Settings() so changes to env vars are picked up."""
    sys.modules.pop("app.config", None)
    return importlib.import_module("app.config").settings


def test_mysql_url_default_empty(monkeypatch):
    monkeypatch.delenv("MYSQL_URL", raising=False)
    s = _reload_settings()
    assert s.MYSQL_URL == ""
    assert s.has_external_mysql is False


def test_mysql_url_set_enables(monkeypatch):
    monkeypatch.setenv(
        "MYSQL_URL",
        "mysql+pymysql://root:***@10.10.10.49:3306/aipdp_data_warehouse_prod",
    )
    s = _reload_settings()
    assert s.MYSQL_URL.startswith("mysql+pymysql://")
    assert s.has_external_mysql is True
