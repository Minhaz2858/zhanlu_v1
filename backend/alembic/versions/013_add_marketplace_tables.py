"""Add marketplace_skills and marketplace_ratings tables for Phase 1 public marketplace.

Revision ID: 013
Revises: 012
Create Date: 2025-07-15

- marketplace_skills: community-published skills with ratings, signatures, verification
- marketplace_ratings: individual user ratings (1-5) per skill
"""

from alembic import op
import sqlalchemy as sa


revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "marketplace_skills",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime(), nullable=False),
        sa.Column("updated_date", sa.DateTime(), nullable=False),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("org_id", sa.String(36), nullable=False, server_default="default-org"),
        sa.Column("app_id", sa.String(36), nullable=False, server_default="default-app"),
        # Skill content
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("summary", sa.String(500), nullable=True),
        sa.Column("category", sa.String(100), nullable=True),
        sa.Column("version", sa.String(50), nullable=False, server_default="1.0.0"),
        sa.Column("publisher_id", sa.String(36), nullable=True),
        sa.Column("publisher_name", sa.String(255), nullable=True),
        sa.Column("skill_md", sa.Text(), nullable=True),
        sa.Column("github_url", sa.String(500), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=True),
        # Metrics
        sa.Column("download_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("avg_rating", sa.Float(), nullable=True),
        sa.Column("ratings_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        # Verification
        sa.Column("signature", sa.Text(), nullable=True),
        sa.Column("is_verified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_published", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("author_email", sa.String(255), nullable=True),
    )
    op.create_index("ix_marketplace_skills_name", "marketplace_skills", ["name"])
    op.create_index("ix_marketplace_skills_publisher_id", "marketplace_skills", ["publisher_id"])

    op.create_table(
        "marketplace_ratings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_date", sa.DateTime(), nullable=False),
        sa.Column("updated_date", sa.DateTime(), nullable=False),
        sa.Column("created_by_id", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("org_id", sa.String(36), nullable=False, server_default="default-org"),
        sa.Column("app_id", sa.String(36), nullable=False, server_default="default-app"),
        sa.Column("marketplace_skill_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=True),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("review", sa.String(1000), nullable=True),
    )
    op.create_index(
        "ix_marketplace_ratings_skill_id", "marketplace_ratings", ["marketplace_skill_id"]
    )
    op.create_index("ix_marketplace_ratings_user_id", "marketplace_ratings", ["user_id"])


def downgrade() -> None:
    op.drop_table("marketplace_ratings")
    op.drop_table("marketplace_skills")
