"""Review queue — manages the skill candidate review pipeline.

Skill candidates flow through:
  quarantined → testing → in_review → approved/rejected

When approved, a SkillProfile is created and the candidate is linked to it.
"""

import logging
from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.skill_candidate import SkillCandidate, CANDIDATE_STATUSES
from app.models.skill_profile import SkillProfile, SKILL_REVIEW_STATUSES, TRUST_LEVELS

logger = logging.getLogger(__name__)


class ReviewQueue:
    """Manages the skill candidate review pipeline."""

    def __init__(self, db: Session):
        self.db = db

    def list_candidates(
        self,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> list[SkillCandidate]:
        """List skill candidates, optionally filtered by status."""
        query = self.db.query(SkillCandidate).filter(SkillCandidate.is_deleted == False)
        if status:
            query = query.filter(SkillCandidate.status == status)
        return query.order_by(SkillCandidate.created_date.desc()).limit(limit).all()

    def get_candidate(self, candidate_id: str) -> Optional[SkillCandidate]:
        """Get a skill candidate by ID."""
        return self.db.query(SkillCandidate).filter(SkillCandidate.id == candidate_id).first()

    def update_status(
        self,
        candidate_id: str,
        new_status: str,
        review_notes: Optional[str] = None,
        reviewed_by: Optional[str] = None,
    ) -> Optional[SkillCandidate]:
        """Update a candidate's review status."""
        if new_status not in CANDIDATE_STATUSES:
            raise ValueError(f"Invalid status '{new_status}'")

        candidate = self.get_candidate(candidate_id)
        if not candidate:
            return None

        candidate.status = new_status
        if review_notes is not None:
            candidate.review_notes = review_notes
        if reviewed_by:
            candidate.reviewed_by = reviewed_by

        # If approved, create a SkillProfile
        if new_status == "approved":
            profile = self._publish_candidate(candidate)
            candidate.published_skill_profile_id = profile.id

        self.db.commit()
        self.db.refresh(candidate)
        logger.info("Candidate %s → %s", candidate_id, new_status)
        return candidate

    def _publish_candidate(self, candidate: SkillCandidate) -> SkillProfile:
        """Create a SkillProfile from an approved candidate."""
        manifest = candidate.generated_manifest or {}
        requires_sandbox = bool(manifest.get("artifact_type"))
        artifact_types = [manifest.get("artifact_type")] if manifest.get("artifact_type") else []

        profile = SkillProfile(
            id=str(uuid4()),
            name=candidate.name,
            display_name=candidate.name,
            description=candidate.description,
            version=manifest.get("version", "1.0.0"),
            manifest=manifest,
            skill_md=candidate.generated_skill_md,
            input_schema=manifest.get("inputs"),
            output_schema=manifest.get("outputs"),
            review_status="published",
            trust_level="community",
            review_notes=candidate.review_notes,
            reviewed_by=candidate.reviewed_by,
            artifact_types=artifact_types,
            requires_sandbox=requires_sandbox,
            sandbox_image=f"zhanlu-sandbox-{artifact_types[0]}" if artifact_types else None,
            org_id=candidate.org_id,
            app_id=candidate.app_id,
        )
        self.db.add(profile)
        self.db.commit()
        self.db.refresh(profile)

        logger.info("Published SkillProfile %s from candidate %s", profile.id, candidate.id)
        return profile

    def submit_for_review(
        self,
        candidate_id: str,
        reviewed_by: Optional[str] = None,
    ) -> Optional[SkillCandidate]:
        """Submit a quarantined candidate for review."""
        return self.update_status(candidate_id, "in_review", reviewed_by=reviewed_by)

    def approve(
        self,
        candidate_id: str,
        reviewed_by: str,
        notes: Optional[str] = None,
    ) -> Optional[SkillCandidate]:
        """Approve a candidate — creates a SkillProfile."""
        return self.update_status(candidate_id, "approved", review_notes=notes, reviewed_by=reviewed_by)

    def reject(
        self,
        candidate_id: str,
        reviewed_by: str,
        reason: str,
    ) -> Optional[SkillCandidate]:
        """Reject a candidate."""
        return self.update_status(candidate_id, "rejected", review_notes=reason, reviewed_by=reviewed_by)
