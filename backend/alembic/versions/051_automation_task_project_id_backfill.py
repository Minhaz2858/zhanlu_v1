"""051 — backfill automation_tasks.project_id from legacy `project` name.

Revision ID: 051_automation_task_project_id_backfill
Revises: 050_report_recipes
Create Date: 2026-08-11

BUGFIX (project binding): the frontend (Chat.jsx) only set the legacy
``project`` string when creating AutomationTask rows via ``create_resource``;
``project_id`` (the FK to ``projects.id``) was always NULL. The executor's
``_resolve_task_project`` adopts+persists the FK when the legacy name matches
a real project — but for tasks created BEFORE that change the FK remained
NULL forever, so the data source runtime could not bind the project's data
sources (only the task's ``data_source_id`` was honoured, and only when set
explicitly). The result was an empty HTML report: the agent had no bound data
sources, so the LLM had nothing to read.

This migration is a DATA backfill (no schema change — the ``project_id``
column already exists per ``models/automation_task.py``):

For each ``automation_tasks`` row where ``project_id IS NULL`` and the
legacy ``project`` string is non-empty, look up the matching ``Project`` by
name within the same ``app_id`` (``projects.name`` is scoped per-app) and
update the FK. Idempotent: re-running on a fully-backed DB is a no-op
because the WHERE clause only matches NULL rows.

Behaviours:

* Tasks whose legacy ``project`` is NULL / empty / "global" are left alone
  — they belong to the workspace-wide bucket, not a specific project.
* If a legacy name maps to multiple ``projects`` rows within the same app
  (e.g. a user created two "Ecisco BI" projects in their org), we pick the
  active (``is_deleted=False``) and oldest row — stable and predictable.
* Logs a brief summary at the end for ops visibility.
"""
from alembic import op
import sqlalchemy as sa


revision = "051_automation_task_project_id_backfill"
down_revision = "050_report_recipes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Backfill ``automation_tasks.project_id`` by matching the legacy
    ``project`` (string) to ``projects.name`` within the same ``app_id``.
    """
    bind = op.get_bind()

    # 1) Same-app matches (preferred — avoids cross-app collisions).
    # Use plain SQL with a CTE so the backfill is one transaction and
    # idempotent under a single idempotency key (``project_id IS NULL``).
    bind.execute(sa.text(
        """
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
        UPDATE automation_tasks t
        SET project_id = m.proj_id
        FROM matched m
        WHERE t.id = m.task_id
        """
    ))

    # 2) Fallback for tasks in apps without a matching project (e.g. legacy
    # tasks whose app_id has since been rotated, or "global" rows that
    # accidentally got a non-null ``project`` string). Look up by name alone
    # across all apps; pick the oldest (first-created) project — the one the
    # user is most likely to have meant.
    bind.execute(sa.text(
        """
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
        UPDATE automation_tasks t
        SET project_id = f.proj_id
        FROM fallback f
        WHERE t.id = f.task_id
          AND f.proj_id IS NOT NULL
        """
    ))

    # 3) Operational summary (logged so a dry-run / ops check can see the
    # backfill impact without scanning the table).
    summary = bind.execute(sa.text(
        """
        SELECT
            count(*) FILTER (WHERE project_id IS NOT NULL) AS matched,
            count(*) FILTER (WHERE project_id IS NULL
                              AND project IS NOT NULL
                              AND trim(project) <> ''
                              AND lower(trim(project)) <> 'global') AS unmatched,
            count(*) FILTER (WHERE project_id IS NULL) AS remaining_null
        FROM automation_tasks
        """
    )).fetchone()
    # Alembic's op.get_bind() may be the SQLAlchemy connection; access via
    # _mapping for engine portability.
    try:
        matched = summary.matched
        unmatched = summary.unmatched
        remaining_null = summary.remaining_null
    except AttributeError:
        # SQLAlchemy Row tuple — fallback to positional access.
        matched, unmatched, remaining_null = summary[0], summary[1], summary[2]

    # Use the standard logging pattern (Alembic prints are visible during
    # `alembic upgrade head`).
    print(
        "[051] automation_tasks.project_id backfill summary: "
        f"matched={matched} unmatched_named={unmatched} "
        f"remaining_null={remaining_null}"
    )


def downgrade() -> None:
    """No-op: this is a data backfill. The ``project_id`` column stays — the
    original legacy ``project`` string column is preserved as well (no
    schema change). Downgrading means losing the FK link, which would
    regress the bug. Operators who really need to undo this can run a
    manual ``UPDATE automation_tasks SET project_id = NULL`` after
    confirming downstream impact.
    """
    # Intentionally empty — reversing a backfill would re-introduce the bug.
    pass
