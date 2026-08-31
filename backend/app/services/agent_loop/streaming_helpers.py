"""2026-08-25: live-streaming spec — typing-effect helper for search queries.

Search tools receive a fully-formed query string from the LLM. To create a
live-typing UX without a true streaming source, we chunk the query and emit
~12 search_query_delta SSE events over ~300ms.
"""
import asyncio
import json
import re
from typing import AsyncIterator


async def _stream_typing_effect(query: str, tool_call_id: str) -> AsyncIterator[str]:
    """Yield `search_query_delta` SSE frames that simulate typing the query.

    Each frame's `partial` field contains the query as it would appear after
    N more characters were typed. The last frame contains the full query.
    """
    if not query:
        return
    # ~12 chunks for queries >= 12 chars; 1 chunk per char for very short
    chunk_count = max(1, min(12, len(query)))
    chunk_size = max(1, len(query) // chunk_count)
    accumulated = ""
    for i in range(0, len(query), chunk_size):
        accumulated = query[: i + chunk_size]
        frame = {
            "type": "search_query_delta",
            "tool_call_id": tool_call_id,
            "partial": accumulated,
        }
        yield f"data: {json.dumps(frame)}\n\n"
        await asyncio.sleep(0.025)  # 25ms between chunks → ~300ms total for 12 chunks


# 2026-08-25: heuristic plan-step parser for live-streaming spec.
# Detects "1. ...", "- ...", "* ...", "Step 1: ...", "步骤 1: ..."
_PLAN_STEP_RE = re.compile(
    r'(?m)^[ \t]*(?:'
    r'(?P<num1>\d+)\.[ \t]+(?P<title1>[^\n]+)'           # 1. Title
    r'|[-*][ \t]+(?P<title2>[^\n]+)'                       # - Title or * Title
    r'|(?:Step|步骤)\s*(?P<num2>\d+)\s*[:：][ \t]*(?P<title3>[^\n]+)'  # Step 1: Title
    r')'
)

# Markdown bold/italic wrappers that must NOT leak into plan-step titles.
# The local LLM streams "**Revenue**" / "- **" fragments; a title that is only
# these markers (e.g. "**" from a mid-stream partial line) is junk.
_MD_WRAP_RE = re.compile(r'^\s*\*{1,3}\s*|\s*\*{1,3}\s*$|^\s*_{1,2}\s*|\s*_{1,2}\s*$')
_PURE_MD_ARTIFACT_RE = re.compile(r'[*_\-=\s]+')


def _clean_stream_title(raw: str) -> str:
    """Strip markdown bold/italic wrappers and reject pure-artifact titles."""
    t = _MD_WRAP_RE.sub('', raw or '')
    t = t.strip()
    if not t or _PURE_MD_ARTIFACT_RE.fullmatch(t):
        return ''
    return t


def parse_plan_steps_from_text(text: str) -> list[dict]:
    """Extract plan steps from streaming text. Returns ordered list of {step_index, title}."""
    if not text:
        return []
    steps = []
    for m in _PLAN_STEP_RE.finditer(text):
        title = m.group("title1") or m.group("title2") or m.group("title3")
        num = m.group("num1") or m.group("num2")
        if title:
            title = _clean_stream_title(title)
            if not title:
                continue  # markdown artifact ("**", "---") — not a real step
            if len(title) > 200:  # truncate very long lines
                title = title[:200] + "…"
            steps.append({
                "step_index": int(num) if num else len(steps) + 1,
                "title": title,
            })
    return steps
