"""Phase 8 T8-1 — AgentDataBinding model: enable flag + unique pair constraint.

Covers: is_enabled column (exists, defaults True, NOT NULL), unique
(agent_app_id, datasource_id) enforcement on live rows, soft-deleted rows
not blocking re-binding (partial unique index), and disable persistence.
"""

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.agent_data_binding import AgentDataBinding

ORG = "org-acme"


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng, tables=[AgentDataBinding.__table__])
    s = sessionmaker(bind=eng)()
    yield s
    s.close()
    eng.dispose()


def _make(**kw):
    defaults = dict(id="b-1", org_id=ORG, agent_app_id="app-1",
                    datasource_id="ds-1")
    defaults.update(kw)
    return AgentDataBinding(**defaults)


def test_is_enabled_column_exists_defaults_true_not_null(db):
    cols = {c["name"]: c
            for c in inspect(db.bind).get_columns("agent_data_bindings")}
    assert "is_enabled" in cols
    assert cols["is_enabled"]["nullable"] is False
    b = _make()
    db.add(b)
    db.commit()
    db.refresh(b)
    assert b.is_enabled is True


def test_duplicate_agent_datasource_pair_rejected(db):
    db.add(_make(id="b-1"))
    db.commit()
    db.add(_make(id="b-2"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_soft_deleted_row_does_not_block_recreate(db):
    b1 = _make(id="b-1")
    db.add(b1)
    db.commit()
    b1.is_deleted = True
    db.commit()
    # Same pair re-created after soft delete must succeed (partial index).
    db.add(_make(id="b-2"))
    db.commit()
    assert db.get(AgentDataBinding, "b-2") is not None


def test_disable_binding_persists(db):
    b = _make()
    db.add(b)
    db.commit()
    b.is_enabled = False
    db.commit()
    db.expire_all()
    assert db.get(AgentDataBinding, "b-1").is_enabled is False
