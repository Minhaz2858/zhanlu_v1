"""BusinessRule — unified, evidence-backed enterprise business rules
(Phase 1A of the Enterprise Knowledge Model, 2026-11-01).

ONE authoritative home for business rules so the same rule can eventually
serve NL2SQL prompt guidelines, business-process reasoning and governed
action pre-checks (``applies_to``). Rules are discovered or authored as
``proposed`` and NEVER auto-enabled: only a human review moves them to
``verified`` / ``rejected``, and only ``enabled`` (verified + activated)
rules may influence runtime behavior.

Relationship to existing machinery (no duplication):
- ``SemanticGuidelines`` (db/semantic_store) remain the NL2SQL prompt
  representation; a future bridge maps enabled ``applies_to='sql'``
  BusinessRules onto them (Phase 1B+, backfill is a bridge only);
- ``source_evidence_ids`` reference EBPM evidence so every rule is
  traceable to verbatim quotes (ProcessEvidence rows are never modified);
- review decisions append to ``review_history`` (same convention as the
  EBPM provenance ``review_history``) and are audited by callers.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Index, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase, Base

SCOPES = ("org", "domain", "object", "process")
STATUSES = ("proposed", "verified", "rejected", "enabled")
APPLIES_TO = ("sql", "action", "process")


class BusinessRule(TimestampedBase, Base):
    """A governed business rule, evidence-backed and human-reviewed."""

    __tablename__ = "business_rules"
    __table_args__ = (
        CheckConstraint(
            "scope IN ('org', 'domain', 'object', 'process')",
            name="ck_business_rule_scope",
        ),
        CheckConstraint(
            "status IN ('proposed', 'verified', 'rejected', 'enabled')",
            name="ck_business_rule_status",
        ),
        CheckConstraint(
            "applies_to IN ('sql', 'action', 'process')",
            name="ck_business_rule_applies_to",
        ),
        # 'org' rules are global to the org; scoped rules must name a target
        CheckConstraint(
            "scope = 'org' OR scope_ref_id IS NOT NULL",
            name="ck_business_rule_scoped_needs_ref",
        ),
        Index("ix_business_rules_org_status", "org_id", "status"),
        Index("ix_business_rules_org_scope", "org_id", "scope", "scope_ref_id"),
    )

    # org | domain | object | process — 'org' rules have scope_ref_id=None
    scope: Mapped[str] = mapped_column(String(20), nullable=False,
                                       default="org", server_default="org")
    # OntologyDomain / OntologyObject / BusinessProcess id (scope-dependent)
    scope_ref_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True)
    statement: Mapped[str] = mapped_column(Text, nullable=False)

    # EBPM evidence ids backing this rule (traceability; never fabricated)
    source_evidence_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # proposed → verified | rejected; verified → enabled. Never automatic.
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="proposed", server_default="proposed",
        index=True)
    # sql | action | process — which runtime may consume the rule
    applies_to: Mapped[str] = mapped_column(
        String(20), nullable=False, default="sql", server_default="sql")

    # append-only [{decision, reviewer_id, reviewer_name, reason, at}]
    review_history: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Optional KB targeting for applies_to='sql' rules (bridge consumes it;
    # NULL = org-wide → every KB of the org at sync time)
    datasource_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)

    def record_review(self, decision: str, reviewer_id: str | None = None,
                      reviewer_name: str | None = None,
                      reason: str | None = None) -> None:
        """Append a review decision (confirmed/rejected/enabled) to the
        history. Callers set ``status`` and persist the audit event."""
        history = list(self.review_history or [])
        from datetime import datetime, timezone

        history.append({
            "decision": decision,
            "reviewer_id": reviewer_id,
            "reviewer_name": reviewer_name,
            "reason": reason,
            "at": datetime.now(timezone.utc).isoformat(),
        })
        self.review_history = history

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"<BusinessRule {self.id} [{self.scope}/{self.applies_to}] "
                f"{self.status}: {self.statement[:40]!r}>")
