"""Connectivity preflight: bound sources are probed before spending a run."""
import os
import sqlite3
import sys
import time
import uuid

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.database import SessionLocal
from app.models.knowledge_base import KnowledgeBase
from app.services import automation_executor as ax


def _kb(**kw):
    defaults = dict(id=str(uuid.uuid4()), name="KB", type="database",
                    source_kind="database", is_deleted=False)
    defaults.update(kw)
    return KnowledgeBase(**defaults)


def test_sqlite_source_passes(tmp_path):
    db_file = tmp_path / "ok.sqlite"
    con = sqlite3.connect(db_file)
    con.execute("CREATE TABLE t (id INTEGER)")
    con.close()
    db = SessionLocal()
    try:
        kb = _kb(db_type="sqlite", api_url=str(db_file), file_url=str(db_file))
        db.add(kb); db.commit()
        assert ax._check_bound_source_connectivity(db, [kb.id]) == []
        db.delete(kb); db.commit()
    finally:
        db.rollback(); db.close()


def test_unreachable_db_source_fails_fast_and_bounded():
    db = SessionLocal()
    try:
        kb = _kb(db_type="mysql", host="10.255.255.1", port=3306,
                 database_name="erp", username="u", password="p")
        db.add(kb); db.commit()
        start = time.monotonic()
        failures = ax._check_bound_source_connectivity(db, [kb.id], timeout_seconds=3.0)
        elapsed = time.monotonic() - start
        assert len(failures) == 1
        assert failures[0]["kind"] == "unreachable"
        assert "10.255.255.1" in failures[0]["target"]
        assert elapsed < 10  # bounded by the wrapper, not the driver default
        db.delete(kb); db.commit()
    finally:
        db.rollback(); db.close()


def test_deleted_source_is_misconfigured():
    db = SessionLocal()
    try:
        kb = _kb(db_type="mysql", host="x", is_deleted=True)
        db.add(kb); db.commit()
        failures = ax._check_bound_source_connectivity(db, [kb.id])
        assert failures[0]["kind"] == "misconfigured"
        db.delete(kb); db.commit()
    finally:
        db.rollback(); db.close()


def test_file_sources_are_skipped():
    db = SessionLocal()
    try:
        kb = _kb(source_kind="file", db_type=None, api_url=None,
                 file_url="/nonexistent/file.pdf")
        db.add(kb); db.commit()
        assert ax._check_bound_source_connectivity(db, [kb.id]) == []
        db.delete(kb); db.commit()
    finally:
        db.rollback(); db.close()


def test_executor_calls_preflight_before_agent_run():
    # Source-literal gate, same style as test_automation_ds_preflight.py.
    src = open(os.path.join(_BACKEND_ROOT, "app/services/automation_executor.py")).read()
    gate = src.index("_check_bound_source_connectivity(")
    run = src.index("pool.submit(_run_agent_in_conversation")
    assert gate < run
