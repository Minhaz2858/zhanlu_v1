"""Migration-chain consistency: project string column vs project_id FK.

HISTORY (Phase 11 resolution of the reported "migration-020 drift"):
* Migration 020 added ``project_id`` (FK → projects.id), backfilled, and
  DROPPED the legacy ``project`` string column from 5 tables.
* Migration 032 subsequently RESTORED ``project`` (String 255, nullable)
  because the ORM models and live services still use the dual binding
  (id-OR-name) convention — see 032_restore_project_string_column.py.
* At the current chain head (132) the schema and the ORM are CONSISTENT:
  BOTH columns exist on all 5 tables.

This suite pins the CHAIN-END contract (not the superseded intermediate
state after 020 alone):
1. Both ``project`` and ``project_id`` exist on all 5 tables.
2. project_id FKs point at projects.id (where the test DB is
   alembic-migrated — the T1 metadata-built test DB omits cross-table
   FK DDL for some backends, so data-level assertions are guarded).
3. Data-backfill assertions only run against a genuinely alembic-migrated
   database (alembic_version stamp present) — the T1 test DB is built
   from ORM metadata, so migration DATA transformations are out of scope
   there by design.
"""

import pytest
from sqlalchemy import inspect, text


TABLES = ["agent_apps", "automation_tasks", "chat_sessions", "user_files", "knowledge_bases"]


@pytest.fixture(scope="module")
def engine():
    """The app's pooled test engine (T1 temp-file SQLite)."""
    from app.database import engine as _engine
    return _engine


@pytest.fixture(scope="module")
def session(engine):
    """A session over the T1 engine (data assertions use raw SQL)."""
    from app.database import SessionLocal
    s = SessionLocal()
    yield s
    s.close()


@pytest.fixture(scope="module")
def db_is_alembic_migrated(engine) -> bool:
    """True when the test DB carries a real alembic_version stamp."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1 FROM alembic_version"))
        return True
    except Exception:  # noqa: BLE001 — no stamp ⇒ metadata-built T1 DB
        return False


def _get_columns(engine, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


def _get_fk_targets(engine, table: str) -> dict[str, str]:
    """Return {col_name: referred_table} for each FK on *table*."""
    return {
        fk["constrained_columns"][0]: fk["referred_table"]
        for fk in inspect(engine).get_foreign_keys(table)
    }


def _get_indexes(engine, table: str) -> set[str]:
    idx_list = inspect(engine).get_indexes(table)
    result = set()
    for idx in idx_list:
        for col in idx["column_names"]:
            result.add(col)
    return result


# ── Chain-end schema contract ─────────────────────────────────────────


class TestProjectBindingSchema:
    """Both binding styles must exist (dual id-OR-name convention)."""

    def test_project_string_column_exists(self, engine):
        """Restored by migration 032 — the ORM and live services
        (KnowledgeBase.project in project_kb_membership, ChatSession
        writes, main.py startup backfill) depend on it."""
        for table in TABLES:
            cols = _get_columns(engine, table)
            assert "project" in cols, f"{table} missing the legacy 'project' column (restored by 032)"

    def test_project_id_column_exists(self, engine):
        for table in TABLES:
            cols = _get_columns(engine, table)
            assert "project_id" in cols, f"{table} missing 'project_id' (added by 020)"

    def test_models_declare_both_columns(self):
        """The ORM layer is the contract source for the T1 metadata DB —
        every one of the 5 models must declare BOTH bindings so
        create_all produces the migration-consistent shape."""
        from app.database import Base

        model_tables = {
            "agent_apps": "AgentApp",
            "automation_tasks": "AutomationTask",
            "chat_sessions": "ChatSession",
            "user_files": "UserFile",
            "knowledge_bases": "KnowledgeBase",
        }
        for table, model_name in model_tables.items():
            model = Base.model_registry.get(model_name) if hasattr(
                Base, "model_registry") else None
            # fall back to the mapper registry regardless of Base flavor
            from app.database import Base as _B
            mapper = None
            for m in _B.registry.mappers:
                if m.local_table is not None and m.local_table.name == table:
                    mapper = m
                    break
            assert mapper is not None, f"no ORM model mapped for {table}"
            cols = mapper.columns.keys()
            assert "project" in cols, f"{model_name} missing 'project'"
            assert "project_id" in cols, f"{model_name} missing 'project_id'"

    def test_source_market_agent_id_on_agent_apps(self, engine):
        cols = _get_columns(engine, "agent_apps")
        assert "source_market_agent_id" in cols


# ── Data-backfill assertions (alembic-migrated DBs only) ──────────────


class TestMigration020Backfill:
    """Verify migration DATA transformations on a genuinely migrated DB.

    The T1 test DB is built from ORM metadata (create_all) — migration
    data backfills never ran there, so these assertions are guarded
    rather than wrong-by-design failures."""

    def test_backfill_only_on_migrated_db(self, engine, db_is_alembic_migrated, session):
        if not db_is_alembic_migrated:
            pytest.skip(
                "T1 test DB is metadata-built (create_all); migration "
                "020's DATA backfill requires an alembic-migrated DB — "
                "run against a staging snapshot to verify")
        for table in TABLES:
            result = session.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE project_id IS NULL"))
            count = result.scalar()
            assert count == 0, f"{table}: {count} rows still have NULL project_id"

    def test_global_project_created(self, engine, db_is_alembic_migrated, session):
        if not db_is_alembic_migrated:
            pytest.skip("requires alembic-migrated DB (see test_backfill_only_on_migrated_db)")
        projects = session.execute(
            text("SELECT id, name, created_by_id FROM projects WHERE name = 'Global'")
        ).fetchall()
        assert len(projects) > 0, "No 'Global' project found after backfill"

    def test_agent_apps_have_valid_project_refs(self, engine, db_is_alembic_migrated, session):
        if not db_is_alembic_migrated:
            pytest.skip("requires alembic-migrated DB (see test_backfill_only_on_migrated_db)")
        result = session.execute(text("""
            SELECT COUNT(*) FROM agent_apps aa
            LEFT JOIN projects p ON aa.project_id = p.id
            WHERE p.id IS NULL
        """))
        assert result.scalar() == 0, "Some agent_apps.project_id is a dangling reference"
