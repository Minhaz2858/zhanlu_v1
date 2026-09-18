"""QueryPlanMemory — a verified question→plan mapping, reused across turns.

Why this exists: nothing in the platform remembered HOW a question was answered.
Every turn re-selected tables and re-wrote SQL from scratch, so "contract
execution status" resolved to ``FCLOSESTATUS`` on one run and
``FDOCUMENTSTATUS`` on the next — both plausible columns, only one right, and
two different numbers for the same business question.

What is stored is a PLAN, never an answer: the tables that answered the
question, the SQL shape that worked, the dimensions it grouped by. Rows are
still read live on every turn, and any relative period ("last month") is
re-resolved against today's date. The memory removes run-to-run drift; it does
not cache data.

Follows the metadata / create_all convention of ``knowledge_catalog.py`` — the
table is created on backend startup, no alembic revision required.
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class QueryPlanMemory(TimestampedBase):
    __tablename__ = "query_plan_memory"

    # The knowledge base whose catalog produced the plan. Plans are per-KB by
    # definition: table names and columns differ per database.
    kb_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # sha256 of the normalised question (see plan_memory.question_signature).
    signature: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # The original phrasing, so a human can see what this plan answers.
    question: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Tables the plan resolved to, in the order the model selected them.
    tables: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # The SQL that produced a verified answer. Reused as a SHAPE hint; the
    # period is always re-resolved live.
    sql: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Dimensions the working SQL grouped by (stops grain drift).
    dimensions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # How many turns reused this plan — 0 means it has only ever been written.
    hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Last time an answer built on this plan passed the answer contract.
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("kb_id", "signature", name="uq_query_plan_memory"),
    )
