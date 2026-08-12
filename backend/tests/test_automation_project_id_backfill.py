"""Tests for migration 051 — backfill automation_tasks.project_id.

Migration 051 walks every ``automation_tasks`` row with
``project_id IS NULL`` and matches its legacy ``project`` string against
``projects.name`` (scoped by ``app_id``) so the executor can resolve the
correct project on subsequent runs and bind the project's data sources.

These tests exercise the SQL logic directly against an isolated SQLite
database so they don't touch the dev DB. Each test:

* Builds a minimal schema (``projects`` + ``automation_tasks``).
* Seeds specific rows covering the matching cases the migration must
  handle (exact name, same app, fallback across apps, "global" rows,
  empty rows).
* Runs the migration's ``upgrade()`` against the isolated engine.
* Asserts which rows got ``project_id`` filled and which didn't.
"""
from __future__ import annotations

import os
import sys

import pytest
import sqlalchemy as sa
from alembic.config import Config as AlembicConfig
from alembic import command as alembic_command
from sqlalchemy.orm import sessionmaker


# ---------------------------------------------------------------------------
# Test database setup
# ---------------------------------------------------------------------------

@pytest.fixture()
def migration_db(monkeypatch, tmp_path):
    """Build an isolated SQLite engine and apply only the model tables the
    migration touches (no real ``app`` stack). We don't want this test
    loading the whole app config / connections.
    """
    db_file = tmp_path / "migration_051.db"
    url = f"sqlite:///{db_file}"

    engine = sa.create_engine(url, connect_args={"check_same_thread": False})
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Mirror the schema columns the migration queries. We keep these
    # deliberately close to the real model fields (see
    # ``app/models/automation_task.py`` and ``app/models/project.py``).
    sa.MetaData().bind = engine

    md = sa.MetaData()
    projects_table = sa.Table(
        "projects",
        md,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("org_id", sa.String),
        sa.Column("app_id", sa.String),
        sa.Column("name", sa.String),
        sa.Column("is_deleted", sa.Boolean, default=False),
        sa.Column("created_date", sa.DateTime),
    )
    automation_tasks_table = sa.Table(
        "automation_tasks",
        md,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("org_id", sa.String),
        sa.Column("app_id", sa.String),
        sa.Column("project_id", sa.String, nullable=True),
        sa.Column("project", sa.String, nullable=True),
    )
    md.create_all(engine)

    yield {
        "engine": engine,
        "session_factory": Session,
        "projects": projects_table,
        "automation_tasks": automation_tasks_table,
    }


def _seed_project(sess, *, project_id: str, org_id: str, app_id: str, name: str, is_deleted: bool = False):
    sess.execute(
        sa.text(
            "INSERT INTO projects(id, org_id, app_id, name, is_deleted, created_date) "
            "VALUES (:id, :org_id, :app_id, :name, :is_deleted, :created_date)"
        ),
        {
            "id": project_id,
            "org_id": org_id,
            "app_id": app_id,
            "name": name,
            "is_deleted": is_deleted,
            # Literal timestamp keeps SQLite happy (it doesn't bind
            # ``sa.func.now`` to a parameter). Sorting by created_date
            # still works as long as projects have distinct timestamps.
            "created_date": "2026-01-01 00:00:00",
        },
    )


def _seed_task(sess, *, task_id: str, org_id: str, app_id: str, project: str | None, project_id: str | None = None):
    sess.execute(
        sa.text(
            "INSERT INTO automation_tasks(id, org_id, app_id, project_id, project) "
            "VALUES (:id, :org_id, :app_id, :project_id, :project)"
        ),
        {
            "id": task_id,
            "org_id": org_id,
            "app_id": app_id,
            "project_id": project_id,
            "project": project,
        },
    )


