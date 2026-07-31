"""WorkspaceSetting model — a per-workspace key/value store for org-level
flags that affect agent and data-source behavior.

The first user is the ``auto_bind_all_datasources`` opt-in flag (per
DATA-CORE-3): when on, every connected database KnowledgeBase in the
workspace is unioned with the explicit per-agent ``knowledge_bases`` list
at agent runtime, so any agent can read from any connected datasource.

The model is intentionally generic so future workspace-level toggles
(``force_grounded_citations``, ``disable_write_tools``, etc.) can be added
without schema changes.
"""

from sqlalchemy import String, Text, Boolean
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class WorkspaceSetting(TimestampedBase):
    __tablename__ = "workspace_settings"

    # The setting key, e.g. "auto_bind_all_datasources".
    key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)

    # The setting value. Stored as text so we can accept booleans
    # ("true"/"false"), ints ("42"), or JSON strings. The reader
    # (``workspace_settings_service.get``) knows how to coerce.
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Soft opt-out so settings can be archived without losing history.
    # ``is_deleted`` is inherited from TimestampedBase.
