"""DashboardVersion table exists with the expected columns."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import create_engine, inspect as sa_inspect


def test_dashboard_versions_table(tmp_path):
    """The dashboard_versions table is created with the expected columns.

    Uses a private engine bound to the shared Base.metadata (populated by
    `import app.models`) - NOT the global engine / os.environ, which couples
    to import-order and is fragile across tests in one process.
    """
    from app.database import Base
    import app.models  # noqa: registers all mappers on Base.metadata

    engine = create_engine(f"sqlite:///{tmp_path/'t.db'}")
    Base.metadata.create_all(engine)
    cols = {c["name"] for c in sa_inspect(engine).get_columns("dashboard_versions")}
    assert {"dashboard_id", "version_number", "config", "source", "summary"} <= cols


def test_update_dashboard_pushes_prior_version(tmp_path):
    """_update_dashboard pushes a DashboardVersion of the PRIOR config before applying the edit.

    Reuses _setup_binding_db from the sibling binding test (private engine +
    shared Base.metadata, seeded org/user/project/kb/conv1 + a created+bound
    dashboard titled "Sales") rather than the brief's broken importlib.reload
    fixture. The brief's verbatim test also nulled conversation_id (which would
    break Task 3's resolution); this version keeps the binding so the update
    resolves the dashboard and reaches the version-push step.
    """
    import asyncio
    sys.path.insert(0, os.path.dirname(__file__))
    from test_dashboard_binding import _setup_binding_db
    from app.services.tool_handlers.dashboard_tools import _update_dashboard
    from app.models.dashboard_version import DashboardVersion
    from app.models.dashboard import Dashboard

    SessionLocal, ctx = _setup_binding_db(tmp_path, with_bound=True)  # creates+binds "Sales"
    with SessionLocal() as db:
        d = db.query(Dashboard).filter(Dashboard.org_id == "o1").first()
        did = d.id
        assert d.name == "Sales"

    # First edit -> a version capturing the PRIOR (pre-edit) config is pushed.
    with SessionLocal() as db:
        updated = asyncio.run(_update_dashboard({"title": "S2"}, db, "u1", context=ctx))
    assert updated["success"], updated

    with SessionLocal() as db:
        versions = (
            db.query(DashboardVersion)
            .filter_by(dashboard_id=did)
            .order_by(DashboardVersion.version_number)
            .all()
        )
        assert len(versions) == 1
        assert versions[0].config["name"] == "Sales"  # PRIOR (pre-edit) title
        assert versions[0].source == "agent"


def test_undo_restores_prior_config_and_records_undo_version(tmp_path):
    """undo_dashboard_edit restores the prior config and records a source='undo' version.

    Reuses _setup_binding_db from the sibling binding test (private engine +
    shared Base.metadata, seeded org/user/project/kb/conv1 + a created+bound
    dashboard titled "Sales"). The brief's verbatim test had two bugs:
    (1) it passed dashboard_id in the context dict but the impl reads
        dashboard_id from pydantic args, not context — so the test could
        never pass even with a correct impl; (2) it nulled conversation_id,
        which would break Task 3's resolution. This version uses `ctx` as-is
        (conversation_id="conv1", bound dashboard) so the conversation-
        resolution path is exercised.
    """
    import asyncio
    sys.path.insert(0, os.path.dirname(__file__))
    from test_dashboard_binding import _setup_binding_db
    from app.services.tool_handlers.dashboard_tools import (
        _update_dashboard,
        _undo_dashboard_edit,
    )
    from app.models.dashboard_version import DashboardVersion
    from app.models.dashboard import Dashboard

    SessionLocal, ctx = _setup_binding_db(tmp_path, with_bound=True)  # creates+binds "Sales"
    with SessionLocal() as db:
        d = db.query(Dashboard).filter(Dashboard.org_id == "o1").first()
        did = d.id
        assert d.name == "Sales"

    # Edit -> Task 6 pushes version 1 (prior config "Sales"), dashboard is now "Changed".
    with SessionLocal() as db:
        updated = asyncio.run(_update_dashboard(
            {"title": "Changed"}, db, "u1", context=ctx
        ))
    assert updated["success"], updated

    # Undo via conversation resolution (ctx has conversation_id="conv1" bound to did).
    with SessionLocal() as db:
        undone = asyncio.run(_undo_dashboard_edit({}, db, "u1", context=ctx))
    assert undone["success"], undone
    assert undone["dashboard"]["name"] == "Sales"  # restored to prior

    # The undo itself records a source="undo" version so it can be redone.
    with SessionLocal() as db:
        undo_v = (
            db.query(DashboardVersion)
            .filter_by(dashboard_id=did, source="undo")
            .all()
        )
        assert len(undo_v) == 1
        # Version 1 (the prior "Sales" snapshot) was consumed by the undo.
        remaining = db.query(DashboardVersion).filter_by(
            dashboard_id=did, source="agent"
        ).all()
        assert len(remaining) == 0
