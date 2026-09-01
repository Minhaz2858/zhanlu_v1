"""Dashboard — a live, schema-driven dashboard bound to one database KnowledgeBase.

A dashboard stores a JSON ``definition`` of widgets (KPI / line / bar / pie /
table), each with its own read-only SQL. At view time the frontend polls
``POST /api/dashboards/{id}/query`` which runs every widget's SQL against the
bound datasource via the existing ``QueryService`` (read-only, row-capped,
timeout-bounded). One dashboard binds to exactly ONE database KnowledgeBase.
"""
from typing import Optional

from sqlalchemy import String, Integer, JSON, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class Dashboard(TimestampedBase):
    __tablename__ = "dashboards"

    # Dual project binding (id-OR-name convention used across zhanlu).
    project_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("projects.id"), nullable=True, index=True
    )
    project: Mapped[str] = mapped_column(String(200), default="global", nullable=False)

    # The ONE database KnowledgeBase this dashboard queries.
    datasource_kb_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("knowledge_bases.id"), nullable=False, index=True
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # {"widgets": [{id, type, title, sql, options}]}
    definition: Mapped[dict] = mapped_column(JSON, nullable=False)

    refresh_interval_seconds: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
