"""Recursive text chunker — splits on paragraph / sentence boundaries.

Token budget is approximated with a word-count heuristic (1 token ≈ 0.75
words for English). Avoids pulling in a tokenizer dependency at chunk
time; the embedder does its own tokenisation.
"""

from __future__ import annotations

import re

_WORDS_PER_TOKEN = 0.75


def _approx_tokens(text: str) -> int:
    return max(1, int(len(text.split()) / _WORDS_PER_TOKEN))


def chunk_text(
    text: str,
    max_tokens: int = 800,
    overlap: int = 100,
) -> list[dict]:
    """Split ``text`` into chunks of <= max_tokens (approx), with overlap.

    Splits first on double-newlines (paragraphs), then accumulates into a
    buffer until the budget is hit. Single paragraphs bigger than the
    budget are sub-split on sentence boundaries, then words. Returns a
    list of ``{"text": str, "index": int, "token_count": int}``.
    """
    text = (text or "").strip()
    if not text:
        return []

    if _approx_tokens(text) <= max_tokens:
        return [{"text": text, "index": 0, "token_count": _approx_tokens(text)}]

    chunks: list[str] = []
    # Split into paragraphs, keeping the separators so structure is preserved.
    paragraphs = re.split(r"(\n{2,})", text)
    buf: list[str] = []
    buf_tokens = 0

    def flush():
        nonlocal buf, buf_tokens
        if buf:
            chunks.append("".join(buf).strip())
            # keep overlap: carry the last `overlap` tokens worth of words
            tail = " ".join(buf)
            tail_words = tail.split()
            keep = max(1, int(overlap / _WORDS_PER_TOKEN))
            buf = [" ".join(tail_words[-keep:]) + "\n\n"]
            buf_tokens = _approx_tokens(buf[0])
        else:
            buf = []
            buf_tokens = 0

    for para in paragraphs:
        para_tokens = _approx_tokens(para)
        if buf_tokens + para_tokens > max_tokens and buf:
            flush()
        # if a single paragraph is bigger than the budget, sub-split it
        if para_tokens > max_tokens:
            sub = _sub_split(para, max_tokens, overlap)
            for s in sub:
                if buf_tokens + _approx_tokens(s) > max_tokens and buf:
                    flush()
                buf.append(s)
                buf_tokens += _approx_tokens(s)
        else:
            buf.append(para)
            buf_tokens += para_tokens

    flush()
    return [
        {"text": c, "index": i, "token_count": _approx_tokens(c)}
        for i, c in enumerate(chunks)
        if c
    ]


def _sub_split(text: str, max_tokens: int, overlap: int) -> list[str]:
    """Split a too-large block on sentence boundaries, then words."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out: list[str] = []
    buf: list[str] = []
    buf_tokens = 0
    for s in sentences:
        st = _approx_tokens(s)
        if buf_tokens + st > max_tokens and buf:
            out.append(" ".join(buf))
            keep = max(1, int(overlap / _WORDS_PER_TOKEN))
            buf = buf[-keep:]
            buf_tokens = sum(_approx_tokens(x) for x in buf)
        if st > max_tokens:
            # single sentence still too big — hard word-split
            words = s.split()
            step = max(1, int(max_tokens * _WORDS_PER_TOKEN))
            for i in range(0, len(words), step):
                out.append(" ".join(words[i:i + step]))
        else:
            buf.append(s)
            buf_tokens += st
    if buf:
        out.append(" ".join(buf))
    return out
