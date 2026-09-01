"""Regression: Skill Agent bare-request flow uses a single multi-select
:::options block (2026-07-28).

Replaces the previous 'Ask 2-3 focused questions' pattern (which
produced 3 separate :::options blocks, one per dimension, requiring
the user to send 3 times) with a single multi-select block: the agent
commits to defaults in one sentence, then emits ONE :::options block
with 3-5 dimension-prefixed chips the user can multi-select.

These tests pin the new prompt contract.
"""
from __future__ import annotations

import re

from app.services.agent_prompts import SKILL_AGENT_SYSTEM_PROMPT


def _bare_request_section(prompt: str) -> str:
    """Locate the '### Bare-Request Handling' section body."""
    m = re.search(
        r"###\s*Bare-Request Handling(.*?)(?=^###\s|\Z)",
        prompt,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert m, (
        "Skill Agent prompt is missing a '### Bare-Request Handling' section"
    )
    return m.group(1)


def _in_negation_context(text: str, pos: int, window: int = 30) -> bool:
    """True if the match at `pos` is preceded by a negation (never, don't, not, etc.).

    Tighter window than the previous-turn helper: we only consider the last
    ~30 chars (≤ 5-6 words). This avoids false positives where a "not" in
    an unrelated nearby clause makes a positive instruction look like a
    negation. We also require the negation to be in the same sentence
    (no intervening period, question mark, or newline).
    """
    start = max(0, pos - window)
    prefix = text[start:pos].lower()
    # No sentence/paragraph break between negation and the match.
    if re.search(r"[.?!\n]\s*$", prefix):
        return False
    return bool(re.search(
        r"\b(never|do(?:es)?n[''`]?t|didn[''`]?t|won[''`]?t|"
        r"wouldn[''`]?t|shouldn[''`]?t|cann[''`]?t|"
        r"mustn[''`]?t|can|do\s+not|does\s+not|did\s+not|"
        r"no|not)\b",
        prefix,
    ))


def test_bare_request_section_requires_single_options_block():
    """The Bare-Request Handling section must commit to ONE :::options block."""
    section = _bare_request_section(SKILL_AGENT_SYSTEM_PROMPT)
    assert re.search(
        r"single\s+`?:::options`?\s+block",
        section,
        flags=re.IGNORECASE,
    ), (
        "Bare-Request Handling must require a SINGLE ':::options' block. "
        "Found section text:\n" + section[:800]
    )


def test_bare_request_section_mentions_chip_count_guidance():
    """The new block should have 3-5 chips as guidance."""
    section = _bare_request_section(SKILL_AGENT_SYSTEM_PROMPT)
    assert re.search(r"\b3-5\b|\b3\s*[–-]\s*5\b", section), (
        "Bare-Request Handling must specify a chip-count guidance "
        "(e.g. '3-5 chips'). Found section text:\n" + section[:800]
    )


def test_bare_request_section_uses_dimension_prefix_convention():
    """Chips must be self-labeling via dimension prefix (Audience: Engineers)."""
    section = _bare_request_section(SKILL_AGENT_SYSTEM_PROMPT)
    # The example "Audience: Engineers" must appear (or "Audience:" + a value).
    assert re.search(r"Audience:\s*\w+", section), (
        "Bare-Request Handling must include a dimension-prefix example "
        "like 'Audience: Engineers'. Found section text:\n" + section[:800]
    )
    # Same for "Format:".
    assert re.search(r"Format:\s*\w+", section), (
        "Bare-Request Handling must include a 'Format: ...' example. "
        "Found section text:\n" + section[:800]
    )


def test_bare_request_section_forbids_old_ask_2_3_pattern():
    """The old 'Ask 2-3 questions' wording must not appear (only as a negation)."""
    section = _bare_request_section(SKILL_AGENT_SYSTEM_PROMPT)
    # Search for "Ask 2-3" / "ask 2-3" / "ask two or three" etc.
    for pat in [
        r"Ask\s+2[-–]\s*3\b",
        r"ask\s+2[-–]\s*3\b",
        r"Ask\s+two\s+or\s+three",
    ]:
        for m in re.finditer(pat, section, flags=re.IGNORECASE):
            assert _in_negation_context(section, m.start()), (
                f"Bare-Request Handling has a POSITIVE occurrence of old "
                f"pattern {pat!r} at position {m.start()}. The new prompt "
                f"must use a single multi-select block instead. "
                f"Surrounding context: "
                f"...{section[max(0,m.start()-30):m.end()+30]}..."
            )


def test_bare_request_section_forbids_second_round_of_questions():
    """The new prompt must forbid a second round of clarifying questions."""
    section = _bare_request_section(SKILL_AGENT_SYSTEM_PROMPT)
    # The section should explicitly say no second round.
    assert re.search(
        r"do\s+NOT\s+ask\s+a\s+second\s+round|"
        r"don[''`]?t\s+ask\s+a\s+second\s+round|"
        r"no\s+second\s+round|"
        r"never\s+ask\s+a\s+second",
        section,
        flags=re.IGNORECASE,
    ), (
        "Bare-Request Handling must explicitly forbid a second round of "
        "clarifying questions. The multi-select block is the ONE question. "
        f"Found section text:\n{section[:800]}"
    )


def test_bare_request_section_requires_committed_defaults_paragraph():
    """The agent MUST commit to defaults in one sentence before the options block."""
    section = _bare_request_section(SKILL_AGENT_SYSTEM_PROMPT)
    # Look for "MUST commit" / "must commit" / "commit to a complete default".
    assert re.search(
        r"MUST\s+commit|"
        r"must\s+commit|"
        r"commit\s+to\s+a\s+complete\s+default",
        section,
        flags=re.IGNORECASE,
    ), (
        "Bare-Request Handling must require the agent to commit to a "
        "complete default in one sentence (audience + format + focus + "
        "trigger) before emitting the multi-select block. "
        f"Found section text:\n{section[:800]}"
    )
