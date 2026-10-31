"""Enterprise Business Process & Operating Model — discovered process models.

Everything here is DISCOVERED from authorized enterprise resources, not
hand-modeled: every row carries evidence status, confidence, provenance and
(when present) verbatim evidence quotes. Discovery distinguishes:

  documented  — what the company SAYS it does (SOPs, policies)
  configured  — what its systems are set up to execute (workflows/automations)
  observed    — what actually happened (timestamped execution records)

An LLM may PROPOSE candidates (evidence_status inferred/proposed); only a
human org-admin confirms (verified) via the audited evidence lifecycle.
"""

from __future__ import annotations

from sqlalchemy import JSON, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampedBase


def _uq(name: str, *cols: str):
    from sqlalchemy import UniqueConstraint

    return UniqueConstraint(*cols, name=name)


class EnterpriseResource(TimestampedBase, Base):
    """Refreshable inventory row for one authorized enterprise resource."""

    __tablename__ = "enterprise_resources"
    __table_args__ = (
        # one inventory row per (org, kind, resource)
        _uq("uq_ent_res", "org_id", "resource_kind", "ref_id"),
    )

    resource_kind: Mapped[str] = mapped_column(
        String(40), nullable=False, index=True,
        doc="datasource | document | automation | agent | ontology | action_interface")
    ref_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    evidence_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="verified", server_default="verified")
    discovered_at = mapped_column(String(40), nullable=True)


class OrgUnit(TimestampedBase, Base):
    """A discovered business unit / department / team / role."""

    __tablename__ = "org_units"
    __table_args__ = (_uq("uq_org_unit", "org_id", "name", "unit_type"),)

    unit_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="department",
        doc="department | team | role")
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    parent_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    responsibilities: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="inferred", server_default="inferred")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    provenance_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class BusinessCapability(TimestampedBase, Base):
    """What the enterprise is capable of doing (≠ department, ≠ system)."""

    __tablename__ = "business_capabilities"
    __table_args__ = (_uq("uq_capability", "org_id", "name"),)

    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # loose references to supporting org units / ontology objects / systems
    supports_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    evidence_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="inferred", server_default="inferred")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    provenance_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class BusinessProcess(TimestampedBase, Base):
    """A discovered end-to-end business process (meaningful operational work)."""

    __tablename__ = "business_processes"
    __table_args__ = (_uq("uq_process", "org_id", "name"),)

    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    purpose: Mapped[str | None] = mapped_column(Text, nullable=True)
    trigger_event: Mapped[str | None] = mapped_column(Text, nullable=True)
    objective: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_condition: Mapped[str | None] = mapped_column(Text, nullable=True)
    completion_condition: Mapped[str | None] = mapped_column(Text, nullable=True)
    # discovered label such as "order-to-cash" — a family label, NEVER a
    # hardcoded catalogue (families are proposed from evidence, not assumed).
    process_family: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    discovery_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="documented",
        server_default="documented",
        doc="documented | configured | observed")
    evidence_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="proposed", server_default="proposed")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    provenance_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    kpis_json: Mapped[list | None] = mapped_column(JSON, nullable=True)


class ProcessActivity(TimestampedBase, Base):
    """One activity / step within a discovered process (ordered)."""

    __tablename__ = "process_activities"
    __table_args__ = (
        _uq("uq_activity", "org_id", "process_id", "seq", "name"),
    )

    process_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    activity_type: Mapped[str] = mapped_column(
        String(30), nullable=False, default="human_task",
        doc="start_event | end_event | human_task | system_task | decision | "
            "approval | subprocess")
    performer: Mapped[str | None] = mapped_column(
        String(255), nullable=True,
        doc="org unit / role / system / agent performing the activity")
    input_refs: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    output_refs: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    business_rules: Mapped[str | None] = mapped_column(Text, nullable=True)
    exception_paths: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="inferred", server_default="inferred")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    provenance_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class ProcessLink(TimestampedBase, Base):
    """Dependency / handoff between two discovered processes."""

    __tablename__ = "process_links"
    __table_args__ = (_uq("uq_plink", "org_id", "predecessor_id", "successor_id"),)

    predecessor_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    successor_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    handoff_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="inferred", server_default="inferred")


class ProcessEvidence(TimestampedBase, Base):
    """Verbatim evidence backing a discovered process / activity."""

    __tablename__ = "process_evidence"
    __table_args__ = (_uq("uq_pevidence", "org_id", "process_id", "source_ref"),)

    process_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    activity_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evidence_kind: Mapped[str] = mapped_column(
        String(40), nullable=False,
        doc="document_chunk | table_catalog | relation | ontology_link | metric "
            "| execution_log")
    source_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    quote: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # evidence-level review status: proposed (AI-extracted, unreviewed) →
    # verified / rejected by a human org-admin. Never auto-promoted.
    evidence_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="proposed", server_default="proposed")
