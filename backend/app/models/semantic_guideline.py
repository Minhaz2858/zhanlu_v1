"""SemanticGuideline — authored organizational artifacts for the semantic store
(OCI offline enrichment, organizational level).

The blog's organizational enrichment converts tribal knowledge into explicit
constraints: fiscal-calendar rules, territory/org mappings, default filters,
exception handling, policy constraints. These are NOT schema facts (the
catalog indexer owns those) and NOT business vocabulary (Terminology /
MetricDefinition own those) — they are RULES the SQL generation must respect.

Each guideline is datasource-scoped, carries a kind from a fixed vocabulary,
and is injected into the NL2SQL prompt as a BUSINESS RULES block (and served
through the SemanticStore for any other consumer).
"""

from typing import Optional

from sqlalchemy import String, Boolean, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase

#: The kinds of organizational knowledge this store holds. Fixed vocabulary —
#: the prompt injector renders each kind with its own instruction.
GUIDELINE_KINDS = [
    "fiscal_calendar",   # fiscal year/month definitions, week start, cutoffs
    "default_filter",    # always-applied filters (exclude cancelled, test data)
    "territory_map",     # region/org/territory mappings
    "exception",         # known exceptions to normal semantics
    "policy",            # reporting policy constraints
    "other",             # uncategorized business rule
]


class SemanticGuideline(TimestampedBase):
    """One authored business rule scoped to a datasource (KB)."""

    __tablename__ = "semantic_guidelines"

    datasource_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False, default="other")
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
