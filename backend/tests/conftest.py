"""Pytest configuration for backend tests — enable async test support."""
import os

# Must be set BEFORE any app.* imports so that app.config.settings
# reads SQLite. Pydantic-settings env vars take priority over .env files.
# Use file::memory:?cache=shared + StaticPool so the in-memory database
# persists across sessions — without sharing, each SessionLocal() opens
# a fresh connection and the tables created in _migrate_schema fixtures
# vanish (the classic SQLite ":memory:" multi-connection pitfall).
os.environ["DATABASE_URL"] = "sqlite:///file::memory:?cache=shared&uri=true"

import pytest
from sqlalchemy.pool import StaticPool
from sqlalchemy import event

pytest_plugins = ("pytest_asyncio",)


def pytest_configure(config):
    """Register custom markers to silence warnings."""
    config.addinivalue_line("markers", "llm: test requires a live LLM backend (DeepSeek)")


# --- Synthetic shop-db fixture (real SQLite + catalog/graph mocks) ---------
# Registered here (top-level) so any test under backend/tests/ can request
# ``shop_db_path`` / ``shop_catalog_meta_fixture``. Builders live in
# tests/fixtures/shop_db/conftest.py.
from tests.fixtures.shop_db.conftest import (  # noqa: E402
    build_shop_db as _build_shop_db,
    shop_catalog_meta as _shop_catalog_meta,
)


@pytest.fixture(scope="module")
def shop_db_path():
    """Real SQLite file with the English snake_case shop schema + data."""
    return _build_shop_db()


@pytest.fixture(scope="module")
def shop_catalog_meta_fixture():
    """catalog_meta dict mock derived from the shop fixture's schema."""
    return _shop_catalog_meta()


def pytest_collection_modifyitems(config, items):
    """Auto-skip LLM tests when --no-llm is passed or LLM is unavailable."""
    skip_llm = config.getoption("--no-llm", default=False)
    if not skip_llm:
        # Check if LLM is available
        try:
            from app.services.llm_service import llm_call
        except Exception:
            skip_llm = True

    if skip_llm:
        skip_marker = pytest.mark.skip(reason="LLM not available")
        for item in items:
            if "llm" in item.keywords:
                item.add_marker(skip_marker)


@pytest.fixture(autouse=True, scope="session")
def _ensure_shared_sqlite_engine():
    """Force the global SQLAlchemy engine to use StaticPool so the shared
    in-memory database is reachable from every SessionLocal() call.
    Without this, each new SessionLocal() opens a fresh :memory: connection
    and sees an empty schema (tables created in test fixtures vanish).
    """
    from app.database import engine  # noqa: F401
    # Re-create the engine with StaticPool if not already set
    if not isinstance(engine.pool, StaticPool):
        from sqlalchemy import create_engine as _ce
        from app.config import settings
        new_engine = _ce(
            settings.DATABASE_URL,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        # Replace module-level engine + SessionLocal
        import app.database as _db_mod
        _db_mod.engine = new_engine
        _db_mod.SessionLocal = _db_mod.sessionmaker(
            autocommit=False, autoflush=False, bind=new_engine,
        )
        # Re-enable FK pragma on the new engine
        @event.listens_for(new_engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    yield


# --- Config-driven domain signals (generic test data) -----------------------
# The platform is industry-agnostic: elasticities / seasonal rules / upstream
# maps are per-app domain-config data. Empty config = no-op overlay. This
# fixture writes a temporary config JSON (the same shape a real app config
# file has), points ZHL_DOMAIN_CONFIG_DIR at it, and loads the parsed values
# into the shared domain-signals tables. Teardown restores the empty
# (fully generic) state. The values are deliberately generic ("widget" etc.).


@pytest.fixture
def domain_signals_config(tmp_path, monkeypatch):
    """Inject a temporary generic domain-signals config into the shared tables.

    Config shape (mirrors a real ``domain_configs/<agent>.json``)::

        {
          "domain_signals": {
            "elasticities":   {"widget": [0.5, 1.0], ...},   # raw × damp
            "seasonal_rules": {"widget|12": -2.5, ...},      # "product|month"
          },
          "upstream_map":    {"widget": ["gadget"]},
          "product_labels":  {"gadget": "Gadget"}
        }

    Returns the config dict; restores the empty state after the test.
    """
    import json as _json

    import app.services.forecasting.domain_signals as _ds
    from app.services.forecasting.analyst import evidence_pack as _ep

    cfg = {
        "domain_signals": {
            "elasticities": {
                "widget": [0.5, 1.0],      # raw 0.5 × damp 1.0  → 0.5
                "widget_t2": [0.5, 0.85],  # raw 0.5 × damp 0.85 → 0.425 (tier 2)
                "gadget": [0.8, 0.85],     # raw 0.8 × damp 0.85 → 0.68
            },
            "seasonal_rules": {
                "widget|1": -2.5,
                "widget|2": -2.5,
                "widget|6": 1.0,
                "widget|8": 1.5,
                "widget|11": -2.5,
                "widget|12": -2.5,
                "gadget|6": 1.5,
            },
        },
        "upstream_map": {
            "widget": ["gadget"],
        },
        "product_labels": {"gadget": "Gadget"},
    }
    cfg_dir = tmp_path / "domain_configs"
    cfg_dir.mkdir()
    (cfg_dir / "forecast.json").write_text(_json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("ZHL_DOMAIN_CONFIG_DIR", str(cfg_dir))

    ds_cfg = cfg["domain_signals"]
    _ds._RAW_ELASTICITIES.clear()
    _ds._RAW_ELASTICITIES.update(
        {k: (v[0], v[1]) for k, v in ds_cfg["elasticities"].items()}
    )
    _ds._ELASTICITIES.clear()
    _ds._ELASTICITIES.update(
        {k: round(v[0] * v[1], 4) for k, v in ds_cfg["elasticities"].items()}
    )
    _ds._SEASONAL_RULES.clear()
    _ds._SEASONAL_RULES.update(
        {(k.split("|")[0], int(k.split("|")[1])): v
         for k, v in ds_cfg["seasonal_rules"].items()}
    )
    _ep.UPSTREAM_MAP.clear()
    _ep.UPSTREAM_MAP.update(
        {k: list(v) for k, v in cfg.get("upstream_map", {}).items()}
    )
    _ep._PRODUCT_LABELS.clear()
    _ep._PRODUCT_LABELS.update(cfg.get("product_labels", {}))

    yield cfg

    _ds._RAW_ELASTICITIES.clear()
    _ds._ELASTICITIES.clear()
    _ds._SEASONAL_RULES.clear()
    _ep.UPSTREAM_MAP.clear()
    _ep._PRODUCT_LABELS.clear()