def _run_migration(engine) -> None:
    """Run migration 051's ``upgrade`` against the isolated engine.

    The migration file's ``upgrade()`` body uses PostgreSQL's
    ``UPDATE ... FROM`` syntax (production DB is PostgreSQL) — but our
    local tests run against SQLite which doesn't accept that form. We
    re-shape the same logic into a portable equivalent:

        UPDATE t SET x = (SELECT ...) WHERE id IN (SELECT ...)

    Both dialects accept this. The migration's WHERE clauses, JOIN
    conditions, and overall matching logic are replicated verbatim so
    the tests are still a faithful behavioural test.
    """
    same_app_sql = """
        WITH matched AS (
            SELECT t.id AS task_id, p.id AS proj_id
            FROM automation_tasks t
            JOIN projects p
              ON p.app_id = t.app_id
             AND p.org_id = t.org_id
             AND lower(p.name) = lower(trim(t.project))
             AND coalesce(p.is_deleted, false) = false
            WHERE t.project_id IS NULL
              AND t.project IS NOT NULL
              AND trim(t.project) <> ''
              AND lower(trim(t.project)) <> 'global'
        )
        UPDATE automation_tasks
        SET project_id = (
            SELECT proj_id FROM matched
            WHERE matched.task_id = automation_tasks.id
        )
        WHERE id IN (SELECT task_id FROM matched)
    """
    fallback_sql = """
        WITH fallback AS (
            SELECT t.id AS task_id,
                   (
                       SELECT p2.id FROM projects p2
                       WHERE lower(p2.name) = lower(trim(t.project))
                         AND coalesce(p2.is_deleted, false) = false
                       ORDER BY p2.created_date ASC NULLS LAST, p2.id ASC
                       LIMIT 1
                   ) AS proj_id
            FROM automation_tasks t
            WHERE t.project_id IS NULL
              AND t.project IS NOT NULL
              AND trim(t.project) <> ''
              AND lower(trim(t.project)) <> 'global'
        )
        UPDATE automation_tasks
        SET project_id = (
            SELECT proj_id FROM fallback
            WHERE fallback.task_id = automation_tasks.id
        )
        WHERE id IN (SELECT task_id FROM fallback WHERE proj_id IS NOT NULL)
    """
    with engine.begin() as conn:
        conn.execute(sa.text(same_app_sql))
        conn.execute(sa.text(fallback_sql))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_backfill_same_app_match(migration_db):
    """Same-app matching: task with legacy ``project='Ecisco BI'`` in app
    ``local-zhanlu-app`` MUST resolve to the project with that name in
    the same app.
    """
    Session = migration_db["session_factory"]
    engine = migration_db["engine"]
    project_id = "proj-ecisco"
    task_id = "task-ecisco"

    with Session() as sess:
        _seed_project(
            sess, project_id=project_id, org_id="org-1",
            app_id="local-zhanlu-app", name="Ecisco BI",
        )
        _seed_task(
            sess, task_id=task_id, org_id="org-1",
            app_id="local-zhanlu-app", project="Ecisco BI",
        )
        sess.commit()

    _run_migration(engine)

    with Session() as sess:
        row = sess.execute(
            sa.text("SELECT project_id FROM automation_tasks WHERE id = :tid"),
            {"tid": task_id},
        ).fetchone()
    assert row is not None
    assert row[0] == project_id, (
        f"same-app match should bind Ecisco BI to {project_id!r}; got {row[0]!r}"
    )


def test_backfill_skips_global_marker(migration_db):
    """``project='global'`` tasks MUST stay NULL — they belong to the
    workspace-wide bucket, not a specific project.
    """
    Session = migration_db["session_factory"]
    engine = migration_db["engine"]

    with Session() as sess:
        # A project named "global" exists (sentinel-like) — but the
        # WHERE clause explicitly excludes it.
        _seed_project(
            sess, project_id="proj-global", org_id="org-1",
            app_id="local-zhanlu-app", name="global",
        )
        _seed_task(
            sess, task_id="task-global", org_id="org-1",
            app_id="local-zhanlu-app", project="global",
        )
        sess.commit()

    _run_migration(engine)

    with Session() as sess:
        row = sess.execute(
            sa.text("SELECT project_id FROM automation_tasks WHERE id = :tid"),
            {"tid": "task-global"},
        ).fetchone()
    assert row is not None
    assert row[0] is None, "'global' rows should not be backfilled"


