"""Synexia VERIFY — validate execution outputs after the OBSERVE state.

Two layers, both pure functions, no I/O outside the provided SQLAlchemy session:

1. ``verify_execution`` (deterministic, always-on when the FSM runs):
   - ``artifact_exists``: every artifact_id referenced by an observation
     resolves to a row in the ``artifacts`` table.
   - ``observation_success``: no observation with ``success=False`` unless the
     originating plan node was marked optional.
   - ``data_integrity``: each successful observation's ``result_data`` is
     non-empty and well-formed (JSON-shaped when it's a dict).

2. ``verify_with_llm`` (optional, gated by
   ``settings.SYNEXIA_VERIFIER_LLM_ENABLED``): calls the shared LLM service
   to run a qualitative rubric pass. Returns a list of additional
   ``{check, ok, detail}`` dicts. On any error (LLM unavailable, malformed
   response, timeout) it returns ``[]`` and the deterministic result is
   unchanged.

The contract is intentionally narrow: VERIFY is non-fatal by default. A
``passed=False`` result is *information*, not a gate — partial results are
still useful and ``confidence_scorer`` will lower the score accordingly.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# Optional llm_callable signature: (prompt: str) -> dict | None
# Default uses the shared LLM service. Pass a stub in tests.
LLMRubricFn = Callable[[str], Optional[dict]]


def _check_artifact_exists(db: Session, observations: list) -> dict:
    """Every artifact_id in any observation must resolve to a row.

    ``critical=True``: a dangling artifact id is a hard correctness failure
    and a re-plan trigger, not a soft warning.
    """
    try:
        from app.models.artifact import Artifact  # local import to avoid cycle
    except Exception:
        # If the Artifact model is unavailable in a slim test env, treat as
        # not-applicable and report ok=True with a note.
        return {"check": "artifact_exists", "ok": True, "detail": "no_artifact_model", "critical": True}

    missing: list[str] = []
    for obs in observations:
        ids = obs.artifact_ids or []
        for aid in ids:
            row = db.query(Artifact).filter(Artifact.id == aid).first()
            if row is None:
                missing.append(aid)
    return {
        "check": "artifact_exists",
        "ok": not missing,
        "detail": f"{len(missing)} missing" if missing else "ok",
        "critical": True,
    }


def _check_observation_success(observations: list) -> dict:
    """No observation should report success=False unless its node is optional.

    ``critical=True``: a failed required step is a re-plan trigger.
    """
    failed = []
    for obs in observations:
        if obs.success is False:
            # optional flag lives on the plan node; absent plan = all required
            optional = False
            try:
                if obs.plan_node is not None and getattr(obs.plan_node, "optional", False):
                    optional = True
            except Exception:
                optional = False
            if not optional:
                failed.append(obs.id)
    return {
        "check": "observation_success",
        "ok": not failed,
        "detail": f"{len(failed)} failed" if failed else "ok",
        "critical": True,
    }


def _check_data_integrity(observations: list) -> dict:
    """Each successful observation must have non-empty result_data.

    ``critical=False``: empty result data degrades quality but is not, by
    itself, a reason to re-plan (the step technically "succeeded").
    """
    bad = []
    for obs in observations:
        if obs.success is False:
            continue
        rd = obs.result_data
        if rd is None or rd == "" or rd == {} or rd == []:
            bad.append(obs.id)
    return {
        "check": "data_integrity",
        "ok": not bad,
        "detail": f"{len(bad)} empty" if bad else "ok",
        "critical": False,
    }


def _check_degenerate_result(observations: list) -> dict:
    """Detect degenerate data-agent results: all-NULL dimensions, 0 rows,
    or metadata-only rows (MIN_DATE/MAX_DATE/ENTRY_COUNT with no
    actual business data).

    ``critical=True``: degenerate results mean the data query failed to
    retrieve meaningful data — a re-plan with adjusted SQL/filters is needed.
    """
    from app.services.goal_contract import is_effective_empty, is_metadata_only_rows  # lazy import

    degenerate = []
    for obs in observations:
        if obs.success is False:
            continue
        rd = obs.result_data
        if not isinstance(rd, dict):
            continue
        # Only check observations that produced rows (data-agent shape)
        if "rows" not in rd:
            continue
        rows = rd.get("rows", [])
        if isinstance(rows, (list, tuple)) and len(rows) == 0:
            degenerate.append(obs.id)
        elif isinstance(rows, list) and len(rows) > 0:
            if is_effective_empty(rows):
                degenerate.append(obs.id)
            elif is_metadata_only_rows(rows):
                # Metadata-only results (e.g. 1 row of MIN_DATE/MAX_DATE/ENTRY_COUNT)
                # are degenerate — the query returned schema info, not business data.
                degenerate.append(obs.id)
    return {
        "check": "degenerate_result",
        "ok": not degenerate,
        "detail": f"{len(degenerate)} degenerate" if degenerate else "ok",
        "critical": True,
    }


def _check_wrong_grain(observations: list) -> dict:
    """Detect wrong-grain results: plan expected aggregate but got raw rows.

    ``critical=False``: wrong grain degrades quality but the data is still
    usable — the LLM can summarize it. Flagged as a soft warning.
    """
    wrong_grain = []
    for obs in observations:
        if obs.success is False:
            continue
        rd = obs.result_data
        if not isinstance(rd, dict):
            continue
        rows = rd.get("rows", [])
        if not isinstance(rows, list):
            continue
        # Heuristic: if a data-agent observation returned >50 rows and the
        # plan node's expected_grain was "aggregate" (1-5 rows), it's likely
        # wrong grain.
        expected_grain = None
        if obs.plan_node is not None:
            expected_grain = getattr(obs.plan_node, "expected_grain", None)
        if expected_grain == "aggregate" and len(rows) > 50:
            wrong_grain.append(obs.id)
        # Also flag if >200 raw rows without pagination — likely unaggregated
        elif len(rows) > 200 and expected_grain != "raw":
            wrong_grain.append(obs.id)
    return {
        "check": "wrong_grain",
        "ok": not wrong_grain,
        "detail": f"{len(wrong_grain)} wrong-grain" if wrong_grain else "ok",
        "critical": False,  # soft — LLM can still summarize
    }


def _check_coverage(observations: list, plan_nodes: list | None = None) -> dict:
    """Verify every plan node that should produce answer data actually did.

    ``critical=True``: missing coverage means the user's question is only
    partially answered — a re-plan is needed.
    """
    if not plan_nodes:
        return {"check": "coverage", "ok": True, "detail": "no_plan", "critical": True}

    # Build set of plan node IDs that were expected to produce data
    expected_node_ids = set()
    for node in plan_nodes:
        if getattr(node, "produces_data", True) and not getattr(node, "optional", False):
            expected_node_ids.add(getattr(node, "id", id(node)))

    # Build set of node IDs that actually produced data
    observed_node_ids = set()
    for obs in observations:
        if obs.success is False:
            continue
        node_id = getattr(obs, "plan_node_id", None) or getattr(obs, "node_id", None)
        if node_id:
            rd = obs.result_data
            if isinstance(rd, dict) and rd.get("rows"):
                observed_node_ids.add(node_id)

    uncovered = expected_node_ids - observed_node_ids
    return {
        "check": "coverage",
        "ok": not uncovered,
        "detail": f"{len(uncovered)} uncovered" if uncovered else "ok",
        "critical": True,
    }


class VerificationResult:
    """Lightweight value object — no pydantic dependency on this hot path."""

    __slots__ = ("checks", "artifact_ok", "observations_ok", "data_integrity_ok",
                 "degenerate_result_ok", "wrong_grain_ok", "coverage_ok")

    def __init__(self, checks: list[dict]):
        self.checks = checks
        by_name = {c["check"]: c.get("ok", False) for c in checks}
        self.artifact_ok = bool(by_name.get("artifact_exists", True))
        self.observations_ok = bool(by_name.get("observation_success", True))
        self.data_integrity_ok = bool(by_name.get("data_integrity", True))
        self.degenerate_result_ok = bool(by_name.get("degenerate_result", True))
        self.wrong_grain_ok = bool(by_name.get("wrong_grain", True))
        self.coverage_ok = bool(by_name.get("coverage", True))

    @property
    def passed(self) -> bool:
        return (self.artifact_ok and self.observations_ok
                and self.data_integrity_ok
                and self.degenerate_result_ok
                and self.wrong_grain_ok
                and self.coverage_ok)

    def all_checks_passed(self) -> bool:
        """True iff every check (deterministic + any LLM-augmented) is ok.

        Use this when you want the overall verdict after the LLM rubric pass
        has added its own checks (which are not part of the three named
        booleans above).
        """
        return all(c.get("ok", False) for c in self.checks)

    @property
    def critical_failed(self) -> bool:
        """True iff any CRITICAL check failed — a re-plan trigger.

        Non-critical checks (e.g. soft data-integrity warnings) do not count:
        they lower confidence but don't justify the cost of a re-plan.
        """
        return any(
            (not c.get("ok", False)) and c.get("critical", False)
            for c in self.checks
        )

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "all_passed": self.all_checks_passed(),
            "checks": list(self.checks),
        }


# --- Grounding checks (P2 Task 4) -------------------------------------------
# These are deterministic, pure-function checks driven by the agent's
# ``evaluation_profile.grounding_checks`` list. The schema is documented
# in backend/app/services/agent_studio/preflight.py and seeded by
# backend/app/services/system_agents.py. Currently two checks are
# supported: ``source_citation`` and ``hallucination_check``. Unknown
# check names are silently skipped (forward-compat — adding a new check
# to the schema is a non-breaking change).

import re as _re  # local alias to avoid shadowing the standard import above


# Artifact ids in assistant content are matched two ways:
#
# 1. Strong (keyword + id): "artifact art-1", "report: art-2", "附件 art-3".
#    High precision — these are obviously artifact references.
# 2. Loose (bare id-shaped token): any token shaped like an id (lowercase
#    prefix + dash/underscore + alphanumerics, or vice-versa, 4+ chars)
#    e.g. "art-1", "report_42", "doc-abc-9". Lower precision but catches
#    sentences like "I produced art-1 and art-2." The LLM-augmented pass
#    (verify_with_llm) does the precise verification.
_ARTIFACT_REF_KEYWORDED_RE = _re.compile(
    r"\b(?:artifact[_\s-]*id|artifact|附件|报表|报告|文档|report|document|file)\b"
    r"[\s:：]*(?P<id>[a-zA-Z0-9_\-]{4,})",
    _re.IGNORECASE,
)
# Bare id pattern: a word with at least one dash or underscore, 4+ chars,
# letters + digits + dashes/underscores. Anchored to word boundaries so
# we don't catch things like "the" or "at" or "data".
_ARTIFACT_REF_BARE_RE = _re.compile(
    r"\b(?P<id>[a-zA-Z]+[_\-][a-zA-Z0-9_\-]{2,}|[a-zA-Z0-9_\-]{2,}[_\-][a-zA-Z0-9]+)\b"
)


def _check_source_citation(execution) -> dict:
    """Heuristic source-citation check.

    Passes when at least one successful observation has a tool_name
    that appears in the assistant content, OR when there are no
    observations (no claim to verify). Fails when the assistant content
    contains concrete numeric/identifier claims but no observation's
    tool_name or any artifact id shows up.
    """
    content = (getattr(execution, "assistant_content", "") or "").strip()
    observations = list(getattr(execution, "observations", []) or [])
    if not observations or not content:
        return {
            "check": "source_citation",
            "ok": True,
            "detail": "no_observations_or_content",
        }
    # Collect evidence: tool names + artifact ids from successful observations.
    evidence: set[str] = set()
    for obs in observations:
        if not getattr(obs, "success", True):
            continue
        tn = getattr(obs, "tool_name", None)
        if tn:
            evidence.add(str(tn))
        for aid in (getattr(obs, "artifact_ids", None) or []):
            if aid:
                evidence.add(str(aid))
    if not evidence:
        return {
            "check": "source_citation",
            "ok": True,
            "detail": "no_evidence_to_check",
        }
    content_lower = content.lower()
    if any(e.lower() in content_lower for e in evidence):
        return {
            "check": "source_citation",
            "ok": True,
            "detail": "cites_observation",
        }
    return {
        "check": "source_citation",
        "ok": False,
        "detail": "no_observation_cited",
    }


def _check_hallucination(execution) -> dict:
    """Heuristic hallucination check.

    If the assistant content references artifact-like ids, every
    referenced id must appear in some observation's ``artifact_ids``
    (or its ``result_data.artifact_id``). Otherwise flag. If no
    artifact-like references are present in the content, pass.
    """
    content = (getattr(execution, "assistant_content", "") or "")
    if not content:
        return {
            "check": "hallucination_check",
            "ok": True,
            "detail": "no_content",
        }
    referenced: set[str] = set()
    for m in _ARTIFACT_REF_KEYWORDED_RE.finditer(content):
        referenced.add(m.group("id"))
    for m in _ARTIFACT_REF_BARE_RE.finditer(content):
        referenced.add(m.group("id"))
    if not referenced:
        return {
            "check": "hallucination_check",
            "ok": True,
            "detail": "no_artifact_refs",
        }
    produced: set[str] = set()
    for obs in (getattr(execution, "observations", None) or []):
        for aid in (getattr(obs, "artifact_ids", None) or []):
            if aid:
                produced.add(str(aid))
        rd = getattr(obs, "result_data", None)
        if isinstance(rd, dict):
            rid = rd.get("artifact_id")
            if rid:
                produced.add(str(rid))
    missing = referenced - produced
    return {
        "check": "hallucination_check",
        "ok": not missing,
        "detail": f"{len(missing)} ungrounded refs" if missing else "all_refs_grounded",
    }


_GROUNDING_CHECKS = {
    "source_citation": _check_source_citation,
    "hallucination_check": _check_hallucination,
}


def verify_grounding(execution, evaluation_profile: Optional[dict]) -> list[dict]:
    """Run the agent's evaluation_profile.grounding_checks deterministically.

    Args:
        execution: An ``Execution`` ORM row with ``.observations`` and
            ``.assistant_content`` loaded.
        evaluation_profile: The agent's ``evaluation_profile`` dict (or None).
            May contain ``grounding_checks`` (list of check names).

    Returns:
        A list of check dicts in the same ``{check, ok, detail}`` shape
        used by the rest of the verifier, in the order the checks were
        declared. Unknown check names are silently skipped (logged at
        debug). Returns ``[]`` when no profile or no grounding_checks
        are configured. Never raises.
    """
    if not evaluation_profile:
        return []
    requested = list(evaluation_profile.get("grounding_checks") or [])
    if not requested:
        return []
    out: list[dict] = []
    for name in requested:
        impl = _GROUNDING_CHECKS.get(name)
        if impl is None:
            logger.debug("verify_grounding: unknown check %r — skipping", name)
            continue
        try:
            out.append(impl(execution))
        except Exception as _chk_err:
            logger.warning("verify_grounding: check %r raised (non-fatal): %s", name, _chk_err)
            out.append({
                "check": name,
                "ok": False,
                "detail": f"error: {_chk_err}",
            })
    return out


def verify_execution(
    db: Session,
    execution,
    plan: Any = None,
) -> VerificationResult:
    """Run the deterministic VERIFY checks against an Execution.

    Args:
        db: SQLAlchemy session.
        execution: An ``Execution`` ORM row with ``.observations`` loaded.
        plan: Optional ``Plan``. When provided, the deterministic checks are
            *scoped* to this plan's observations only — so a corrective
            re-plan is not penalized for the failed observations of the plan
            it replaced. (In the single-plan case this is equivalent to
            checking all observations.)

    Returns:
        A :class:`VerificationResult`. Never raises; a malformed execution
        yields ``passed=False`` with the offending check detail.
    """
    observations = list(execution.observations or [])
    if plan is not None:
        valid_node_ids = {
            n.id for n in (getattr(plan, "nodes", None) or [])
        }
        if valid_node_ids:
            observations = [o for o in observations if o.plan_node_id in valid_node_ids]

    checks: list[dict] = []
    try:
        checks.append(_check_artifact_exists(db, observations))
    except Exception as e:
        logger.warning("VERIFY: artifact_exists check raised: %s", e)
        checks.append({"check": "artifact_exists", "ok": False, "detail": f"error: {e}", "critical": True})
    try:
        checks.append(_check_observation_success(observations))
    except Exception as e:
        logger.warning("VERIFY: observation_success check raised: %s", e)
        checks.append({"check": "observation_success", "ok": False, "detail": f"error: {e}", "critical": True})
    try:
        checks.append(_check_data_integrity(observations))
    except Exception as e:
        logger.warning("VERIFY: data_integrity check raised: %s", e)
        checks.append({"check": "data_integrity", "ok": False, "detail": f"error: {e}", "critical": False})
    try:
        checks.append(_check_degenerate_result(observations))
    except Exception as e:
        logger.warning("VERIFY: degenerate_result check raised: %s", e)
        checks.append({"check": "degenerate_result", "ok": False, "detail": f"error: {e}", "critical": True})
    try:
        checks.append(_check_wrong_grain(observations))
    except Exception as e:
        logger.warning("VERIFY: wrong_grain check raised: %s", e)
        checks.append({"check": "wrong_grain", "ok": False, "detail": f"error: {e}", "critical": False})
    try:
        plan_nodes = list(getattr(plan, "nodes", None) or []) if plan else None
        checks.append(_check_coverage(observations, plan_nodes))
    except Exception as e:
        logger.warning("VERIFY: coverage check raised: %s", e)
        checks.append({"check": "coverage", "ok": False, "detail": f"error: {e}", "critical": True})

    result = VerificationResult(checks)
    logger.info(
        "VERIFY execution=%s passed=%s checks=%s",
        getattr(execution, "id", "?"),
        result.passed,
        [(c["check"], c["ok"]) for c in checks],
    )
    return result


def verify_with_llm(
    execution,
    result: VerificationResult,
    llm_callable: Optional[LLMRubricFn] = None,
    endpoint=None,
) -> list[dict]:
    """Optional qualitative LLM rubric pass.

    Gated by ``settings.SYNEXIA_VERIFIER_LLM_ENABLED``. When that flag is
    False (the default), this function short-circuits and returns ``[]``.

    The default ``llm_callable`` invokes the shared LLM service with a
    classify-prompt that asks the model to evaluate the execution's
    observations as a JSON rubric. Tests can inject a stub via
    ``llm_callable``. ``endpoint`` (hierarchical LLMEndpoint) pins the
    rubric call to a specific provider+model.
    """
    try:
        from app.config import settings
    except Exception:
        return []

    if not getattr(settings, "SYNEXIA_VERIFIER_LLM_ENABLED", False):
        return []

    if llm_callable is None:
        # Default: shared LLM service. Import lazily so a missing/broken
        # llm_service never blocks the deterministic path.
        try:
            from app.services.llm_service import chat_completion_json_sync
        except Exception as e:
            logger.debug("VERIFY: llm service unavailable (%s); skipping LLM pass", e)
            return []
        if endpoint is not None:
            llm_callable = lambda prompt: chat_completion_json_sync(  # noqa: E731
                prompt, endpoint=endpoint,
            )
        else:
            llm_callable = chat_completion_json_sync  # type: ignore[assignment]

    observations = list(execution.observations or [])
    obs_summary = [
        {
            "id": getattr(o, "id", None),
            "tool_name": getattr(o, "tool_name", None),
            "success": getattr(o, "success", None),
            "result_data": getattr(o, "result_data", None),
        }
        for o in observations
    ]
    prompt = (
        "You are a verifier. Given the following observations, return a strict "
        "JSON object of the form "
        '{"checks": [{"name": str, "ok": bool, "detail": str}, ...]}. '
        "Be conservative — flag anything that looks inconsistent, missing, or "
        "low-quality. Do not invent data.\n\n"
        f"OBSERVATIONS:\n{obs_summary!r}"
    )
    try:
        out = llm_callable(prompt) or {}
        llm_checks = out.get("checks", [])
        if not isinstance(llm_checks, list):
            return []
        return [
            {
                "check": str(c.get("name", "llm")),
                "ok": bool(c.get("ok", False)),
                "detail": str(c.get("detail", "")),
            }
            for c in llm_checks
        ]
    except Exception as e:
        logger.warning("VERIFY: LLM rubric pass failed (non-fatal): %s", e)
        return []
