"""Skill dry-run gate — validates a skill after save by running a test case.

When a skill is saved (created, uploaded, or collected), a ``SkillTestCase``
is auto-generated and executed. The test verifies:

  - **Schema validation**: The SKILL.md has valid frontmatter, required
    sections (Overview, Steps), and is non-trivial in length.
  - **Registry availability**: The skill is discoverable via the
    SkillsRegistry after the save+reload cycle.

For code-based skills with bundled scripts, a sandbox execution test
can also be generated (test_type="integration").

The gate is **non-blocking**: a failure produces a warning in the
``SkillTestCase`` record but does NOT prevent the skill from being saved.
The result is returned to the caller for display in the UI.
"""

from __future__ import annotations

import logging
from typing import Optional
from uuid import uuid4

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Minimum body length for a skill to be considered non-trivial
_MIN_BODY_LENGTH = 100
# Sections we look for in a well-formed SKILL.md
_REQUIRED_SECTIONS = ["overview", "steps"]


def run_dry_run_gate(
    skill_name: str,
    db: Session,
    skill_body: Optional[str] = None,
) -> dict:
    """Auto-generate a SkillTestCase and execute it.

    Args:
        skill_name: The name of the skill to validate.
        db: Database session for reading/writing SkillTestCase records.
        skill_body: The SKILL.md body (if already available; otherwise
            loaded from the registry).

    Returns:
        ``{"passed": bool, "test_case_id": str, "result": str, "error": str|None,
           "checks": list[dict]}``
    """
    checks: list[dict] = []
    passed = True
    error_msg: Optional[str] = None

    # Load the skill body if not provided
    if skill_body is None:
        try:
            from app.services.skills_loader import get_skill
            meta = get_skill(skill_name)
            if meta and meta.body:
                skill_body = meta.body
            else:
                skill_body = ""
        except Exception as exc:
            logger.warning("Dry-run: failed to load skill %r: %s", skill_name, exc)
            skill_body = ""

    # Check 1: Non-empty body
    body_len = len(skill_body.strip())
    if body_len == 0:
        checks.append({"check": "non_empty", "passed": False, "detail": "SKILL.md body is empty"})
        passed = False
    elif body_len < _MIN_BODY_LENGTH:
        checks.append({"check": "min_length", "passed": False, "detail": f"SKILL.md body is only {body_len} chars (minimum {_MIN_BODY_LENGTH})"})
        passed = False
    else:
        checks.append({"check": "non_empty", "passed": True, "detail": f"Body length: {body_len} chars"})

    # Check 2: Required sections present
    body_lower = skill_body.lower()
    missing_sections = []
    for section in _REQUIRED_SECTIONS:
        if section not in body_lower:
            missing_sections.append(section)
    if missing_sections:
        checks.append({"check": "required_sections", "passed": False, "detail": f"Missing sections: {', '.join(missing_sections)}"})
        passed = False
    else:
        checks.append({"check": "required_sections", "passed": True, "detail": "All required sections present"})

    # Check 3: Registry discoverability
    try:
        from app.services.skills_loader import get_skill
        meta = get_skill(skill_name)
        if meta is not None:
            checks.append({"check": "registry_discoverable", "passed": True, "detail": f"Skill found in registry (category={meta.category})"})
        else:
            checks.append({"check": "registry_discoverable", "passed": False, "detail": "Skill not found in SkillsRegistry after save"})
            passed = False
    except Exception as exc:
        checks.append({"check": "registry_discoverable", "passed": False, "detail": f"Registry check error: {exc}"})
        passed = False

    # Check 4: Security scan (warn-only, doesn't fail the gate)
    try:
        from app.services.skill_scanner.scanner import scan_text
        scan_result = scan_text(body=skill_body, skill_name=skill_name)
        if scan_result.has_critical:
            checks.append({"check": "security_scan", "passed": False, "detail": f"Critical findings: {scan_result.summary}"})
            passed = False
        else:
            checks.append({"check": "security_scan", "passed": True, "detail": scan_result.summary})
    except Exception as exc:
        checks.append({"check": "security_scan", "passed": True, "detail": f"Scan skipped: {exc}"})

    if not passed:
        failed_checks = [c for c in checks if not c["passed"]]
        error_msg = "; ".join(c["detail"] for c in failed_checks)

    result_status = "pass" if passed else "fail"

    # Persist a SkillTestCase record
    test_case_id = str(uuid4())
    try:
        from app.models.skill_test_case import SkillTestCase
        from datetime import datetime, timezone

        # Look for an existing auto-generated test case for this skill
        existing = db.query(SkillTestCase).filter(
            SkillTestCase.name == f"[auto] {skill_name} schema validation",
            SkillTestCase.is_deleted == False,
        ).first()

        now_dt = datetime.now(timezone.utc)

        if existing:
            # Update the existing test case
            existing.status = result_status
            existing.last_run_at = now_dt
            existing.last_result = result_status
            existing.last_error = error_msg
            existing.run_count = (existing.run_count or 0) + 1
            if passed:
                existing.pass_count = (existing.pass_count or 0) + 1
            existing.assertions = checks
            test_case_id = existing.id
        else:
            tc = SkillTestCase(
                id=test_case_id,
                name=f"[auto] {skill_name} schema validation",
                description=f"Auto-generated dry-run gate for skill '{skill_name}'",
                test_type="schema",
                skill_profile_id=None,
                input_json={"skill_name": skill_name},
                expected_schema_valid=True,
                assertions=checks,
                status=result_status,
                last_run_at=now_dt,
                last_result=result_status,
                last_error=error_msg,
                run_count=1,
                pass_count=1 if passed else 0,
            )
            db.add(tc)

        db.commit()
    except Exception as exc:
        logger.warning("Dry-run: failed to persist SkillTestCase (non-fatal): %s", exc)

    logger.info(
        "Dry-run gate for %r: %s (%d checks, %d passed)",
        skill_name, result_status, len(checks), sum(1 for c in checks if c["passed"]),
    )

    return {
        "passed": passed,
        "test_case_id": test_case_id,
        "result": result_status,
        "error": error_msg,
        "checks": checks,
    }


def trigger_dry_run_after_save(skill_name: str, db: Optional[Session] = None) -> dict:
    """Convenience wrapper to run the dry-run gate after a skill save.

    Opens its own DB session if none is provided, so callers like
    ``skill_sync.write_skill_md()`` (which don't have a db handle) can
    trigger the gate without managing sessions.
    """
    if db is not None:
        return run_dry_run_gate(skill_name, db)

    try:
        from app.database import SessionLocal
        own_db = SessionLocal()
        try:
            return run_dry_run_gate(skill_name, own_db)
        finally:
            own_db.close()
    except Exception as exc:
        logger.warning("Dry-run gate failed to start (non-fatal): %s", exc)
        return {"passed": False, "error": str(exc), "checks": []}
