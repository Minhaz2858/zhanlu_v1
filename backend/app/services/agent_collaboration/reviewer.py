"""Critic / Reviewer workflow (P2 Phase 2, Part 1).

A reviewer judges a specialist's output against the ORIGINAL question and the
EVIDENCE that was actually produced — it never invents facts, never runs
database queries, and never replaces a real calculation with a guess.

The reviewer evaluates seven dimensions:
  1. numerical_accuracy      — do the reported numbers match the cited evidence?
  2. evidence_completeness   — is there enough evidence to support the claim?
  3. source_attribution      — are findings tied to a concrete source/provenance?
  4. contradictory_findings  — are there internal contradictions / conflicts?
  5. unsupported_conclusions — does the answer assert things not in the evidence?
  6. assumptions_uncertainty — are assumptions/uncertainty made explicit?
  7. question_answered        — does the output actually answer the user's ask?

If evidence is insufficient the reviewer returns ``needs_evidence`` — a list of
*targeted* gaps. The orchestrator turns those into follow-up delegations through
the Collaboration Broker (see delegate_tool._maybe_review_and_refine). The
reviewer itself never performs the investigation.

Invocation is SELECTIVE: simple questions should not trigger a reviewer. Callers
opt in (delegate_task review=True, or a DAG "reviewer" node). There is no
automatic-everything path.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from app.config import settings
from app.services.llm_service import chat_completion_json_sync

logger = logging.getLogger(__name__)

DIMENSIONS = [
    "numerical_accuracy",
    "evidence_completeness",
    "source_attribution",
    "contradictory_findings",
    "unsupported_conclusions",
    "assumptions_uncertainty",
    "question_answered",
]

# status vocabulary per dimension
OK = "ok"
WARN = "warn"
FAIL = "fail"

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "dimensions": {
            "type": "object",
            "properties": {
                d: {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "enum": [OK, WARN, FAIL]},
                        "detail": {"type": "string"},
                    },
                    "required": ["status"],
                }
                for d in DIMENSIONS
            },
            "required": DIMENSIONS,
        },
        "overall": {
            "type": "string",
            "enum": ["approved", "needs_revision", "rejected"],
        },
        "answered": {"type": "boolean"},
        "issues": {"type": "array", "items": {"type": "string"}},
        "needs_evidence": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Targeted gaps the orchestrator should investigate further.",
        },
        # Phase 2.3: structured gap fields consumed by the execution decision
        # model (evaluate_reviewer_findings reads exactly these keys).
        "unsupported_claims": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Assertions in the answer with no supporting evidence.",
        },
        "contradictions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Conflicting figures/findings across sources or within the answer.",
        },
        "failed_acceptance_criteria": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Acceptance criteria from the task that the answer fails.",
        },
    },
    "required": ["dimensions", "overall", "answered"],
}


@dataclass
class ReviewReport:
    """Structured, machine-readable reviewer verdict."""

    dimensions: dict = field(default_factory=dict)   # name -> {status, detail}
    overall: str = "needs_revision"
    answered: bool = False
    issues: list = field(default_factory=list)
    needs_evidence: list = field(default_factory=list)
    # Phase 2.3: structured gap fields (consumed by the execution decision
    # model). Derived DETERMINISTICALLY from the dimension details when the
    # LLM payload omits them — the fields are always present and honest.
    unsupported_claims: list = field(default_factory=list)
    contradictions: list = field(default_factory=list)
    failed_acceptance_criteria: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "dimensions": self.dimensions,
            "overall": self.overall,
            "answered": self.answered,
            "issues": self.issues,
            "needs_evidence": self.needs_evidence,
            "unsupported_claims": self.unsupported_claims,
            "contradictions": self.contradictions,
            "failed_acceptance_criteria": self.failed_acceptance_criteria,
        }

    @property
    def approved(self) -> bool:
        return self.overall == "approved"

    @classmethod
    def from_dict(cls, d: dict) -> "ReviewReport":
        dims = d.get("dimensions") or {}
        # Normalise: ensure every dimension has a status, default warn.
        norm = {}
        for name in DIMENSIONS:
            entry = dims.get(name) or {}
            status = entry.get("status") if isinstance(entry, dict) else None
            if status not in (OK, WARN, FAIL):
                status = WARN
            norm[name] = {
                "status": status,
                "detail": (entry.get("detail") if isinstance(entry, dict) else None) or "",
            }

        def _explicit(key: str) -> list:
            v = d.get(key)
            return [str(x) for x in v if str(x).strip()] if isinstance(v, list) else []

        def _derived(dim: str) -> list:
            """Deterministic derivation: a warn/fail dimension detail IS the
            structured gap (no invented facts — the reviewer authored it)."""
            entry = norm.get(dim) or {}
            if entry.get("status") in (WARN, FAIL) and (entry.get("detail") or "").strip():
                return [str(entry["detail"]).strip()]
            return []

        return cls(
            dimensions=norm,
            overall=d.get("overall") or "needs_revision",
            answered=bool(d.get("answered", False)),
            issues=list(d.get("issues") or []),
            needs_evidence=list(d.get("needs_evidence") or []),
            unsupported_claims=_explicit("unsupported_claims") or _derived("unsupported_conclusions"),
            contradictions=_explicit("contradictions") or _derived("contradictory_findings"),
            failed_acceptance_criteria=_explicit("failed_acceptance_criteria"),
        )

    @classmethod
    def conservative(cls, reason: str) -> "ReviewReport":
        """Used when the judge call is unavailable/unparseable — never say ok."""
        return cls(
            dimensions={n: {"status": WARN, "detail": "reviewer could not assess"} for n in DIMENSIONS},
            overall="needs_revision",
            answered=False,
            issues=[reason],
            needs_evidence=[],
        )


def _build_review_prompt(
    question: str,
    content: str,
    evidence_text: str,
    acceptance_criteria: Optional[list] = None,
) -> str:
    criteria_block = ""
    if acceptance_criteria:
        criteria_block = (
            "ACCEPTANCE CRITERIA (the answer is judged against these):\n"
            + "\n".join(f"- {c}" for c in acceptance_criteria)
            + "\nMark any criterion the answer does not satisfy under "
              "'failed_acceptance_criteria'.\n\n"
        )
    return (
        "You are a meticulous enterprise answer reviewer. Your job is to ASSESS "
        "the answer below against the original question and the provided evidence.\n\n"
        "HARD RULES (violations make your review invalid):\n"
        "- You MUST NOT invent facts, figures, or database results.\n"
        "- You MUST NOT replace any reported calculation with your own estimate.\n"
        "- You may ONLY judge what is present in the question, the answer, and the "
        "evidence. If something is missing, mark the relevant dimension as a GAP "
        "(warn/fail) and list it under 'needs_evidence' — do NOT fill it yourself.\n"
        "- An agent's unsupported textual claim does NOT become verified evidence "
        "merely because it is stated confidently.\n\n"
        f"{criteria_block}"
        f"ORIGINAL QUESTION:\n{question}\n\n"
        f"ANSWER TO REVIEW:\n{content}\n\n"
        f"EVIDENCE PRODUCED (references only, no raw rows):\n{evidence_text}\n\n"
        "Return strict JSON with these keys:\n"
        "- dimensions: for each of the seven named dimensions, an object "
        "{status: ok|warn|fail, detail: short reason}.\n"
        "- overall: approved | needs_revision | rejected.\n"
        "- answered: boolean — does the answer actually address the question?\n"
        "- issues: list of concrete problems (empty if none).\n"
        "- needs_evidence: list of TARGETED gaps the orchestrator should "
        "investigate next (empty if none). Each item must name the specific "
        "missing fact/provenance, not vague 'more data'.\n"
        "- unsupported_claims: assertions in the answer with no supporting "
        "evidence (empty if none).\n"
        "- contradictions: conflicting figures/findings across sources or "
        "within the answer (empty if none).\n"
        "- failed_acceptance_criteria: acceptance criteria the answer fails "
        "(empty if none).\n"
    )


def _evidence_to_text(evidence_items: list) -> str:
    if not evidence_items:
        return "(no evidence provided)"
    parts = []
    for e in evidence_items:
        d = e.to_dict() if hasattr(e, "to_dict") else e
        parts.append(
            f"- evidence_id={d.get('evidence_id')} producer={d.get('producer_agent')} "
            f"source={d.get('source_id') or d.get('datasource_id')} "
            f"validation={d.get('validation_status')} "
            f"findings={json.dumps(d.get('findings'), ensure_ascii=False, default=str)[:300]}"
        )
    return "\n".join(parts)


def review_findings(
    question: str,
    content: str,
    evidence_items: Optional[list] = None,
    endpoint: Any = None,
    acceptance_criteria: Optional[list] = None,
) -> ReviewReport:
    """Run the fact-safe reviewer. Returns a ReviewReport (never raises)."""
    if not getattr(settings, "COLLAB_REVIEW_ENABLED", True):
        # Reviewer disabled — treat as approved so normal flow proceeds.
        return ReviewReport(
            dimensions={n: {"status": OK, "detail": "reviewer disabled"} for n in DIMENSIONS},
            overall="approved",
            answered=True,
            issues=[],
            needs_evidence=[],
        )
    try:
        prompt = _build_review_prompt(
            question, content or "", _evidence_to_text(evidence_items or []),
            acceptance_criteria=acceptance_criteria,
        )
        raw = chat_completion_json_sync(prompt, schema=REVIEW_SCHEMA, temperature=0.0, endpoint=endpoint)
        if not raw or not isinstance(raw, dict):
            return ReviewReport.conservative("reviewer returned no parseable verdict")
        return ReviewReport.from_dict(raw)
    except Exception as e:  # noqa: BLE001 — reviewer is advisory; never block the answer
        logger.debug("reviewer failed (non-fatal): %s", e)
        return ReviewReport.conservative(f"reviewer error: {e}")


def review_dict(question: str, content: str, evidence_items=None, endpoint=None) -> dict:
    """Convenience wrapper returning a plain dict for delegation results."""
    return review_findings(question, content, evidence_items, endpoint).to_dict()
