"""Tests for the read-transaction leak fix (_end_read_txn).

2026-09-03 regression: context-assembly helpers (prepare_data_source_runtime
/ _extend_with_project_kbs) run read-only SELECTs on a caller-supplied
session that the caller then holds across a long agent turn. The open
transaction pinned the connection in ``idle in transaction`` for the whole
turn — observed 7 leaked backend connections stuck on the KB-scoping SELECT.

_end_read_txn must end the read transaction when the session carries no
pending writes, and MUST NOT roll back (discarding) when the caller has
uncommitted INSERTs/UPDATEs.
"""

import os
import sys
from sqlalchemy import Column, Integer, String, create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


_TestBase = declarative_base()


class _Row(_TestBase):
    __tablename__ = "row"
    id = Column(Integer, primary_key=True)
    value = Column(String)


def _session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    _TestBase.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


class TestEndReadTxn:
    def test_ends_read_transaction(self):
        from app.services.data_source_runtime.data_source_runtime import _end_read_txn

        db = _session()
        db.execute(text("SELECT 1"))
        assert db.in_transaction()
        _end_read_txn(db)
        assert not db.in_transaction()
        db.close()

    def test_skips_when_pending_writes_exist(self):
        from app.services.data_source_runtime.data_source_runtime import _end_read_txn

        db = _session()
        db.execute(text("SELECT 1"))
        db.add(_Row(value="pending"))
        assert db.new
        _end_read_txn(db)
        # Must NOT have rolled back the caller's pending work.
        assert db.new, "pending INSERT must survive _end_read_txn"
        db.close()
