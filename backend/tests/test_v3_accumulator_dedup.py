"""v3 loop: accumulator dedup on nudge re-entry.

Regression guard for the live trace (Daily Sales Data Sync, 2026-08-20):
the `_v3_iter_contents` accumulator captured each iteration's prose before
reset. When ANY nudge (verification gate, pptx guard, file guard, goal-
contract unmet) caused `continue`, the next iteration generated new
`assistant_content` which got appended again at L8163. The `"\n\n".join()`
produced duplicated content in the final response (the entire report
appeared twice).

This test verifies that every nudge `continue` site pops the just-captured
iter content before continuing, so the re-iteration's prose replaces the
original instead of being appended alongside it.

The 4 nudge-continue sites (all inside the v3 loop):
1. Goal-contract unmet → continue (line ~8415)
2. PPTX turn-guard nudge → continue (line ~8471)
3. File turn-guard nudge → continue (line ~8503)
4. Answer-verification gate nudge → continue (line ~8547)
"""

from __future__ import annotations

import re

import app.routers.agents as agents_mod


_SOURCE = open(agents_mod.__file__).read()


def _find_v3_loop_block() -> str:
    """Return the source code of the v3 stream loop (the second
    ``for iteration in range(MAX_TOOL_ITERATIONS):`` block — the first
    is the v2 stream). The v3 loop body spans ~2700 lines so we bound
    the return at 250_000 chars (more than enough for all nudge sites)."""
    # The v3 loop is the one that initialises `_v3_iter_contents`
    # immediately before it.
    anchor = "_v3_iter_contents: list[str] = []"
    assert anchor in _SOURCE, (
        "v3 loop anchor not found — the v3 accumulator pattern was removed"
    )
    # From the anchor, scan forward to the next
    # ``for iteration in range(MAX_TOOL_ITERATIONS):`` — that's the v3 loop.
    start = _SOURCE.find("for iteration in range(MAX_TOOL_ITERATIONS):", _SOURCE.find(anchor))
    assert start != -1
    # Bound at 250_000 chars — the v3 loop body contains ~2700 lines.
    return _SOURCE[start:start + 250_000]


# ── All 4 nudge sites have _v3_iter_contents.pop() before continue ──────────


def test_all_four_nudge_sites_have_pop_before_continue() -> None:
    """Each of the 4 nudge-continue sites in the v3 loop must pop the just-
    captured iter content. Otherwise the re-iteration's prose is appended
    alongside the original, producing duplicated response content."""
    block = _find_v3_loop_block()

    pop_positions = [m.start() for m in re.finditer(
        r"_v3_iter_contents\.pop\(\)", block
    )]
    continue_positions = [m.start() for m in re.finditer(
        r"^\s+continue\s*$", block, re.MULTILINE
    )]

    # Each `continue` inside the v3 loop body should be preceded by a
    # `pop()` call within ~30 lines.
    pops_within_30 = 0
    for c_pos in continue_positions:
        for p_pos in reversed(pop_positions):
            if p_pos < c_pos and (c_pos - p_pos) < 2400:  # ~30 lines @ 80 chars/line
                pops_within_30 += 1
                break

    # We have at least 4 nudge sites (pptx, file, gate, goal-contract).
    assert pops_within_30 >= 4, (
        f"Expected at least 4 _v3_iter_contents.pop() sites before "
        f"`continue` in the v3 loop body (one per nudge site), got "
        f"{pops_within_30}. Without these pops, the agent's re-emit after "
        f"a nudge duplicates the previous iteration's prose in the final "
        f"response (the Daily Sales Data Sync bug from 2026-08-20)."
    )


# ── Per-site dedup ────────────────────────────────────────────────────────


def _find_nudge_block(marker_before: str, marker_after: str = "continue") -> str:
    """Return the source between ``marker_before`` and the next ``continue``."""
    start = _SOURCE.find(marker_before)
    assert start != -1, f"marker not found: {marker_before!r}"
    end = _SOURCE.find(marker_after, start)
    assert end != -1, f"end marker not found: {marker_after!r}"
    return _SOURCE[start:end]


def test_pptx_nudge_dedups_accumulator() -> None:
    """PPTX guard nudge → continue must pop the just-captured iter content."""
    block = _find_nudge_block("v3 stream: pptx turn-guard nudge injected")
    assert "_v3_iter_contents.pop()" in block, (
        "PPTX nudge continue must pop _v3_iter_contents to prevent the "
        "next iteration's prose from being appended alongside the pre-nudge "
        "prose (causes the entire response to be duplicated)."
    )


def test_file_nudge_dedups_accumulator() -> None:
    """File turn-guard nudge → continue must pop the just-captured iter content."""
    block = _find_nudge_block("v3 stream: file turn-guard nudge injected")
    assert "_v3_iter_contents.pop()" in block, (
        "File nudge continue must pop _v3_iter_contents to prevent the "
        "next iteration's prose from being appended alongside the pre-nudge "
        "prose."
    )


def test_verification_gate_nudge_dedups_accumulator() -> None:
    """Answer-verification gate nudge → continue must pop the just-captured
    iter content. This is the PRIMARY offender from the user's repro: the
    gate fired on 'markdown/outcome/running' false positives, the agent
    re-emitted the report, and both copies ended up in the accumulator."""
    block = _find_nudge_block("P2.2: Universal Self-Evaluation & Re-Planning gate (v3 loop)")
    assert "_v3_iter_contents.pop()" in block, (
        "Verification gate nudge continue must pop _v3_iter_contents. This "
        "is the primary offender for the 'Daily Sales Data Sync' duplication "
        "bug — the gate fired on automation metadata tokens, the agent "
        "re-emitted, and the entire report appeared in the final output twice."
    )


def test_goal_contract_nudge_dedups_accumulator() -> None:
    """Goal-contract unmet → continue must pop the just-captured iter content."""
    block = _find_nudge_block("v3 stream: goal-contract unmet")
    assert "_v3_iter_contents.pop()" in block, (
        "Goal-contract unmet continue must pop _v3_iter_contents to prevent "
        "the forced-tool/synthesis-replacement iteration from appending "
        "alongside the pre-unmet prose."
    )


# ── Empty-list safety ─────────────────────────────────────────────────────


def test_pop_is_guarded_against_empty_list() -> None:
    """Every `_v3_iter_contents.pop()` must be guarded with `if _v3_iter_contents:`
    to avoid IndexError on the very first iteration when the accumulator
    hasn't been written yet (a tool-only first iter leaves it empty)."""
    pop_positions = [m.start() for m in re.finditer(
        r"if _v3_iter_contents:\s*\n\s*_v3_iter_contents\.pop\(\)", _SOURCE
    )]
    # v3 has 4 nudge sites — expect at least 4 guarded pop sites.
    assert len(pop_positions) >= 4, (
        f"Expected at least 4 guarded `_v3_iter_contents.pop()` sites "
        f"(one per nudge site in the v3 loop), got {len(pop_positions)}. "
        f"Unguarded pop calls would raise IndexError on tool-only first iters."
    )