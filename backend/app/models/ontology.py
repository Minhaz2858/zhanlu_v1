"""Ontology core — the fourfold business layer (data + logic + action +
security), Option "Enterprise AI OS" P0-2 (2026-10-15).

The strategy (docs: 2026-10-15-zhanlu-business-value-strategy.md): Zhanlu's
fragments — glossary, metrics, entities, tools, approvals — never composed
into one layer that agents operate through and admins can SEE. These tables
are that composition point, modeled on the Palantir Ontology (semantic =
nouns, kinetic = verbs, dynamic = logic) but scoped to what a mid-market
private deployment needs.

Design rules (from the strategy's anti-goals):
- Every object MUST be bound to a live backing source (table / ProjectEntity
  / MetricDefinition) — an unbound object is decoration, and the coverage
  scores report it as such.
- Every action MUST name a registry tool; ``risk_level`` decides the
  execution policy (read = auto, write = human confirmation until P1 trust
  upgrades).
- All rows are org-scoped (TimestampedBase) → RLS tenant_isolation applies,
  same wall as the rest of the platform.
"""

from typing import Optional

from sqlalchemy import Boolean, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase

#: Backing kinds — where an object's live data comes from.
BACKING_KINDS = ("table", "entity", "metric", "manual")

#: Action risk tiers — the kinetic-layer safety contract.
RISK_LEVELS = ("read", "write")


class OntologyDomain(TimestampedBase):
    """One of the 14 enterprise domains (IBM use-case map), per tenant.

    Seeded idempotently per org (``ensure_domains``); the starter objects/
    actions come from ``services/ontology/packs.py`` and are drafts the
    admin confirms during onboarding.
    """

    __tablename__ = "ontology_domains"
    __table_args__ = (
        UniqueConstraint("org_id", "code", name="uq_ontology_domain_org_code"),
    )

    code: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Sort order for the Coverage Map (supply chain / procurement / sales
    # first — the mainline per the strategy).
    sort_order: Mapped[int] = mapped_column(default=100)


class OntologyObject(TimestampedBase):
    """A business noun: unified from the tenant's data sources.

    ``backing_kind``/``backing_ref`` bind it to live data — ``table`` holds
    a real table name, ``entity`` a ProjectEntity id, ``metric`` a
    MetricDefinition id, ``manual`` = drafted but not yet wired (honest
    gap: counts as not-live in coverage).
    """

    __tablename__ = "ontology_objects"
    __table_args__ = (
        UniqueConstraint("org_id", "domain_id", "name", name="uq_ontology_object"),
    )

    domain_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    backing_kind: Mapped[str] = mapped_column(
        String(20), nullable=False, default="manual"
    )
    backing_ref: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # [{name, type, column?, description?}] — the object's properties.
    properties_json: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # P1 evidence taxonomy (PRD 2026-10-30) — canonical statuses live in
    # services/ontology/evidence.py. Writers: evidence.stamp_* ONLY.
    evidence_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="proposed",
        server_default="proposed", index=True,
    )
    confidence: Mapped[Optional[float]] = mapped_column(nullable=True)
    provenance_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)


class OntologyLink(TimestampedBase):
    """A typed edge between two objects — the decision graph's wiring."""

    __tablename__ = "ontology_links"

    from_object_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    to_object_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    link_type: Mapped[str] = mapped_column(
        String(50), nullable=False, doc="e.g. supplies, buys, affects, belongs_to"
    )
    cardinality: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # P1 evidence taxonomy (writers: services/ontology/evidence.py)
    evidence_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="proposed",
        server_default="proposed", index=True,
    )
    confidence: Mapped[Optional[float]] = mapped_column(nullable=True)
    provenance_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)


class OntologyAction(TimestampedBase):
    """A business verb bound to a tool — how agents DO, not just say.

    ``tool_name`` MUST exist in the tool registry (coverage treats a
    dangling tool as not-executable). ``risk_level=write`` actions default
    to human confirmation through the governance approval flow.
    """

    __tablename__ = "ontology_actions"
    __table_args__ = (
        UniqueConstraint("org_id", "object_id", "name", name="uq_ontology_action"),
    )

    object_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tool_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    risk_level: Mapped[str] = mapped_column(String(10), nullable=False, default="read")
    requires_confirmation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # Default argument template for the bound tool.
    params_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # P1 evidence taxonomy (writers: services/ontology/evidence.py)
    evidence_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="proposed",
        server_default="proposed", index=True,
    )
    confidence: Mapped[Optional[float]] = mapped_column(nullable=True)
    provenance_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)


class AgentDomainBinding(TimestampedBase):
    """Which agents serve which domains — with the permission scope they
    inherit (Palantir: humans and agents on the same security foundation)."""

    __tablename__ = "agent_domain_bindings"
    __table_args__ = (
        UniqueConstraint("org_id", "agent_name", "domain_id",
                         name="uq_agent_domain_binding"),
    )

    agent_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    domain_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    permission_scope: Mapped[str] = mapped_column(
        String(20), nullable=False, default="read",
        doc="read | write — the ceiling of action risk the agent may fire.",
    )


class CoverageState(TimestampedBase):
    """Per-domain readiness scores for the admin Coverage Map.

    Recomputed by ``services.ontology.service.compute_coverage`` — never
    hand-edited: the map must be a measurement, not a wish.
    """

    __tablename__ = "coverage_states"
    __table_args__ = (
        UniqueConstraint("org_id", "domain_id", name="uq_coverage_org_domain"),
    )

    domain_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # 0/1 flags (int for future graded scoring):
    data_connected: Mapped[int] = mapped_column(default=0)
    objects_live: Mapped[int] = mapped_column(default=0)
    actions_executable: Mapped[int] = mapped_column(default=0)
    agents_deployed: Mapped[int] = mapped_column(default=0)
    # The business metric this domain is accountable for (from §4 matrix).
    metric_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    metric_value: Mapped[Optional[float]] = mapped_column(default=None)
