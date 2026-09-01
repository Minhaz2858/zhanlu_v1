"""v3 loop: multi-iteration content accumulator.

Tests that the v3 stream loop preserves ALL iterations' prose content
when the loop runs multiple iterations. Without the fix, only the LAST
iteration's content survives in `assistant_msg["content"]` and the
`done` event's `content` field — earlier iterations' prose (tables,
key findings, recommendations) is lost.

Symptom (user report 2026-08-19): During streaming, the chat shows the
full response (description + tables + key findings + recommendations +
a late "let me verify..." text). After completion, ONLY the artifact
cards (KPI/chart/insights) + the short late text remain — all the
markdown prose between is GONE.

Root cause:
  agents.py:7976  `assistant_content = ""` resets per iteration
  agents.py:8010  `assistant_content += ev_data` accumulates WITHIN iter only
  agents.py:9635  `assistant_msg["content"] = assistant_content` (last iter only)
  agents.py:9850  `done.content = assistant_content` (last iter only)

Frontend `done` handler also OVERWRITES the locally-streamed delta
buffer (which has all iter content) with the server's truncated
`evt.content` — covered separately by the frontend defensive fix.

This test follows the project's structural-test convention (see
`test_agents_force_synthesis.py:test_loop_exit_monotonic_stamped`).
"""

from __future__ import annotations

import app.routers.agents as agents_mod


_SOURCE = open(agents_mod.__file__).read()


# ── Accumulator is declared before the v3 loop ─────────────────────────────


def test_accumulator_declared_before_loop() -> None:
    """A `_v3_iter_contents` list must be initialized before the v3 loop
    starts so we can capture per-iteration content across iterations."""
    assert "_v3_iter_contents" in _SOURCE, (
        "v3 stream must declare `_v3_iter_contents` to capture multi-iter content"
    )
    # Must appear BEFORE the loop starts
    decl_pos = _SOURCE.find("_v3_iter_contents")
    loop_pos = _SOURCE.find("for iteration in range(MAX_TOOL_ITERATIONS):", decl_pos)
    assert loop_pos > decl_pos, (
        "`_v3_iter_contents` must be initialized before the v3 loop starts"
    )


# ── Per-iteration capture BEFORE the reset ────────────────────────────────


def test_capture_before_reset() -> None:
    """At line 7976 (where `assistant_content = ""` resets per iter), the
    code MUST first append the previous iter's content to the accumulator.
    Otherwise multi-iter turns lose all but the last iter's content."""
    # Find the reset pattern within the v3 loop
    reset_marker = "assistant_content = \"\""
    # We expect to see the accumulator capture appear inline before the reset.
    # Specifically: `if assistant_content:\n    _v3_iter_contents.append(assistant_content)\n*assistant_content = ""`
    # We assert the capture line exists and precedes the reset in source order.
    capture_line = "_v3_iter_contents.append(assistant_content)"
    assert capture_line in _SOURCE, (
        "v3 stream must append previous iter's content to `_v3_iter_contents` "
        "before resetting `assistant_content = \"\"` for the next iter"
    )
    capture_pos = _SOURCE.find(capture_line)
    # The reset must appear AFTER the capture (within the same iteration block)
    reset_pos = _SOURCE.find(reset_marker, capture_pos)
    assert reset_pos > capture_pos, (
        "`_v3_iter_contents.append(...)` must precede the `assistant_content = \"\"` reset"
    )


# ── Post-loop accumulation + use in assistant_msg + done event ─────────────


def test_accumulated_content_used_for_assistant_msg() -> None:
    """`assistant_msg["content"]` must be set to the accumulated multi-iter
    content, NOT raw `assistant_content` (which only holds the LAST iter)."""
    assert "accumulated_content" in _SOURCE, (
        "v3 stream must compute `accumulated_content` from `_v3_iter_contents`"
    )
    # The assistant_msg construction must use accumulated_content for the
    # content field (not raw assistant_content).
    assert '"content": accumulated_content' in _SOURCE or \
           '"content": accumulated_content,' in _SOURCE, (
        "`assistant_msg` must set `content` to `accumulated_content` (multi-iter concat)"
    )


