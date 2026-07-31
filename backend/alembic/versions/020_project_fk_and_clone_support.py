"""020_project_fk_and_clone_support

Revision ID: 020
Revises: 017
Create Date: 2026-07-20

Convert loose ``project`` string columns into a proper ``project_id``
ForeignKey across 5 tables, add ``source_market_agent_id`` to agent_apps
(for the clone-to-my-space flow), and backfill existing rows into a
per-user "Global" project.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import uuid

revision: str = "020"
down_revision: Union[str, None] = "017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _ensure_global_project(conn, user_id: str, org_id: str, app_id: str) -> str:
    """Return existing or newly-created 'Global' project id for *user_id*."""
    row = conn.execute(
        sa.text(
            "SELECT id FROM projects WHERE name = 'Global' AND created_by_id = :uid LIMIT 1"
        ),
        {"uid": user_id},
    ).fetchone()
    if row:
        return row[0]

    pid = str(uuid.uuid4())
    conn.execute(
        sa.text("""
            INSERT INTO projects
                (id, name, description, color, status,
                 created_by_id, created_date, updated_date,
                 org_id, app_id, is_deleted)
            VALUES
                (:pid, 'Global', 'Default project for unscoped work',
                 '#6B7280', 'active',
                 :uid, NOW(), NOW(),
                 :org, :app, false)
        """),
        {"pid": pid, "uid": user_id, "org": org_id or "default-org", "app": app_id or "default-app"},
    )
    return pid


def _backfill_table(conn, table: str, old_col: str):
    """Backfill ``project_id`` from the old string column.

    For each distinct user, ensure a "Global" project exists, then map
    rows: exact match by Project.name → fallback to Global.
    """
    # 1 — collect distinct users and create Global projects
    user_rows = conn.execute(
        sa.text(f"""
            SELECT DISTINCT t.created_by_id, t.org_id, t.app_id
            FROM {table} t
            WHERE t.created_by_id IS NOT NULL
              AND t.project_id IS NULL
        """)
    ).fetchall()

    global_cache: dict[str, str] = {}
    for uid, org, app in user_rows:
        if uid not in global_cache:
            global_cache[uid] = _ensure_global_project(conn, uid, org, app)

    # 2 — exact name match → set project_id to matched project
    conn.execute(sa.text(f"""
        UPDATE {table}
        SET project_id = (
            SELECT p.id FROM projects p
            WHERE p.name = {table}.{old_col}
              AND p.created_by_id = {table}.created_by_id
        )
        WHERE {table}.{old_col} IS NOT NULL
          AND {table}.{old_col} != ''
          AND {table}.{old_col} != 'global'
          AND {table}.project_id IS NULL
    """))

    # 3 — remaining NULL rows → fallback to Global
    for uid, gpid in global_cache.items():
        conn.execute(
            sa.text(f"""
                UPDATE {table}
                SET project_id = :gpid
                WHERE created_by_id = :uid
                  AND project_id IS NULL
            """),
            {"gpid": gpid, "uid": uid},
        )


def upgrade() -> None:
    conn = op.get_bind()

    # ── Phase 1: add nullable project_id columns (keep old string cols) ──
    tables_to_add = [
        ("agent_apps",        "project"),
        ("automation_tasks",  "project"),
        ("chat_sessions",     "project"),
        ("user_files",        "project"),
        ("knowledge_bases",   "project"),
    ]
    for table, _ in tables_to_add:
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(sa.Column("project_id", sa.String(36), nullable=True))

    # source_market_agent_id on agent_apps only
    with op.batch_alter_table("agent_apps") as batch_op:
        batch_op.add_column(sa.Column("source_market_agent_id", sa.String(36), nullable=True))

    # ── Phase 2: backfill ──
    for table, old_col in tables_to_add:
        _backfill_table(conn, table, old_col)

    # ── Phase 3: drop old string column, add FK + indexes ──
    for table, _ in tables_to_add:
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_column("project")

    with op.batch_alter_table("agent_apps") as batch_op:
        batch_op.create_foreign_key("fk_agent_app_project", "projects", ["project_id"], ["id"])
        batch_op.create_index("ix_agent_app_project_id", ["project_id"])
        batch_op.create_index("ix_agent_app_source_market", ["source_market_agent_id"])

    for table in ("automation_tasks", "chat_sessions", "user_files", "knowledge_bases"):
        with op.batch_alter_table(table) as batch_op:
            batch_op.create_foreign_key(f"fk_{table}_project", "projects", ["project_id"], ["id"])
            batch_op.create_index(f"ix_{table}_project_id", ["project_id"])


def downgrade() -> None:
    # ── Reverse: drop FK, indexes, project_id col; restore old string col ──
    for table in ("agent_apps", "automation_tasks", "chat_sessions", "user_files", "knowledge_bases"):
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_constraint(f"fk_{table}_project", type_="foreignkey")
            batch_op.drop_index(f"ix_{table}_project_id")

    with op.batch_alter_table("agent_apps") as batch_op:
        batch_op.drop_index("ix_agent_app_source_market")
        batch_op.drop_column("source_market_agent_id")

    for table in ("agent_apps", "automation_tasks", "chat_sessions", "user_files", "knowledge_bases"):
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_column("project_id")

    for table, old_col in (
        ("agent_apps", "project"),
        ("automation_tasks", "project"),
        ("chat_sessions", "project"),
        ("user_files", "project"),
        ("knowledge_bases", "project"),
    ):
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(sa.Column(old_col, sa.String(255), nullable=True))
