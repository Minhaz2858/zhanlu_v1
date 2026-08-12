"""ReportRecipe model — first-class reusable report templates.

A recipe bundles the SQL queries, chart specs, section layout, and
validation rules for a recurring report (sales / weekly / monthly /
inventory / customer). The runner executes recipes deterministically;
the LLM never decides what SQL to run.

SQL bundles run read-only via QueryService (never through the sandbox).
Charts/artifacts run via the sandbox. Validation rules enforce
post-execution quality gates (non-empty, row-count threshold, etc.).
"""

from __future__ import annotations

from sqlalchemy import JSON, Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class ReportRecipe(TimestampedBase):
    __tablename__ = "report_recipes"

    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    project_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True,
        comment="NULL = global recipe available to all projects",
    )

    # Metric references — resolved via MetricDefinition at run time
    required_metrics: Mapped[list | None] = mapped_column(JSON, nullable=True)
    optional_dimensions: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Direct SQL bundle: [{key, sql, params, kb_id}] (kb_id optional;
    # if absent, the runner uses the project's first bound DB KB)
    sql_bundle: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Chart specs: [{type, source_key, x, y, title}] — rendered via sandbox
    charts: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Section layout: [{title, source_key | template}] — assembled into output
    sections: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Validation rules: [{rule, params}] — enforced post-execution
    validation_rules: Mapped[list | None] = mapped_column(JSON, nullable=True)

    output_format: Mapped[str] = mapped_column(
        String(20), nullable=False, default="markdown"
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
