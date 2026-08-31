"""Delegation nudge — deterministic fan-out guidance for parallel asks.

Modern agents (Kimi/Claude/Codex) fan out independent subtasks to parallel
sub-agents; weak local models tend to answer "top 5 customers, top 5
products, and top 3 regions" one-by-one in a single linear pass. This
module detects parallelizable work from the USER MESSAGE ALONE (no LLM) and
returns a one-shot system-prompt directive steering the agent to
``delegate_task(tasks=[...])``.

Signals (conservative — false positives are cheap, false negatives are
missed optimizations):
  1. Repeated "top/list N <thing>" clauses (>= 2)
  2. Numbered enumerations — "1) 2) 3)", "① ② ③", "first/second/third" (>= 3)
  3. Chinese "、" enumerations with >= 3 items or explicit "分别"

Only fires for multi-item requests (>= 2 independent asks). Dashboard-shaped
turns are excluded (they have their own build-tool forcing).
"""

from __future__ import annotations

import re

_TOP_N_RE = re.compile(
    r"\b(?:top|list|best|show)\s+\d+\s+[a-z]+\b", re.IGNORECASE
)
_NUMBERED_RE = re.compile(
    r"(?:\b(?:first|second|third|fourth|fifth)\b"
    r"|1\)|2\)|3\)|\(1\)|\(2\)|\(3\)"
    r"|①|②|③|④|⑤"
    r"|(?:\b\d+[.、])(?=\s))",
    re.IGNORECASE,
)
_ZH_ENUMERATION_RE = re.compile(r"[、；;]")
_ZH_EXPLICIT_RE = re.compile(r"分别|以及|各自")

# Dashboard turns get their own forcing — do not cross streams.
_DASHBOARD_WORDS = ("dashboard", "看板", "仪表盘", "数据面板")


def parallelizable_ask_count(user_content: str | None) -> int:
    """Number of independent ask clauses detected (0 = not parallelizable)."""
    if not user_content:
        return 0
    top_n = len(set(_TOP_N_RE.findall(user_content)))
    numbered = len(_NUMBERED_RE.findall(user_content))
    zh_items = len(_ZH_ENUMERATION_RE.findall(user_content)) + 1 if _ZH_ENUMERATION_RE.search(user_content) else 0
    zh_explicit = 2 if _ZH_EXPLICIT_RE.search(user_content) else 0

    score = top_n + numbered + max(zh_items, zh_explicit)
    return score


def delegation_nudge_directive(user_content: str | None) -> str | None:
    """Return a system-prompt directive when the ask is parallelizable.

    Returns ``None`` when the request is single-item, dashboard-shaped, or
    empty. The directive is appended to the system prompt ONCE before the
    tool loop so it cannot interfere with mid-loop control flow.
    """
    if not user_content:
        return None
    text = user_content.lower()
    if any(word in text for word in _DASHBOARD_WORDS):
        return None
    count = parallelizable_ask_count(user_content)
    if count < 2:
        return None
    return (
        "\n\nPARALLEL WORK DETECTED: the user asked for multiple independent "
        f"items ({count} clauses detected) — top-N lists, comparisons, or "
        "separate analyses with NO data dependency between them. Call "
        "`delegate_task(tasks=[...])` with ONE task per item so they run as "
        "parallel sub-agents, then synthesize their results into the final "
        "answer. Do NOT answer them one-by-one in a single linear pass."
    )
