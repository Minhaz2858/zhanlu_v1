"""Duplicate-answer guard — collapse a byte-identical duplicated tail from a
final answer before it is persisted / emitted as ``done``.

Measured live (2026-10-19, automation run, session db04120f): the persisted
assistant message contained the SAME ~4.9k-char report twice, concatenated
with NO separator. Both halves normalized-identical → a mechanical
duplicate somewhere in the multi-layer assembly (streamed deltas + forced
regeneration + post-loop synthesis each hold a full copy of the answer).

Rather than chasing every assembly path, the guard runs ONCE at the final
assembly point (agents.py, right after the multi-iteration join and the
narration strips, before persist + done) and collapses the duplicate tail
whatever produced it. Same philosophy as the existing collapse guards.

Design rules:
- Only fires when the tail of the content repeats an earlier block
  (whitespace-normalized identical) — a normal answer whose second half is
  genuinely different text is never touched.
- Minimum block length (default 400 normalized chars) so short accidental
  repeats are never mangled.
- Handles the live shapes: direct concatenation (no separator), "\\n\\n"
  join, trailing-newline drift, and 3+ copies.
- Returns the collapsed text + a fired flag; the caller logs loudly so the
  upstream duplication source identifies itself in the logs.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

#: Minimum normalized block length before the guard will consider firing.
MIN_BLOCK_CHARS = 400


def _normalize(text: str) -> str:
    """Whitespace-insensitive form for identity comparison."""
    return re.sub(r"\s+", " ", text or "").strip()


def is_duplicated_answer(text: str) -> bool:
    """True when ``text`` ends with a repeat of an earlier block."""
    _, fired = collapse_duplicate_tail(text)
    return fired


def collapse_duplicate_tail(text: str) -> tuple[str, bool]:
    """Collapse a duplicated tail. Returns (content, fired).

    Repeats until stable so a 3×/4× duplication collapses to one copy.
    """
    original = text or ""
    content = original
    fired = False
    for _ in range(3):
        result = _collapse_once(content)
        if result is None:
            break
        content = result
        fired = True
    if fired:
        logger.warning(
            "duplicate_answer_guard: collapsed duplicated answer tail "
            "(%d chars → %d chars) — identical repeated block; upstream "
            "assembly duplicated the answer",
            len(original), len(content),
        )
    return content, fired


def _collapse_once(content: str) -> str | None:
    """One collapse pass, or None when no duplicated tail is present.

    content = P + B + B'  →  returns P + B  (B' ≈ B normalized, and
    len(B') >= MIN_BLOCK_CHARS). Pure doubling is P = "".
    """
    if not content:
        return None
    if len(_normalize(content)) < MIN_BLOCK_CHARS * 2:
        return None

    mid = len(content) // 2
    # Probe: substantial lines of the SECOND half. The duplicated block's
    # last copy starts with one of them (the live failure began the second
    # copy with the report's "## Last Week Sales Overview" heading). The
    # midpoint may land mid-paragraph, so try several candidate lines.
    candidates = [
        ln for ln in content[mid:].splitlines()
        if len(_normalize(ln)) >= 20
    ][:6]

    for probe_raw in candidates:
        positions: list[int] = []
        start = 0
        while True:
            i = content.find(probe_raw, start)
            if i < 0:
                break
            positions.append(i)
            start = i + len(probe_raw)
        if len(positions) < 2:
            continue

        last = positions[-1]
        tail = content[last:]
        norm_tail = _normalize(tail)
        if len(norm_tail) < MIN_BLOCK_CHARS:
            continue
        head_raw = content[:last]
        head_n = _normalize(head_raw)
        # The tail must appear in the head (it repeats the answer's own
        # ending). A SHORT normalized drift between the first copy and the
        # tail (e.g. the duplicated block's heading line, ≤80 chars) is
        # absorbed by cutting at the drift's raw start.
        idx = head_n.rfind(norm_tail)
        if idx < 0:
            continue
        drift_n = head_n[idx + len(norm_tail):]
        if not drift_n:
            return head_raw.rstrip()
        if len(drift_n) > 80:
            continue
        cut = _raw_cut_for_drift(head_raw, drift_n)
        if cut is None or cut <= 0:
            continue
        return content[:cut].rstrip()
    return None


def _raw_cut_for_drift(head_raw: str, drift_n: str) -> int | None:
    """Raw position where the drift block (e.g. a duplicated heading)
    begins — its LAST occurrence in the head, whitespace-flexible."""
    tokens = drift_n.split()[:8]
    if not tokens:
        return None
    pat = r"\s+".join(re.escape(t) for t in tokens)
    last_match = None
    for m in re.finditer(pat, head_raw):
        last_match = m.start()
    return last_match
