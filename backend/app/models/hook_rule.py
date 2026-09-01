"""HookRule — DB-backed lifecycle hook configuration (org/app-scoped).

Mirrors the fields of ``HookConfig`` (app/services/hooks/__init__.py) so a
``HookRule`` row can be converted to a ``HookConfig`` and registered with the
``HookExecutor`` at startup or on mutation. Built-in safety hooks live in the
code registry (``hooks/registry.py``); org/app-specific rules live here and
are editable via the ``/api/hooks`` CRUD API.
"""
from typing import Optional

from sqlalchemy import String, Integer, JSON, Boolean, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class HookRule(TimestampedBase):
    __tablename__ = "hook_rules"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # HookConfig-mirroring fields
    event: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    command: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    url: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)
    method: Mapped[str] = mapped_column(String(16), default="POST", nullable=False)
    headers: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    timeout: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    matcher: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    block_on_failure: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
