"""118_kb_multi_project — one connector shared by multiple projects.

Revision ID: 118_kb_multi_project
Revises: 115_observation_evidence
Create Date: 2026-10-31

HISTORY NOTE: originally a merge of the two 115 heads
(``115_observation_evidence`` + ``115_process_review_lifecycle``). The
enterprise Phase-3E/4 commits (2026-11-02) replaced that second branch —
``115_process_review_lifecycle`` no longer exists and the surviving branch
was renamed to ``115_observation_evidence_ids.py`` — and chained
``119_business_rule`` on top of THIS revision. The merge was therefore
flattened to a single parent, or Alembic could not build the graph and the
backend crash-looped on startup.

WHY
---
The single ``knowledge_bases.project_id`` FK enforced "one database → one
project": attaching a KB to a second project overwrote the first binding.
Users need one datasource (MySQL / PostgreSQL / JSON / SQLite connector) to be
usable by multiple projects at the same time.

CHANGE (additive only)
----------------------
1. New link table ``project_knowledge_bases`` (mirrors ``project_agents``):
   (project_id, knowledge_base_id) membership rows, org/app-scoped, with the
   same forced tenant-isolation RLS policy as every other tenant table.
   Unique constraint makes attach idempotent.
2. Backfill: every KB with a non-null ``project_id`` gets one "home" link row,
   so the legacy home pointer and the link table agree from day one.
3. ``KnowledgeBase.project_id`` KEEPS its meaning (the KB's home project);
   runtime reads switch to "home OR legacy name OR linked" via a shared
   membership filter — nothing existing changes behavior.

Safe on production data: no existing row is modified or removed.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import uuid

revision: str = "118_kb_multi_project"
down_revision: Union[str, Sequence[str], None] = "115_observation_evidence"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _backfill_home_links(conn) -> int:
    """One 'home' link row per KB that already has a project_id (idempotent)."""
    rows = conn.execute(sa.text(
        "SELECT id, project_id, org_id, app_id, created_by_id "
        "FROM knowledge_bases WHERE project_id IS NOT NULL"
    )).fetchall()
    count = 0
    for kb_id, project_id, org_id, app_id, created_by_id in rows:
        exists = conn.execute(
            sa.text(
                "SELECT 1 FROM project_knowledge_bases "
                "WHERE project_id = :pid AND knowledge_base_id = :kid"
            ),
            {"pid": project_id, "kid": kb_id},
        ).fetchone()
        if exists:
            continue
        conn.execute(
            sa.text("""
                INSERT INTO project_knowledge_bases
                    (id, project_id, knowledge_base_id, role,
                     created_by_id, created_date, updated_date,
                     org_id, app_id)
                VALUES
                    (:id, :pid, :kid, 'home',
                     :cby, NOW(), NOW(),
                     :org, :app)
            """),
            {
                "id": str(uuid.uuid4()), "pid": project_id, "kid": kb_id,
                "cby": created_by_id,
                "org": org_id or "default-org", "app": app_id or "default-app",
            },
        )
        count += 1
    return count


def upgrade() -> None:
    op.create_table(
        "project_knowledge_bases",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id", sa.String(36),
            sa.ForeignKey("projects.id"), nullable=False, index=True,
        ),
        sa.Column(
            "knowledge_base_id", sa.String(36),
            sa.ForeignKey("knowledge_bases.id"), nullable=False, index=True,
        ),
        sa.Column("role", sa.String(20), nullable=True, server_default="member"),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        # Tenant columns — required by the forced RLS policy below.
        sa.Column("org_id", sa.String(36), nullable=False,
                  server_default="default-org"),
        sa.Column("app_id", sa.String(36), nullable=False,
                  server_default="default-app"),
        sa.Column("created_date", sa.DateTime(), nullable=True),
        sa.Column("updated_date", sa.DateTime(), nullable=True),
        # TimestampedBase also carries is_deleted — the startup ensure-columns
        # hook ADDs it with NOT NULL, which fails on a table that already has
        # rows if the column is missing here (observed on first boot).
        sa.Column("is_deleted", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
        sa.UniqueConstraint(
            "project_id", "knowledge_base_id", "org_id", "app_id",
            name="uq_project_kbs_project_kb_org_app",
        ),
    )
    op.create_index(
        "ix_project_kbs_kb", "project_knowledge_bases", ["knowledge_base_id"],
    )

    # Same tenant policy as every other table (see 102_turn_identity).
    op.execute("ALTER TABLE project_knowledge_bases ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE project_knowledge_bases FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON project_knowledge_bases USING ("
        "  coalesce(current_setting('app.current_org', true), '') = ''"
        "  OR org_id = current_setting('app.current_org', true)"
        ")"
    )

    conn = op.get_bind()
    _backfill_home_links(conn)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON project_knowledge_bases")
    op.drop_index("ix_project_kbs_kb", table_name="project_knowledge_bases")
    op.drop_table("project_knowledge_bases")