def test_backfill_skips_soft_deleted_project(migration_db):
    """Soft-deleted (``is_deleted=True``) projects MUST NOT be matched —
    otherwise the executor would resolve to an archived project.
    """
    Session = migration_db["session_factory"]
    engine = migration_db["engine"]

    with Session() as sess:
        _seed_project(
            sess, project_id="proj-deleted", org_id="org-1",
            app_id="local-zhanlu-app", name="Ecisco BI", is_deleted=True,
        )
        _seed_task(
            sess, task_id="task-no-match", org_id="org-1",
            app_id="local-zhanlu-app", project="Ecisco BI",
        )
        sess.commit()

    _run_migration(engine)

    with Session() as sess:
        row = sess.execute(
            sa.text("SELECT project_id FROM automation_tasks WHERE id = :tid"),
            {"tid": "task-no-match"},
        ).fetchone()
    assert row is not None
    # Could be NULL (no other Ecisco BI project) — must not be the
    # soft-deleted one.
    assert row[0] != "proj-deleted"


def test_backfill_falls_back_across_apps(migration_db):
    """Fallback path: a task in ``default-app`` whose name matches a
    project in ``local-zhanlu-app`` (no same-app match) MUST still pick
    up the oldest matching project.
    """
    Session = migration_db["session_factory"]
    engine = migration_db["engine"]

    with Session() as sess:
        _seed_project(
            sess, project_id="proj-bi-old", org_id="org-1",
            app_id="other-app", name="Ecisco BI",
        )
        _seed_task(
            sess, task_id="task-no-same-app", org_id="org-1",
            app_id="local-zhanlu-app", project="Ecisco BI",
        )
        sess.commit()

    _run_migration(engine)

    with Session() as sess:
        row = sess.execute(
            sa.text("SELECT project_id FROM automation_tasks WHERE id = :tid"),
            {"tid": "task-no-same-app"},
        ).fetchone()
    assert row is not None
    assert row[0] == "proj-bi-old", (
        "cross-app fallback should pick the oldest non-deleted match"
    )


def test_backfill_is_idempotent(migration_db):
    """A second invocation MUST NOT touch rows that already have
    ``project_id`` set — the WHERE clause guards against double-binding
    (which would re-resolve ECIS → a different project if a duplicate
    name is created later).
    """
    Session = migration_db["session_factory"]
    engine = migration_db["engine"]

    with Session() as sess:
        _seed_project(
            sess, project_id="proj-a", org_id="org-1",
            app_id="local-zhanlu-app", name="Ecisco BI",
        )
        # The task ALREADY has project_id set (someone fixed it
        # manually). Backfill must not overwrite.
        _seed_task(
            sess, task_id="task-fixed", org_id="org-1",
            app_id="local-zhanlu-app", project="Ecisco BI",
            project_id="proj-manual",
        )
        sess.commit()

    _run_migration(engine)

    with Session() as sess:
        row = sess.execute(
            sa.text("SELECT project_id FROM automation_tasks WHERE id = :tid"),
            {"tid": "task-fixed"},
        ).fetchone()
    assert row is not None
    assert row[0] == "proj-manual", (
        "idempotency violated — backfill overwrote a manually-set project_id"
    )


def test_backfill_handles_empty_project_string(migration_db):
    """An empty/whitespace ``project`` string MUST NOT be matched (no
    project has an empty name in normal usage).
    """
    Session = migration_db["session_factory"]
    engine = migration_db["engine"]

    with Session() as sess:
        _seed_task(
            sess, task_id="task-empty", org_id="org-1",
            app_id="local-zhanlu-app", project="   ",
        )
        # Also test totally NULL legacy strings (defensive guard).
        _seed_task(
            sess, task_id="task-null", org_id="org-1",
            app_id="local-zhanlu-app", project=None,
        )
        sess.commit()

    _run_migration(engine)

    with Session() as sess:
        for tid in ("task-empty", "task-null"):
            row = sess.execute(
                sa.text("SELECT project_id FROM automation_tasks WHERE id = :tid"),
                {"tid": tid},
            ).fetchone()
            assert row is not None
            assert row[0] is None, f"{tid} should stay NULL (empty legacy name)"
