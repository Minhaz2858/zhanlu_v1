"""R4 regression test — TOOL_CALL_HARD_CAP is now 10.

R3 set the cap to 3 to fix the `skills` `load_skill` ImportError loop.
But legitimate skill discovery (1 `list_tools` + 2-3 `skills(load, X)`
for different candidates) trips the cap at 3, so the agent never
reaches `create_agent`. The system prompt now mandates a bounded
discovery budget (1 list_tools, 1 list_market_agents, 3 skills(load))
plus 1-2 iterations of decision-summary negotiation. TOOL_CALL_HARD_CAP
= 10 is a comfortable ceiling: smart detection (name + canonicalized
arguments key) still blocks exact-repeat loops while permitting the
expected investigation pattern, and it stays well below the raised
per-turn MAX_TOOL_ITERATIONS=40 so a single bad tool cannot eat the
whole budget.
"""
import os
import re

_AGENTS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "app", "routers", "agents.py"
)


def _load_source():
    with open(_AGENTS_PATH) as f:
        return f.read()


def test_tool_call_hard_cap_is_ten():
    """TOOL_CALL_HARD_CAP must be 10 (R4 fix) — not 3 (R3) or 1."""
    src = _load_source()
    m = re.search(r"^TOOL_CALL_HARD_CAP\s*=\s*(\d+)\s*$", src, re.MULTILINE)
    assert m, "TOOL_CALL_HARD_CAP constant not found in agents.py"
    value = int(m.group(1))
    assert value == 10, (
        f"TOOL_CALL_HARD_CAP must be 10 (R4), got {value}. The system "
        f"prompt's bounded skill discovery needs headroom above 3, but "
        f"the smart (name+args) detection still blocks exact-repeat loops, "
        f"and it must stay well below MAX_TOOL_ITERATIONS (40)."
    )


def test_loop_guard_user_facing_text_no_longer_mentions_internal_scaffolding():
    """The user-facing loop-guard text must NOT be the internal
    'I have enough information to proceed' string. After R4 it should
    explicitly say we're building with sensible defaults so the user
    understands the agent is about to commit.

    This protects against accidental reverts of the R4 wording.
    """
    src = _load_source()
    # The old text is forbidden (it made the user feel the agent was
    # just stalling)
    assert "I have enough information to proceed" not in src, (
        "Loop-guard user_facing text still says 'I have enough "
        "information to proceed' — that wording was replaced in R4 with "
        "'I'm going to build the agent with sensible defaults now' "
        "so the user understands the next step is commit, not stall."
    )
    # The new text must be present at all 3 sites
    new_text_count = src.count(
        "I'm going to build the agent with sensible defaults now"
    )
    assert new_text_count == 3, (
        f"Expected the new loop-guard user_facing text at 3 sites "
        f"(v2 main, v2 resume, v3 stream), found {new_text_count}."
    )
