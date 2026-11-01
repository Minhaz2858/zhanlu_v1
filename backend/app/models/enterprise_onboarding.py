"""EnterpriseOnboardingRun — resource onboarding lifecycle record (Phase 4).

One row per onboarding/refresh attempt of an authorized enterprise
resource. Bounded, idempotent, retryable: the run records per-step
status, the resource's inventory digest (drift detection), and an honest
failure surface. Proposals produced during onboarding are ALWAYS
proposed (never auto-verified); the run row itself claims nothing about
process correctness.
"""

from typing import Optional

from sqlalchemy import String, Integer, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class EnterpriseOnboardingRun(TimestampedBase):
    __tablename__ = "enterprise_onboarding_runs"

    org_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(40), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(120), nullable=False)
    resource_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # completed | partial_failed | failed | skipped_unchanged | resource_missing
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    # per-step: [{step, status: ok|failed|skipped|unsupported, detail}]
    steps_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # digest of the resource's current inventory (drift detection); NULL =
    # digest not computable for this resource type (honestly noted)
    inventory_digest: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    actor_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
