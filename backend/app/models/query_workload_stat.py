"""QueryWorkloadStat — mined behavioral signals from the NL2SQL query logs.

The persisted home for the OCI "behavioral level" enrichment: what the
business ACTUALLY asked, mined offline from successful executions. The
SemanticStore facade stays read-only — it READS this table but never writes
it; the miner (services/knowledge_graph/workload_miner.py) is the only
writer. Follows the metadata / create_all convention of knowledge_catalog.py.
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class QueryWorkloadStat(TimestampedBase):
    __tablename__ = "query_workload_stats"

    # The knowledge base (data source) the mined statements ran against.
    kb_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # 'table_pair' (co-joined tables) | 'table_filter' (common WHERE literal
    # conjunct) | 'popular_table' (statement frequency) | 'rewrite_outcome'
    # (execution_feedback: rules applied + estimated rows for one rewrite).
    stat_kind: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    # The subject table (one side of a pair; the filtered/popular table).
    table_name: Mapped[str] = mapped_column(String(256), nullable=False)
    # The other side of a 'table_pair' (normalized so a < b); NULL otherwise.
    peer: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # Kind-specific payload, e.g. {"filter": "status = 'open'"} for
    # table_filter rows. Bounded: literals are truncated by the miner.
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # How many logged statements contributed (accumulated across runs).
    occurrences: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Newest contributing execution.
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Stable identity of the stat (kind|table|peer|detail hash) — the upsert key.
    stat_key: Mapped[str] = mapped_column(String(64), nullable=False)
    # Free-text provenance for operators.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("kb_id", "stat_key", name="uq_query_workload_stat"),
    )
