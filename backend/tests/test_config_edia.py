"""EDIA_MYSQL_URL config — empty by default, populated from env."""
import importlib
import sys


def _reload_settings():
    """Re-instantiate Settings() so changes to env vars are picked up."""
    sys.modules.pop("app.config", None)
    return importlib.import_module("app.config").settings


def test_edia_mysql_url_default_empty(monkeypatch):
    monkeypatch.delenv("EDIA_MYSQL_URL", raising=False)
    s = _reload_settings()
    assert s.EDIA_MYSQL_URL == ""
    assert s.is_edia_enabled is False


def test_edia_mysql_url_set_enables(monkeypatch):
    monkeypatch.setenv(
        "EDIA_MYSQL_URL",
        "mysql+pymysql://root:pw@10.10.10.49:3306/aipdp_data_warehouse_prod",
    )
    s = _reload_settings()
    assert s.EDIA_MYSQL_URL.startswith("mysql+pymysql://")
    assert s.is_edia_enabled is True
