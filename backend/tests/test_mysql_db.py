"""mysql_db — lazy engine, 503 when unconfigured, reachability probe."""
import importlib
import sys

import pytest
from fastapi import HTTPException


def _reload(monkeypatch, url=""):
    if url:
        monkeypatch.setenv("MYSQL_URL", url)
    else:
        monkeypatch.delenv("MYSQL_URL", raising=False)
    sys.modules.pop("app.core.mysql_db", None)
    sys.modules.pop("app.config", None)
    importlib.import_module("app.config")  # rebuild settings
    return importlib.import_module("app.core.mysql_db")


def test_get_mysql_engine_returns_none_when_unconfigured(monkeypatch):
    mod = _reload(monkeypatch)
    assert mod.get_mysql_engine() is None


def test_get_mysql_db_raises_503_when_unconfigured(monkeypatch):
    mod = _reload(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        next(mod.get_mysql_db())
    assert exc.value.status_code == 503
    assert exc.value.detail == "mysql_unavailable"


def test_get_mysql_engine_returns_engine_when_configured(monkeypatch):
    mod = _reload(monkeypatch, "mysql+pymysql://root:***@10.10.10.49:3306/db")
    eng = mod.get_mysql_engine()
    assert eng is not None
    # Cached on second call (same object)
    assert mod.get_mysql_engine() is eng


def test_is_mysql_reachable_false_when_unconfigured(monkeypatch):
    mod = _reload(monkeypatch)
    assert mod.is_mysql_reachable() is False
