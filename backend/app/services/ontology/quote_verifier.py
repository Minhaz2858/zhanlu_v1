"""P4 T5 — deterministic quote verification (NO LLM).

The LLM proposes candidate quotations; this module is the deterministic
gate that decides whether a quotation actually exists in the referenced
source text. Matching is whitespace-normalized containment; the returned
span is the source's ORIGINAL characters (normalization is for matching
only, never stored). Untraceable quotes never become evidence.
"""
from __future__ import annotations

import re

_WS = re.compile(r"\s+")


def normalize_ws(text: str) -> str:
    """Collapse all whitespace runs to single spaces (matching only)."""
    return _WS.sub(" ", (text or "")).strip()


def _norm_with_index_map(src: str) -> tuple[str, list[int]]:
    """Collapse whitespace runs to single spaces AND record, for each
    normalized character, its ORIGINAL index in ``src``. Leading/trailing
    whitespace is stripped (mirrors normalize_ws().strip())."""
    stripped = src.strip()
    offset = len(src) - len(src.lstrip())  # where stripped content starts
    norm_chars: list[str] = []
    orig_idx: list[int] = []
    i = 0
    n = len(stripped)
    while i < n:
        if stripped[i].isspace():
            norm_chars.append(" ")
            orig_idx.append(offset + i)
            while i < n and stripped[i].isspace():
                i += 1
        else:
            norm_chars.append(stripped[i])
            orig_idx.append(offset + i)
            i += 1
    return "".join(norm_chars), orig_idx


def verify_quote(source_text: str, quote: str) -> tuple[bool, str | None]:
    """Check the quote against the source.

    Returns ``(True, original_span)`` when the whitespace-normalized quote
    occurs in the whitespace-normalized source (the span is the ORIGINAL
    source characters, extracted via offset mapping); ``(False, None)``
    otherwise. Deterministic — no model calls, no fuzzy thresholds.
    """
    src = (source_text or "")
    q = normalize_ws(quote)
    if not q or not src.strip():
        return False, None

    norm_src, orig_idx = _norm_with_index_map(src)
    if len(q) > len(norm_src):
        return False, None

    idx = norm_src.find(q)
    if idx < 0:
        return False, None

    start_orig = orig_idx[idx]
    end_orig = orig_idx[idx + len(q) - 1] + 1
    return True, src[start_orig:end_orig]
