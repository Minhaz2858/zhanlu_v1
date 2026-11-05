"""Final-answer recovery decisions (2026-10-29 cycle 2).

Behavioural regression basis: the 2026-10-29 controlled replay streamed a
complete, data-grounded 5,042-character report; a guard then discarded it
and an emergency re-synthesis returned a single 89-character sentence,
which was accepted because it exceeded 50 characters — REPLACING the full
report on stream, terminal frame and persistence alike.

This module holds the decision rules so they can be unit-tested without
instantiating the 6,000-line stream generator:

- ``register_best`` — grow-only registration of the best completed answer
  seen in a turn (never regress to a shorter text).
- ``accept_resynthesis`` — a recovery re-synthesis may only REPLACE an
  already-preserved best answer if it is not drastically shorter; a
  one-sentence fragment must never overwrite a full report.
- ``final_answer`` — the resulting canonical content, preferring the
  preserved best (with any trailing pending-work sentence removed) over a
  degraded replacement.

These are content-shape rules only: they know nothing about any
datasource, schema, or business domain.
"""
from __future__ import annotations

import re

#: How short a re-synthesis may be relative to the preserved best before
#: it is considered a degradation (candidate must reach 1/3 of the best).
_DEGRADATION_RATIO = 3

#: Absolute floor for accepting any recovery re-synthesis.
_DEFAULT_MIN_LEN = 50

_TRAILING_PENDING_RE = re.compile(
    r"\s*(?:\n+)?[^\n]{0,200}?(?:want me to|want this|shall i|would you like|i can also|"
    r"if you (?:'d like|want)|let me know if)[^\n]*\??\s*$",
    re.IGNORECASE,
)


def strip_trailing_pending(text: str) -> str:
    """Remove a trailing offer/pending-work sentence ("Want this as a PDF?").

    Deterministic, content-agnostic: only the final sentence is considered,
    only when the remainder stays non-trivial. This keeps honest reports
    that end with a follow-up offer without discarding the whole answer.
    """
    if not text:
        return text
    stripped = _TRAILING_PENDING_RE.sub("", text).rstrip()
    if len(stripped) >= max(200, len(text) // 2):
        return stripped
    return text


def register_best(store: list[str], text: str) -> None:
    """Grow-only: keep the LONGEST completed answer seen in this turn.

    ``store`` is the generator's one-slot recovery list; passed in so the
    rule is testable and the ownership stays with the caller. Tolerates the
    generator's ``[None]`` initialization (store[0] is None until the first
    registration) — a None slot is treated as empty, never crash the stream.
    """
    if not text or not text.strip():
        return
    if not store:
        store.append(text)
        return
    current = store[0]
    if current is None or len(text) > len(current):
        store[0] = text


def accept_resynthesis(
    candidate: str,
    preserved_best: str,
    min_len: int = _DEFAULT_MIN_LEN,
) -> bool:
    """May ``candidate`` replace ``preserved_best`` as the final answer?

    Rules:
    - Empty/nonsense candidates: never.
    - No preserved best: the ordinary floor (``min_len``) applies.
    - With a preserved best: the candidate must reach 1/3 of its length —
      a 89-char sentence may not overwrite a 5,042-char report.
    """
    cand = (candidate or "").strip()
    if len(cand) < min_len:
        return False
    best = (preserved_best or "").strip()
    if not best:
        return True
    return len(cand) * _DEGRADATION_RATIO >= len(best)


def final_answer(
    candidate: str,
    preserved_best: str,
    min_len: int = _DEFAULT_MIN_LEN,
) -> str:
    """Canonical final content for a recovery path.

    Returns ``candidate`` when :func:`accept_resynthesis` allows it;
    otherwise the preserved best with any trailing pending-work sentence
    stripped (the report stays complete and the offer sentence — the usual
    reason a guard discarded it — is removed deterministically).
    """
    if accept_resynthesis(candidate, preserved_best, min_len):
        return candidate
    best = (preserved_best or "").strip()
    if best:
        return strip_trailing_pending(best)
    return candidate