def test_accumulated_content_used_for_done_event() -> None:
    """The `done` SSE event must carry the accumulated content, NOT raw
    `assistant_content`. The frontend relies on `done.content` to render
    the final chat message — if it's truncated, all earlier iter prose
    is lost."""
    assert '"content": accumulated_content' in _SOURCE, (
        "v3 stream `done` event must send `accumulated_content` as content"
    )


def test_final_iter_appended_post_loop() -> None:
    """After the loop ends, the FINAL iteration's `assistant_content` must
    also be appended to the accumulator (the loop's reset-at-top pattern
    captures iter 1..N-1, but iter N's content needs an explicit post-loop
    capture)."""
    # Look for the post-loop append: should appear after the loop ends
    # but before `assistant_msg` construction.
    # The pattern we look for is a SECOND `_v3_iter_contents.append(...)`
    # call that handles the post-loop final iter capture.
    occurrences = []
    pos = 0
    while True:
        idx = _SOURCE.find("_v3_iter_contents.append", pos)
        if idx == -1:
            break
        occurrences.append(idx)
        pos = idx + 1
    assert len(occurrences) >= 2, (
        "Expected at least 2 `_v3_iter_contents.append(...)` calls: "
        f"one before each iter reset, one for the final iter post-loop. Found {len(occurrences)}."
    )


def test_quality_eval_replaces_last_entry() -> None:
    """When quality eval revises `assistant_content` post-loop, the LAST
    entry of `_v3_iter_contents` must be replaced so the revision is
    preserved while earlier iters stay intact."""
    # Look for the pattern: after quality eval revises assistant_content,
    # replace the last accumulator entry.
    qe_marker = "_qe_result.final_text"
    assert qe_marker in _SOURCE, "test prerequisite: quality eval block must exist"
    qe_pos = _SOURCE.find(qe_marker)
    # Find the next occurrence of _v3_iter_contents[-1] = ... after qe
    replace_pattern = "_v3_iter_contents[-1] ="
    replace_pos = _SOURCE.find(replace_pattern, qe_pos)
    assert replace_pos > qe_pos, (
        "After quality eval revises `assistant_content`, must replace "
        "`_v3_iter_contents[-1]` so the revision is preserved while "
        "earlier iters stay intact"
    )


def test_separator_is_double_newline() -> None:
    """Iterations are joined with `"\\n\\n"` (markdown paragraph break).
    Other separators (e.g., horizontal rule) would add visual noise."""
    # Look for the specific join that operates on `_v3_iter_contents`
    # (other `"\\n\\n".join(...)` calls exist in the file — e.g. the
    # attachment context builder — so we anchor on the accumulator
    # variable itself to avoid false matches).
    join_anchor = 'accumulated_content = "\\n\\n".join('
    assert join_anchor in _SOURCE, (
        "v3 stream must join iter contents with `\\n\\n` separator "
        "into `accumulated_content`"
    )
    join_pos = _SOURCE.find(join_anchor)
    following = _SOURCE[join_pos:join_pos + 120]
    assert "_v3_iter_contents" in following, (
        "`\\n\\n`.join(...) must operate on `_v3_iter_contents`"
    )


# ── Single-iter regression safety ─────────────────────────────────────────


def test_single_iter_no_regression_doc() -> None:
    """Structural guarantee: in single-iteration turns, the accumulator
    has one entry, the joined result equals that entry — no behavior
    change. This is a code-comment expectation test."""
    # The filter `for c in _v3_iter_contents if c` must be present so
    # empty iter entries (tool-only iters with no prose) are skipped.
    assert "for c in _v3_iter_contents if c" in _SOURCE or \
           "for c in _v3_iter_contents if c.strip()" in _SOURCE, (
        "Must filter empty iter entries to avoid blank-line noise from "
        "tool-only iterations"
    )
