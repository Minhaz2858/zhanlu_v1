"""Shadow-mode wiring — the nl2sql validator/policy runs alongside the live
agent data path (NLAnswerService), logging divergence without ever changing
the served result.

Covers:
- nl2sql.validate_sql facade (never raises, correct verdicts).
- NLAnswerService._shadow_validate_and_log: flag gating, divergence
  classification, log persistence, error swallowing.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.models.nl2sql_query_log import Nl2sqlQueryLog


@pytest.fixture
def db(tmp_path):
    db_file = tmp_path / f"test_{uuid.uuid4().hex[:8]}.db"
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


# ── facade ────────────────────────────────────────────────────────────────

class TestValidateSqlFacade:
    def test_plain_select_is_valid(self):
        from app.services.nl2sql import validate_sql

        res = validate_sql("SELECT id, name FROM customers LIMIT 10")
        assert res["is_valid"] is True
        assert res["policy_allowed"] is True
        assert "customers" in [t.lower() for t in res["tables_referenced"]]
        assert res["sql_hash"]

    def test_write_statement_rejected(self):
        from app.services.nl2sql import validate_sql

        res = validate_sql("DELETE FROM customers")
        assert res["is_valid"] is False
        assert res["errors"]

    def test_garbage_never_raises(self):
        from app.services.nl2sql import validate_sql

        res = validate_sql("THIS IS NOT SQL AT ALL ;;")
        assert res["is_valid"] is False  # rejected, not raised

    def test_allowed_tables_enforced(self):
        from app.services.nl2sql import validate_sql

        res = validate_sql(
            "SELECT id FROM secret_table", allowed_tables=["public_table"]
        )
        assert res["is_valid"] is False or res["policy_allowed"] is False


# ── shadow hook ───────────────────────────────────────────────────────────

class TestShadowHook:
    def _run_hook(self, db, monkeypatch, **kw):
        import app.database as app_db
        from app.services.db import nl_answer_service as nas

        Session = sessionmaker(bind=db.get_bind())
        monkeypatch.setattr(app_db, "SessionLocal", Session)
        nas._shadow_validate_and_log(**kw)

    def test_flag_off_writes_nothing(self, db, monkeypatch):
        monkeypatch.setattr(settings, "NL2SQL_SHADOW_MODE_ENABLED", False)
        self._run_hook(
            db, monkeypatch,
            kb_id="kb-1", question="q", sql="SELECT 1",
            live_success=True,
        )
        assert db.query(Nl2sqlQueryLog).count() == 0

    def test_valid_sql_live_success_agree_pass(self, db, monkeypatch):
        monkeypatch.setattr(settings, "NL2SQL_SHADOW_MODE_ENABLED", True)
        self._run_hook(
            db, monkeypatch,
            kb_id="kb-1", question="how many users?",
            sql="SELECT count(*) FROM users",
            live_success=True,
        )
        rows = db.query(Nl2sqlQueryLog).all()
        assert len(rows) == 1
        r = rows[0]
        assert r.outcome == "shadow"
        assert r.datasource_id == "kb-1"
        assert "agree_pass" in (r.explanation or "")
        assert r.generated_sql == "SELECT count(*) FROM users"
        assert r.policy_decision == "allowed"

    def test_shadow_rejects_but_live_succeeded_divergence(self, db, monkeypatch):
        """The key risk metric: validator over-rejection of working SQL."""
        monkeypatch.setattr(settings, "NL2SQL_SHADOW_MODE_ENABLED", True)
        self._run_hook(
            db, monkeypatch,
            kb_id="kb-1", question="q",
            sql="UPDATE users SET name='x'",  # live "succeeded" (hypothetically)
            live_success=True,
        )
        r = db.query(Nl2sqlQueryLog).first()
        assert r is not None
        assert "shadow_fail_live_pass" in (r.explanation or "")
        assert r.validation_errors is not None

    def test_shadow_passes_but_live_failed(self, db, monkeypatch):
        monkeypatch.setattr(settings, "NL2SQL_SHADOW_MODE_ENABLED", True)
        self._run_hook(
            db, monkeypatch,
            kb_id="kb-1", question="q",
            sql="SELECT id FROM users",
            live_success=False, live_error="connection timeout",
        )
        r = db.query(Nl2sqlQueryLog).first()
        assert "shadow_pass_live_fail" in (r.explanation or "")

    def test_hook_swallows_internal_errors(self, db, monkeypatch):
        """Shadow logging must never break the served answer."""
        monkeypatch.setattr(settings, "NL2SQL_SHADOW_MODE_ENABLED", True)
        import app.database as app_db
        from app.services.db import nl_answer_service as nas

        def _boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(app_db, "SessionLocal", _boom)
        # Must not raise
        nas._shadow_validate_and_log(
            kb_id="kb-1", question="q", sql="SELECT 1", live_success=True
        )
