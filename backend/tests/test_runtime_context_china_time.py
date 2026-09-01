"""Regression (2026-08-06): the user is in China and asked the agent
"what's time now" — the agent replied "10:31 UTC" instead of the
local time. The root cause is that ``_runtime_context_block()`` used
``datetime.now().astimezone()`` which is tied to the *server* timezone.
The backend container runs in UTC, so the system prompt only ever
showed UTC to the LLM, and the LLM echoed UTC back to the user.

The deployment is China-based — every Chinese user expects the agent
to default to China Standard Time (UTC+8, Asia/Shanghai) unless the
user has explicitly set a different timezone. This test pins the
default-timezone invariant so a future refactor doesn't silently
revert to UTC (e.g. by reading ``TZ`` from the env, by reading
``datetime.now().astimezone()`` again, or by deleting the
``ZoneInfo(...)`` call in favor of something simpler).
"""

import re

from app.services.agent_prompts import _runtime_context_block


def _extract_clock(line_pattern: str, block: str) -> str:
    """Pull the ``HH:MM:SS`` clock from a line matching the pattern."""
    m = re.search(line_pattern + r"[^\d]*(\d{2}:\d{2}:\d{2})", block)
    assert m, (
        f"runtime context block is missing a '{line_pattern} HH:MM:SS' line; "
        f"block tail:\n{block[-400:]}"
    )
    return m.group(1)


def test_runtime_context_block_anchors_local_time_to_asia_shanghai():
    """The 'Local time' clock MUST be in Asia/Shanghai (UTC+8) by default.

    The deployment is in China; users expect the agent to answer
    "what's the time?" with the China local time, not UTC. The block
    names the timezone explicitly so a future reader (or the LLM
    itself) cannot mis-attribute the clock.
    """
    block = _runtime_context_block()
    assert "Asia/Shanghai" in block or "China Standard Time" in block, (
        "Local time must be anchored to Asia/Shanghai (UTC+8). The block "
        "should mention 'Asia/Shanghai' or 'China Standard Time' so the "
        f"LLM cannot mis-attribute the clock.\n--- block tail ---\n{block[-400:]}"
    )


def test_runtime_context_block_local_clock_is_utc_plus_eight():
    """The Local time clock must be exactly UTC+8 (28800 seconds) ahead
    of the UTC time clock. This is the durable invariant: even if the
    server timezone or the wall clock changes, the local clock in the
    prompt must always represent China time relative to UTC.
    """
    block = _runtime_context_block()
    local_hms = _extract_clock(r"Local time", block)
    utc_hms = _extract_clock(r"UTC time", block)

    def to_seconds(hms: str) -> int:
        h, m, s = (int(x) for x in hms.split(":"))
        return h * 3600 + m * 60 + s

    local_secs = to_seconds(local_hms)
    utc_secs = to_seconds(utc_hms)
    # Local should be ahead of UTC by exactly 8 hours (28800 s), unless
    # the day rolled over (then we adjust by ±86400).
    diff = (local_secs - utc_secs) % 86400
    # diff is in [0, 86400). The expected offset is 28800 (8 hours).
    assert diff == 28800, (
        f"Local time ({local_hms}) must be exactly 8 hours ahead of UTC "
        f"time ({utc_hms}). Computed diff (mod 24h): {diff} seconds; "
        f"expected 28800.\n--- block ---\n{block}"
    )


def test_runtime_context_block_keeps_utc_line_for_cross_team_reference():
    """A UTC line must still appear so cross-team messages in non-CN
    timezones can be coordinated without losing the UTC reference.

    The fix is NOT to remove the UTC line — it is to add an explicit
    China-local line as the *primary* clock so the LLM stops defaulting
    to UTC. The UTC line stays for sanity.
    """
    block = _runtime_context_block()
    assert "UTC" in block, (
        "UTC time line should still appear for cross-team reference; "
        f"got block tail:\n{block[-400:]}"
    )


def test_runtime_context_block_local_line_appears_before_utc():
    """Order the Local (China) line first so the LLM is more likely to
    echo it back. Putting UTC first biased the model toward reporting
    UTC (which is what the user observed in the bug screenshot).
    """
    block = _runtime_context_block()
    local_idx = block.find("Local time")
    utc_idx = block.find("UTC time")
    assert local_idx > 0 and utc_idx > 0, (
        f"Both 'Local time' and 'UTC time' lines must be present; "
        f"local_idx={local_idx}, utc_idx={utc_idx}\nblock tail:\n{block[-400:]}"
    )
    assert local_idx < utc_idx, (
        "Local (China) time should appear BEFORE the UTC time line in "
        "the block so the LLM is biased toward echoing the local clock "
        f"back to the user. Found local_idx={local_idx}, utc_idx={utc_idx}."
    )
