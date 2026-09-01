"""Recursive text chunker — splits on paragraph / sentence boundaries.

Token budget is approximated with a word-count heuristic (1 token ≈ 0.75
words for English). Avoids pulling in a tokenizer dependency at chunk
time; the embedder does its own tokenisation.
"""

from __future__ import annotations

import re

_WORDS_PER_TOKEN = 0.75

# Chinese tokenization heuristic: each CJK char counts as ~1.5 tokens when
# counted in the BPE-style budget used by bge-m3 / multilingual models.
CHARS_PER_CN_TOKEN = 1.5

# Sentence-ending punctuation for Chinese (full-width) plus ASCII fallbacks
# and Chinese semicolon — used to break long paragraphs into chunkable units.
_CN_SENT_RE = re.compile(r"(?<=[。！？；\n!?;])\s*")


def _approx_tokens(text: str) -> int:
    return max(1, int(len(text.split()) / _WORDS_PER_TOKEN))


def _approx_tokens_chinese(text: str) -> int:
    """Approximate token count for Chinese-heavy text (1.5 chars/token).

    Uses the raw char count (no whitespace stripping) since CJK has no
    word boundaries. Returns at least 1 for non-empty text.
    """
    if not text:
        return 1
    return max(1, int(len(text) / CHARS_PER_CN_TOKEN))


def _split_chinese_sentences(text: str) -> list[str]:
    """Split Chinese text on sentence boundaries (。！？；\\n and ASCII fallbacks).

    Returns a list of trimmed, non-empty sentence strings. Empty / whitespace-only
    inputs return ``[]``. Falls back to a single-element list when no boundary
    punctuation is found (so callers always get a list).
    """
    if not text or not text.strip():
        return []
    parts = _CN_SENT_RE.split(text)
    out: list[str] = []
    buf = ""
    for p in parts:
        buf += p
        # flush when the trailing char is a known sentence terminator
        if buf and buf[-1] in "。！？；\n":
            stripped = buf.strip()
            if stripped:
                out.append(stripped)
            buf = ""
    tail = buf.strip()
    if tail:
        out.append(tail)
    return out or ([text.strip()] if text.strip() else [])


def chunk_text(
    text: str,
    max_tokens: int = 800,
    overlap: int = 100,
    mode: str = "english",
) -> list[dict]:
    """Split ``text`` into chunks of <= max_tokens (approx), with overlap.

    ``mode`` selects the token estimator and sentence splitter:
        - ``"english"`` (default): word-based heuristic, ASCII sentence split.
        - ``"chinese"``: 1.5 chars/token, splits on CJK punctuation.

    Splits first on double-newlines (paragraphs), then accumulates into a
    buffer until the budget is hit. Single paragraphs bigger than the
    budget are sub-split on sentence boundaries, then words. Returns a
    list of ``{"text": str, "index": int, "token_count": int}``.
    """
    text = (text or "").strip()
    if not text:
        return []

    # Resolve mode (unknown values fall back to english)
    if mode not in ("english", "chinese"):
        mode = "english"

    # Pick estimator
    if mode == "chinese":
        approx = _approx_tokens_chinese
    else:
        approx = _approx_tokens

    if approx(text) <= max_tokens:
        return [{"text": text, "index": 0, "token_count": approx(text)}]

    # Pick paragraph splitter
    if mode == "chinese":
        paragraphs = re.split(r"(\n{2,})", text)
    else:
        paragraphs = re.split(r"(\n{2,})", text)
    buf: list[str] = []
    buf_tokens = 0

    def flush():
        nonlocal buf, buf_tokens
        if buf:
            chunks.append("".join(buf).strip())
            # keep overlap: carry the last `overlap` tokens worth of text
            tail = "".join(buf)
            if mode == "chinese":
                keep = max(1, int(overlap * CHARS_PER_CN_TOKEN))
                buf = [tail[-keep:]]
            else:
                tail_words = tail.split()
                keep = max(1, int(overlap / _WORDS_PER_TOKEN))
                buf = [" ".join(tail_words[-keep:]) + "\n\n"]
            buf_tokens = approx(buf[0])
        else:
            buf = []
            buf_tokens = 0

    chunks: list[str] = []
    for para in paragraphs:
        para_tokens = approx(para)
        if buf_tokens + para_tokens > max_tokens and buf:
            flush()
        # if a single paragraph is bigger than the budget, sub-split it
        if para_tokens > max_tokens:
            sub = _sub_split(para, max_tokens, overlap, mode, approx)
            for s in sub:
                if buf_tokens + approx(s) > max_tokens and buf:
                    flush()
                buf.append(s)
                buf_tokens += approx(s)
        else:
            buf.append(para)
            buf_tokens += para_tokens

    flush()
    return [
        {"text": c, "index": i, "token_count": approx(c)}
        for i, c in enumerate(chunks)
        if c
    ]


def chunk_text_chinese(
    text: str,
    max_tokens: int = 800,
    overlap: int = 100,
) -> list[dict]:
    """Convenience wrapper: ``chunk_text(..., mode="chinese")``."""
    return chunk_text(text, max_tokens=max_tokens, overlap=overlap, mode="chinese")


def _sub_split(
    text: str,
    max_tokens: int,
    overlap: int,
    mode: str = "english",
    approx=None,
) -> list[str]:
    """Split a too-large block on sentence boundaries, then words/char-windows."""
    if approx is None:
        approx = _approx_tokens_chinese if mode == "chinese" else _approx_tokens

    # Pick sentence splitter
    if mode == "chinese":
        sentences = _split_chinese_sentences(text)
        if not sentences:
            sentences = [text]
    else:
        sentences = re.split(r"(?<=[.!?])\s+", text)

    out: list[str] = []
    buf: list[str] = []
    buf_tokens = 0
    for s in sentences:
        st = approx(s)
        if buf_tokens + st > max_tokens and buf:
            out.append(" ".join(buf) if mode != "chinese" else "".join(buf))
            if mode == "chinese":
                keep = max(1, int(overlap * CHARS_PER_CN_TOKEN))
                joined = "".join(buf)
                buf = [joined[-keep:]]
                buf_tokens = approx(buf[0])
            else:
                # Overlap is expressed in TOKENS; convert to words and keep
                # that many trailing WORDS (not sentences — keeping whole
                # sentences here explodes chunk count ~100x for long
                # single-paragraph documents: each flush re-emits the whole
                # sentence tail, so every char is duplicated dozens of
                # times and retrieval index sizes balloon).
                keep = max(1, int(overlap / _WORDS_PER_TOKEN))
                tail_words = " ".join(buf).split()
                buf = [" ".join(tail_words[-keep:])] if tail_words else []
                buf_tokens = approx(buf[0]) if buf else 0
        if st > max_tokens:
            # single sentence still too big — hard char/word-split window
            if mode == "chinese":
                step = max(1, int(max_tokens * CHARS_PER_CN_TOKEN))
                for i in range(0, len(s), step):
                    out.append(s[i : i + step])
            else:
                words = s.split()
                step = max(1, int(max_tokens * _WORDS_PER_TOKEN))
                for i in range(0, len(words), step):
                    out.append(" ".join(words[i : i + step]))
        else:
            buf.append(s)
            buf_tokens += st
    if buf:
        out.append(" ".join(buf) if mode != "chinese" else "".join(buf))
    return out
