"""Repository base — shared CRUD helpers for all repositories.

Provides create, get_by_id, update, delete (soft), list_paginated
that work with any TimestampedBase model.
"""

from __future__ import annotations

from typing import Any, Sequence, TypeVar, Optional
from uuid import uuid4

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.models.base import TimestampedBase

T = TypeVar("T", bound=TimestampedBase)


def create(session: Session, model_cls: type[T], **kwargs: Any) -> T:
    """Create and persist a new model instance with a UUID id."""
    if "id" not in kwargs:
        kwargs["id"] = str(uuid4())
    instance = model_cls(**kwargs)
    session.add(instance)
    session.flush()
    return instance


def get_by_id(session: Session, model_cls: type[T], instance_id: str) -> Optional[T]:
    """Get a non-deleted instance by id."""
    stmt = select(model_cls).where(
        model_cls.id == instance_id,
        model_cls.is_deleted == False,  # noqa: E712
    )
    return session.scalar(stmt)


def update(session: Session, instance: T, **kwargs: Any) -> T:
    """Update fields on an existing instance and flush."""
    for key, value in kwargs.items():
        if hasattr(instance, key):
            setattr(instance, key, value)
    session.flush()
    return instance


def soft_delete(session: Session, instance: T) -> T:
    """Soft-delete an instance."""
    instance.is_deleted = True
    session.flush()
    return instance


def list_paginated(
    session: Session,
    model_cls: type[T],
    *,
    org_id: str = "default-org",
    app_id: str = "default-app",
    offset: int = 0,
    limit: int = 50,
    filters: Optional[dict[str, Any]] = None,
    order_by: Optional[str] = None,
) -> Sequence[T]:
    """List non-deleted instances with optional filtering and pagination."""
    stmt = select(model_cls).where(
        model_cls.is_deleted == False,  # noqa: E712
        model_cls.org_id == org_id,
        model_cls.app_id == app_id,
    )
    if filters:
        for col_name, value in filters.items():
            col = getattr(model_cls, col_name, None)
            if col is not None:
                stmt = stmt.where(col == value)
    if order_by:
        col = getattr(model_cls, order_by, None)
        if col is not None:
            stmt = stmt.order_by(col)
    return session.scalars(stmt.offset(offset).limit(limit)).all()


def list_by_org(
    session: Session,
    model_cls: type[T],
    *,
    org_id: str = "default-org",
    offset: int = 0,
    limit: int = 50,
) -> Sequence[T]:
    """List non-deleted instances for a specific org (ignoring app_id)."""
    stmt = select(model_cls).where(
        model_cls.is_deleted == False,  # noqa: E712
        model_cls.org_id == org_id,
    )
    return session.scalars(stmt.offset(offset).limit(limit)).all()


def count(
    session: Session,
    model_cls: type[T],
    *,
    org_id: str = "default-org",
    app_id: str = "default-app",
    filters: Optional[dict[str, Any]] = None,
) -> int:
    """Count non-deleted instances matching filters."""
    stmt = select(func.count()).select_from(model_cls).where(
        model_cls.is_deleted == False,  # noqa: E712
        model_cls.org_id == org_id,
        model_cls.app_id == app_id,
    )
    if filters:
        for col_name, value in filters.items():
            col = getattr(model_cls, col_name, None)
            if col is not None:
                stmt = stmt.where(col == value)
    return session.scalar(stmt) or 0
