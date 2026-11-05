"""Pytest configuration for backend tests — enable async test support.

T1 — test database isolation (2026-10-31, reviewer-approved design):

The previous setup used ONE shared in-memory SQLite database behind
StaticPool. StaticPool means a single DBAPI connection reused by every
Session — sessions did not have independent connections, so one test's
uncommitted writes were visible to (and committable by) another test's
session, and combined runs could interleave transactions. That was the
diagnosed root cause of order-dependent combined-run flakiness.

The T1 contract:
  * INDEPENDENT CONNECTIONS: the test database is a real temp-file
    SQLite database reached through the app's normal pooled engine —
    every SessionLocal() gets its own connection with its own
    transaction, exactly like production. No StaticPool anywhere.
  * ORDER INDEPENDENCE ACROSS MODULES: an autouse module-scoped fixture
    truncates every table and re-seeds the admin user before each test
    module runs, so suite results cannot depend on which modules ran
    earlier (within a module, tests share the database as before).
  * The ":memory:" multi-connection pitfall disappears with the file
    database: tables created in fixtures persist for every connection
    without shared-cache tricks.
"""
import os
import tempfile
import shutil

# Must be set BEFORE any app.* imports so that app.config.settings
# reads the file-backed SQLite URL. Pydantic-settings env vars take
# priority over .env files.
_TEST_DB_DIR = tempfile.mkdtemp(prefix="zhanlu-t1-test-db-")
_TEST_DB_PATH = os.path.join(_TEST_DB_DIR, "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB_PATH}"

import pytest

pytest_plugins = ("pytest_asyncio",)


def pytest_configure(config):
    """Register custom markers to silence warnings."""
    config.addinivalue_line("markers", "llm: test requires a live LLM backend (DeepSeek)")


def pytest_addoption(parser):
    """Register the --no-llm switch (Phase 9: it was read via
    config.getoption in pytest_collection_modifyitems but never
    registered, so passing --no-llm errored out and marked-LLM tests
    always attempted live calls)."""
    parser.addoption(
        "--no-llm", action="store_true", default=False,
        help="skip tests marked 'llm' (they need a live LLM backend)",
    )


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
def _ensure_independent_file_db_engine():
    """Verify the global engine honors the T1 contract (T1, 2026-10-31).

    The engine must be the app's NORMAL pooled engine (independent
    connection per session) on the temp-file test database — never a
    StaticPool single-connection swap. The DATABASE_URL env var is set
    before any app import, so no engine surgery is needed; this fixture
    fails loudly if that invariant is ever violated again.
    """
    from sqlalchemy.pool import StaticPool
    from app.config import settings
    from app.database import engine

    assert not isinstance(engine.pool, StaticPool), (
        "T1 violation: the test engine uses StaticPool (one shared DBAPI "
        "connection) — sessions no longer have independent connections"
    )
    assert settings.DATABASE_URL == os.environ["DATABASE_URL"], (
        "T1 violation: app settings and the test DATABASE_URL env var "
        "disagree — app imports happened before the test URL was set"
    )
    yield


def _seed_admin_user():
    """Seed the admin user the auth tests log in with (idempotent)."""
    from app.models.user import User
    from app.database import SessionLocal
    from app.services.auth_service import auth_service

    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.email == "admin@zhanlu.dev").first()
        if existing is None:
            db.add(
                User(
                    email="admin@zhanlu.dev",
                    full_name="Admin",
                    password_hash=auth_service.hash_password("admin123"),
                    role="admin",
                    org_id="default-org",
                    app_id="default-app",
                    is_deleted=False,
                )
            )
            db.commit()
    finally:
        db.close()


def _reset_database():
    """Truncate every table and re-seed the admin user (T1).

    Runs at MODULE boundaries so test modules cannot observe rows left
    by earlier modules (order independence). FK enforcement is disabled
    on the cleanup connection only; every other connection keeps the
    app.database connect-time ``PRAGMA foreign_keys=ON``.

    Phase 9 reliability fix: models registered lazily by mid-sweep test
    modules (e.g. app.models.automation_file, imported directly by a
    later test) appear in Base.metadata only AFTER the session-scoped
    bootstrap ran create_all — truncating such a table then failed with
    "no such table" and cascaded ~12k setup errors across the sweep.
    create_all is idempotent, so re-running it here lets the truncate
    loop see exactly the tables that exist in the database.
    """
    import app.models  # noqa: F401 — register every mapper on Base.metadata
    from app.database import Base, engine

    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        try:
            for table in reversed(Base.metadata.sorted_tables):
                conn.exec_driver_sql(f'DELETE FROM "{table.name}"')
        finally:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    _seed_admin_user()


# --- App-DB bootstrap for TestClient-driven tests ---------------------------
# Tests that drive the app through ``fastapi.testclient.TestClient`` (login →
# endpoint) need the same database the app's own lifespan would build: the full
# schema plus a seeded admin user. ``TestClient(app)`` used as a plain object
# does NOT run the lifespan, so ``Base.metadata.create_all`` never fires and the
# first query (e.g. the login's ``SELECT ... FROM users``) dies with
# ``sqlite3.OperationalError: no such table: users``. This session-scoped
# fixture closes that gap once, on the pooled engine that ``get_db`` uses.
@pytest.fixture(autouse=True, scope="session")
def _bootstrap_app_schema(_ensure_independent_file_db_engine):
    """Create the full schema on the test database and seed the admin user.

    Mirrors ``main.lifespan`` (create_all) plus the admin seed the auth
    tests log in with (admin@zhanlu.dev / admin123).
    """
    import app.models  # noqa: F401 — register every mapper on Base.metadata
    from app.database import Base, engine

    Base.metadata.create_all(bind=engine)
    _seed_admin_user()
    yield


@pytest.fixture(autouse=True, scope="module")
def _fresh_db_per_module(_bootstrap_app_schema):
    """T1: every test module starts from a truncated, admin-seeded DB.

    Guarantees module-level order independence: what a module sees never
    depends on which modules ran before it. Within a module, tests share
    the database exactly as before.
    """
    _reset_database()
    yield


def pytest_unconfigure(config):
    """Remove the T1 temp test database directory."""
    shutil.rmtree(_TEST_DB_DIR, ignore_errors=True)


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

# ─────────────────────────────────────────────────────────────────────────────
# Phase 11 (S4) — cross-module GLOBAL state isolation
# ─────────────────────────────────────────────────────────────────────────────
# The T1 per-module DB truncation covers database state, but in-process
# globals survived module boundaries: agent_tools.TOOL_CONTEXT leaked ids
# (e.g. chat_session_id from automation tests) into later modules — the
# suspected driver of the full-sweep order-dependent failure clusters.


@pytest.fixture(autouse=True, scope="module")
def _reset_process_globals():
    from app.services import agent_tools as _at

    def _clear():
        try:
            _at.TOOL_CONTEXT.clear()
        except Exception:  # noqa: BLE001
            pass
        try:
            from app.services.tenant_context import set_tenant_org
            set_tenant_org(None)
        except Exception:  # noqa: BLE001
            pass

    _clear()
    yield
    _clear()
