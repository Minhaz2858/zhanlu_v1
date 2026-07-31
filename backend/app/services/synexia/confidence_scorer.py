"""Confidence scorer — deterministic confidence calculation for execution results.

Computes a confidence score (0.0-1.0) based on multiple factors:
1. Plan completion rate (how many nodes succeeded)
2. Observation success rate (how many tool calls succeeded)
3. Artifact validation (were artifacts validated)
4. Data integrity (were DataSnapshots verified)
5. Policy compliance (was the plan approved)

The score is deterministic — not based on LLM self-grading.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def compute_confidence(execution, plan) -> tuple[float, dict]:
    """Compute a deterministic confidence score for an execution.

    Returns:
        (score, factors) where score is 0.0-1.0 and factors is a dict
        of individual factor scores with explanations.
    """
    factors = {}
    weights = {
        "plan_completion": 0.30,
        "observation_success": 0.30,
        "artifact_validation": 0.15,
        "data_integrity": 0.15,
        "policy_compliance": 0.10,
    }

    # 1. Plan completion rate
    if plan and plan.nodes:
        completed = sum(1 for n in plan.nodes if n.status == "completed")
        failed = sum(1 for n in plan.nodes if n.status == "failed")
        total = len(plan.nodes)
        plan_score = (completed / total) if total > 0 else 0.0
        factors["plan_completion"] = {
            "score": plan_score,
            "weight": weights["plan_completion"],
            "detail": f"{completed}/{total} nodes completed, {failed} failed",
        }
    else:
        plan_score = 0.5  # No plan = moderate confidence
        factors["plan_completion"] = {
            "score": plan_score,
            "weight": weights["plan_completion"],
            "detail": "No plan nodes",
        }

    # 2. Observation success rate
    observations = execution.observations or []
    if observations:
        successful = sum(1 for o in observations if o.success)
        obs_score = successful / len(observations)
        factors["observation_success"] = {
            "score": obs_score,
            "weight": weights["observation_success"],
            "detail": f"{successful}/{len(observations)} observations succeeded",
        }
    else:
        obs_score = 0.5
        factors["observation_success"] = {
            "score": obs_score,
            "weight": weights["observation_success"],
            "detail": "No observations recorded",
        }

    # 3. Artifact validation — REAL: each produced artifact must have a built
    #    version with a non-empty original blob (replaces the 0.8 placeholder).
    artifact_ids = execution.artifact_ids or []
    verification = _verification_summary(execution)
    art_score, art_detail = _artifact_validation_score(execution, artifact_ids, verification)
    factors["artifact_validation"] = {
        "score": art_score,
        "weight": weights["artifact_validation"],
        "detail": art_detail,
    }

    # 4. Data integrity — REAL: derive from the VERIFY result's data_integrity
    #    check (stored on execution.confidence_factors["verification"] by the
    #    VERIFY state), falling back to observation result_data inspection
    #    when no verification is available (replaces the 0.9 placeholder).
    data_score, data_detail = _data_integrity_score(execution, observations, verification)
    factors["data_integrity"] = {
        "score": data_score,
        "weight": weights["data_integrity"],
        "detail": data_detail,
    }

    # 5. Policy compliance
    policy = execution.policy_decision or {}
    if policy:
        decision = policy.get("decision", "allow")
        if decision == "allow":
            pol_score = 1.0
        elif decision == "require_confirm":
            pol_score = 0.7  # Confirmed but flagged
        else:  # deny
            pol_score = 0.0
        factors["policy_compliance"] = {
            "score": pol_score,
            "weight": weights["policy_compliance"],
            "detail": f"Policy decision: {decision} (risk={policy.get('risk_tier', 'unknown')})",
        }
    else:
        pol_score = 0.8
        factors["policy_compliance"] = {
            "score": pol_score,
            "weight": weights["policy_compliance"],
            "detail": "No policy evaluation recorded",
        }

    # Preserve the VERIFY summary on the returned factors so downstream
    # consumers (eval harness, API) can read it after FINALIZE overwrites
    # execution.confidence_factors with this dict.
    if verification:
        factors["verification"] = verification

    # Compute weighted score
    total_score = (
        plan_score * weights["plan_completion"] +
        obs_score * weights["observation_success"] +
        art_score * weights["artifact_validation"] +
        data_score * weights["data_integrity"] +
        pol_score * weights["policy_compliance"]
    )

    total_score = round(total_score, 2)
    logger.info("Confidence score: %.2f (plan=%.2f, obs=%.2f, art=%.2f, data=%.2f, pol=%.2f)",
                total_score, plan_score, obs_score, art_score, data_score, pol_score)

    return total_score, factors


# --- Quality gate (Phase B) --------------------------------------------------

def quality_gate_decision(
    confidence: float,
    artifact_ids: list,
    *,
    enabled: bool = True,
    threshold: float = 0.4,
) -> dict:
    """Decide whether produced artifacts may ship to the user.

    The gate fires only when artifacts exist AND confidence is below
    ``threshold``. When it fires, the caller holds the artifacts back from
    the shipped ExecutionResult (they remain in the DB and are listed in
    ``held_artifact_ids`` for operators/tests).

    Returns a dict with at least ``{"passed": bool}``.
    """
    if not enabled:
        return {"passed": True, "enabled": False}
    if not artifact_ids:
        return {"passed": True, "reason": "no_artifacts"}
    if confidence >= threshold:
        return {
            "passed": True,
            "confidence": confidence,
            "threshold": threshold,
            "artifact_count": len(artifact_ids),
        }
    return {
        "passed": False,
        "reason": "confidence_below_threshold",
        "confidence": confidence,
        "threshold": threshold,
        "artifact_count": len(artifact_ids),
        "held_artifact_ids": list(artifact_ids),
    }


# --- Real factor implementations (Phase 4) -----------------------------------

def _verification_summary(execution) -> dict:
    """Return the VERIFY result dict stored on execution.confidence_factors, or {}."""
    cf = getattr(execution, "confidence_factors", None) or {}
    v = cf.get("verification") if isinstance(cf, dict) else None
    return v if isinstance(v, dict) else {}


def _session_for(execution):
    """Best-effort: recover the SQLAlchemy session bound to an ORM object."""
    try:
        from sqlalchemy import inspect as _sa_inspect
        return _sa_inspect(execution).session
    except Exception:
        return None


def _check_ok(verification: dict, check_name: str):
    """Return the ok bool for a named check, or None if absent."""
    checks = verification.get("checks") if isinstance(verification, dict) else None
    if not checks:
        return None
    for c in checks:
        if isinstance(c, dict) and c.get("check") == check_name:
            return bool(c.get("ok", False))
    return None


def _artifact_validation_score(execution, artifact_ids: list, verification: dict) -> tuple[float, str]:
    """Score artifact validation by checking each artifact has a built version
    with a non-empty original blob. Falls back to the verifier's existence
    check when the execution is detached from a session.
    """
    if not artifact_ids:
        return 1.0, "No artifacts to validate"

    session = _session_for(execution)
    if session is None:
        ok = _check_ok(verification, "artifact_exists")
        if ok is True:
            return 1.0, f"{len(artifact_ids)} artifacts (existence verified, blob check skipped — detached)"
        if ok is False:
            return 0.2, f"{len(artifact_ids)} artifacts (existence check FAILED)"
        return 0.5, f"{len(artifact_ids)} artifacts (no session, no verification — indeterminate)"

    try:
        from app.models.artifact import Artifact, ArtifactBlob
        validated = 0
        for aid in artifact_ids:
            art = session.query(Artifact).filter(Artifact.id == aid).first()
            if art is None or not art.current_version_id:
                continue
            blob = (
                session.query(ArtifactBlob)
                .filter(
                    ArtifactBlob.version_id == art.current_version_id,
                    ArtifactBlob.blob_type == "original",
                )
                .first()
            )
            if blob is not None and (blob.file_size or 0) > 0:
                validated += 1
        score = validated / len(artifact_ids) if artifact_ids else 0.0
        return score, f"{validated}/{len(artifact_ids)} artifacts validated (non-empty original blob)"
    except Exception as e:
        logger.debug("artifact validation failed (non-fatal): %s", e)
        ok = _check_ok(verification, "artifact_exists")
        base = 1.0 if ok else (0.4 if ok is False else 0.5)
        return base, f"validation error: {e}"


def _data_integrity_score(execution, observations: list, verification: dict) -> tuple[float, str]:
    """Score data integrity from the VERIFY data_integrity check, falling back
    to inspecting nl2sql observations' result_data when no verification exists.
    """
    has_data = any(getattr(o, "observation_type", None) == "nl2sql" for o in observations)
    if not has_data:
        return 1.0, "No data queries involved"

    ok = _check_ok(verification, "data_integrity")
    if ok is True:
        return 1.0, "data_integrity check passed (VERIFY)"
    if ok is False:
        return 0.3, "data_integrity check FAILED (VERIFY)"

    # No verification available — derive from observation result_data.
    try:
        data_obs = [
            o for o in observations
            if getattr(o, "observation_type", None) == "nl2sql" and o.success is not False
        ]
        if not data_obs:
            return 0.4, "nl2sql observations present but none successful"
        nonempty = sum(
            1 for o in data_obs
            if getattr(o, "result_data", None) not in (None, "", {}, [])
        )
        score = nonempty / len(data_obs)
        return score, f"{nonempty}/{len(data_obs)} nl2sql observations have non-empty result_data"
    except Exception:
        return 0.5, "data_integrity indeterminate"
