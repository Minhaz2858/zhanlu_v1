"""turn_identity — durable turn ordinals, idempotent claims, message versioning.

Revision ID: 102_turn_identity
Revises: 101_cad_revisions
Create Date: 2026-10-27

WHY
---
Stage 2B (docs/plans/2026-10-27-agent-turn-identity-successor-safety-design.md Rev 4)
needs three durable facts that did not exist:

  1. a monotonically increasing per-conversation turn ordinal, so a *newer*
     generation can supersede an older one regardless of which finishes first;
  2. a durable (conversation_id, request_id) -> ordinal mapping, so a retried
     logical request is idempotent instead of allocating a second ordinal;
  3. a per-message version counter for optimistic concurrency, so a client's
     blind PUT can no longer overwrite a newer committed answer.

This migration is ADDITIVE ONLY. It never rewrites existing message content.

RLS
---
``agent_turn_claims`` carries ``org_id`` and gets the SAME forced RLS policy as
every other tenant table, so a claim cannot be read or written across tenants.
Without the policy the application role (``zhanlu_app``, not BYPASSRLS) would be
unable to use the table at all.

BACKFILL
--------
Implicit, and deliberately so:

  * ``chat_messages.committed_turn_seq`` stays NULL -> "no turn has committed a
    canonical answer to this row". The first new turn (ordinal >= 1) therefore
    always satisfies the "newer than the row" predicate.
  * ``agent_conversations.current_turn_seq`` defaults to 0 -> no claim yet.
  * ``agent_turn_seq`` starts at 1 -> the first claim receives ordinal 1.
  * ``chat_messages.version`` defaults to 0 for every existing row.

No content is modified, so this migration is safe to run on production data and
is a no-op for any reader that has not been upgraded.

DOWNGRADE
---------
Drops everything this migration created, restoring the pre-migration schema.
Dropping the columns discards turn ordinals/versions only; message content is
untouched. A message whose content was written under the new protocol keeps that
content - only its concurrency metadata is lost.
"""

from alembic import op
import sqlalchemy as sa

revision = "102_turn_identity"
down_revision = "101_cad_revisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── per-message concurrency metadata (additive, defaulted) ──────────
    op.add_column(
        "chat_messages",
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "chat_messages",
        sa.Column("committed_turn_seq", sa.BigInteger(), nullable=True),
    )

    # ── per-conversation current claim pointer ──────────────────────────
    op.add_column(
        "agent_conversations",
        sa.Column("current_turn_seq", sa.BigInteger(),
                  nullable=False, server_default="0"),
    )

    # ── ordinal allocator ──────────────────────────────────────────────
    op.execute("CREATE SEQUENCE IF NOT EXISTS agent_turn_seq START 1")

    # ── durable request -> ordinal mapping (the idempotency authority) ──
    op.create_table(
        "agent_turn_claims",
        sa.Column("conversation_id", sa.String(36), primary_key=True,
                  nullable=False),
        sa.Column("request_id", sa.String(64), primary_key=True,
                  nullable=False),
        sa.Column("turn_seq", sa.BigInteger(), nullable=False),
        sa.Column("assistant_message_id", sa.String(36), nullable=True),
        # Tenant columns — required by the forced RLS policy below.
        sa.Column("org_id", sa.String(36), nullable=False,
                  server_default="default-org"),
        sa.Column("app_id", sa.String(36), nullable=False,
                  server_default="default-app"),
        sa.Column("created_date", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_agent_turn_claims_seq", "agent_turn_claims",
        ["conversation_id", "turn_seq"],
    )
    op.create_index(
        "ix_agent_turn_claims_org_id", "agent_turn_claims", ["org_id"],
    )

    # Same tenant policy as every other table (see 095_procurement_po_drafts).
    op.execute("ALTER TABLE agent_turn_claims ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE agent_turn_claims FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON agent_turn_claims USING ("
        "  coalesce(current_setting('app.current_org', true), '') = ''"
        "  OR org_id = current_setting('app.current_org', true)"
        ")"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON agent_turn_claims")
    op.drop_index("ix_agent_turn_claims_org_id", table_name="agent_turn_claims")
    op.drop_index("ix_agent_turn_claims_seq", table_name="agent_turn_claims")
    op.drop_table("agent_turn_claims")
    op.execute("DROP SEQUENCE IF EXISTS agent_turn_seq")
    op.drop_column("agent_conversations", "current_turn_seq")
    op.drop_column("chat_messages", "committed_turn_seq")
    op.drop_column("chat_messages", "version")
