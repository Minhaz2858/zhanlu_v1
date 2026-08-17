"""ResourceAccessPolicy model — per-user, per-KB, per-table data access rules.

Sits on top of the existing ``ResourceShare`` system.  A ``ResourceShare``
grants a user *access to* a project/agent (view + use); a
``ResourceAccessPolicy`` *narrows* that access by restricting which databases
(KnowledgeBases) and which tables within those databases the shared user may
use.

Semantics
---------
**Default = allow all.**  When no policy rows exist for a (user, resource)
tuple, the shared user sees every KB and table the share grants them.  Policies
only apply when the owner explicitly creates them.

**Specificity-based resolution** (most specific wins)::

    (kb_id + table_name)  >  (kb_id only)  >  (table only)  >  (global)

- A table-level ``deny`` overrides a KB-level ``allow``.
- A table-level ``allow`` overrides a KB-level ``deny``.
- A KB-level ``deny`` (``kb_id`` set, ``table_name`` NULL) hides the whole KB.
- A global ``deny`` (``kb_id`` NULL, ``table_name`` NULL) hides every KB.

**Modes**:

- ``allow``           — the target (KB or table) is explicitly permitted.
- ``deny``            — the target is hidden/blocked.
- ``allow_columns``   — only the columns in ``column_allowlist`` are visible
  and queryable for the target table (requires ``table_name`` set).

``created_by_id`` (inherited from ``TimestampedBase``) is the owner/admin who
configured the policy — *not* the shared user.  ``user_id`` identifies the
shared user the policy constrains.
"""

from typing import Optional

from sqlalchemy import String, ForeignKey, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class ResourceAccessPolicy(TimestampedBase):
    # NOTE: uniqueness on (resource_share_id, kb_id, table_name) is enforced by
    # a *partial* unique index in migration 058 (``WHERE is_deleted = false``),
    # NOT by a model-level UniqueConstraint.  A full unique constraint would
    # conflict when the batch-upsert endpoint soft-deletes old rows and re-inserts
    # the same (kb_id, table_name).  See access_policies.py for the upsert logic.
    __tablename__ = "resource_access_policies"

    # The share this policy narrows.  Cascade-deleted (soft) when share revoked.
    resource_share_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("resource_shares.id"), nullable=False, index=True,
        doc="UUID of the ResourceShare this policy constrains.",
    )

    # Denormalized resource target (mirrors the ResourceShare for cheap lookups).
    resource_type: Mapped[str] = mapped_column(
        String(20), nullable=False, index=True,
        doc="Entity type: 'project' or 'agent'.",
    )
    resource_id: Mapped[str] = mapped_column(
        String(36), nullable=False, index=True,
        doc="UUID of the shared resource (projects.id or agent_apps.id).",
    )

    # The shared user being constrained.
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False, index=True,
        doc="User whose access this policy constrains.",
    )

    # Granularity: None = all KBs / all tables (global-level).
    kb_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, index=True,
        doc="KnowledgeBase id; NULL = all KBs in the resource.",
    )
    table_name: Mapped[Optional[str]] = mapped_column(
        String(256), nullable=True,
        doc="Table name within the KB; NULL = all tables in the KB.",
    )

    # Access mode for the target granularity.
    mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="allow",
        doc="'allow' | 'deny' | 'allow_columns'.",
    )

    # Column allowlist (mode='allow_columns' only): list of column names.
    column_allowlist: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    # Optional row-level filter, e.g. {"department": "sales"}.
    row_filter: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
