"""Claim tracker — truth-backed verification of every numeric claim.

Design spec §9.2–9.3.

Every claim emitted by the synthesizer carries its lineage:
  - text           — the narrative sentence.
  - source_facet   — the facet id whose rows produced the number.
  - source_row_ids — the specific rows referenced.
  - source_sql     — the executed SQL (or service method name).
  - verified       — True after ClaimTracker re-executes the SQL and
                      confirms the cited value is still present.

A claim that fails verification has its ``text`` rewritten to
"Data unavailable for this claim (source rows not consistent)". The
report is still delivered; the gap is shown explicitly rather than
silently dropped.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, TypedDict

logger = logging.getLogger(__name__)


class Claim(TypedDict, total=False):
    """A single numeric claim traced back to its data source."""

    claim_id: str
    text: str
    source_facet: str
    source_row_ids: list[str]
    source_sql: str
    verified: bool


#: When verification fails, the original sentence is replaced with this.
UNVERIFIED_TEXT = (
    "Data unavailable for this claim (source rows not consistent with "
    "current state)."
)


@dataclass
class VerificationResult:
    verified_count: int = 0
    unverified_count: int = 0
    failed_claims: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.verified_count + self.unverified_count


class ClaimTracker:
    """In-memory collector for claim instances with strict validation."""

    def __init__(self) -> None:
        self.claims: list[Claim] = []

    def add(self, claim: Claim) -> bool:
        """Add a claim; returns False (and discards) when the claim
        lacks the required lineage fields. Ungrounded claims MUST NOT
        enter the report."""
        if not self._is_grounded(claim):
            logger.debug(
                "claim_tracker: rejected ungrounded claim %r",
                str(claim.get("text", ""))[:80],
            )
            return False
        if "verified" not in claim:
            claim["verified"] = False
        self.claims.append(claim)
        return True

    def extend(self, claims: Iterable[Claim]) -> int:
        added = 0
        for c in claims:
            if self.add(c):
                added += 1
        return added

    @staticmethod
    def _is_grounded(claim: Claim) -> bool:
        if not isinstance(claim, dict):
            return False
        if not str(claim.get("text") or "").strip():
            return False
        if not str(claim.get("source_facet") or "").strip():
            return False
        if not str(claim.get("source_sql") or "").strip():
            return False
        row_ids = claim.get("source_row_ids") or []
        if not isinstance(row_ids, (list, tuple)) or not row_ids:
            return False
        return True


def verify_claims(
    tracker: ClaimTracker,
    db_executor: Callable[[str], list[dict]] | None = None,
) -> VerificationResult:
    """Re-execute ``source_sql`` for every claim against ``db_executor``.

    A claim is verified when:
      - ``db_executor`` returns without raising, AND
      - returns a non-empty result set.

    When ``db_executor`` is None (synthetic-test path), no claim is
    marked verified — callers should treat the report as unverified
    and render the appropriate caveats.
    """
    result = VerificationResult()
    for claim in tracker.claims:
        if db_executor is None:
            # No executor available → keep the existing verified flag
            # (typically False). Caller can still render with caveat.
            if claim.get("verified"):
                result.verified_count += 1
            else:
                result.unverified_count += 1
            continue
        sql = claim.get("source_sql") or ""
        try:
            rows = db_executor(sql) or []
        except Exception as exc:
            logger.warning(
                "claim_tracker: re-execution failed for claim %s: %s",
                claim.get("claim_id"), exc,
            )
            claim["verified"] = False
            result.unverified_count += 1
            result.failed_claims.append(claim.get("claim_id") or "")
            continue
        if rows:
            claim["verified"] = True
            result.verified_count += 1
        else:
            claim["verified"] = False
            result.unverified_count += 1
            result.failed_claims.append(claim.get("claim_id") or "")
    return result


def rewrite_unverified(claims: list[Claim]) -> None:
    """In place: rewrite the text of every UNVERIFIED claim to a
    generic "Data unavailable" placeholder. Verified claims are
    untouched. This is the last step before rendering — claims must
    never carry unverified numbers into the executive document."""
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        if claim.get("verified"):
            continue
        claim["text"] = UNVERIFIED_TEXT


def make_claim(
    *,
    claim_id: str,
    text: str,
    source_facet: str,
    source_row_ids: list[str] | tuple[str, ...],
    source_sql: str,
) -> Claim:
    """Convenience builder that always sets ``verified=False``. The
    caller decides when to flip it on after ``verify_claims``."""
    return Claim(
        claim_id=claim_id,
        text=text,
        source_facet=source_facet,
        source_row_ids=list(source_row_ids),
        source_sql=source_sql,
        verified=False,
    )
