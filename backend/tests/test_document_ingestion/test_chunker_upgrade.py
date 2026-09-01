"""Tests for the upgraded document_ingestion.chunker module.

The chunker upgrade adds:
- ``mode="chinese"`` parameter for Chinese-optimized token estimation (1.5 chars/token)
- ``chunk_text_chinese()`` convenience wrapper
- CJK sentence splitting (。！？；\n)
- Backward compatibility with existing ``chunk_text()`` API
"""
from __future__ import annotations

import pytest

from app.services.document_ingestion.chunker import (
    chunk_text,
    chunk_text_chinese,
    _approx_tokens,
    _approx_tokens_chinese,
    _split_chinese_sentences,
    CHARS_PER_CN_TOKEN,
)


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


class TestBackwardCompatAPI:
    def test_chunk_text_empty_returns_empty_list(self):
        assert chunk_text("") == []
        assert chunk_text("   ") == []

    def test_chunk_text_short_returns_single_chunk(self):
        out = chunk_text("Hello world")
        assert len(out) == 1
        assert out[0]["text"] == "Hello world"
        assert out[0]["index"] == 0

    def test_chunk_text_result_has_required_keys(self):
        out = chunk_text("Some text here")
        assert len(out) == 1
        chunk = out[0]
        assert "text" in chunk
        assert "index" in chunk
        assert "token_count" in chunk

    def test_chunk_text_long_splits_into_multiple(self):
        long_text = ("This is a paragraph. " * 100).strip()
        out = chunk_text(long_text, max_tokens=50, overlap=10)
        assert len(out) > 1
        # Indices are sequential
        assert [c["index"] for c in out] == list(range(len(out)))


# ---------------------------------------------------------------------------
# Chinese token estimation
# ---------------------------------------------------------------------------


class TestChineseTokenEstimation:
    def test_chinese_chars_per_token_constant(self):
        assert CHARS_PER_CN_TOKEN == 1.5

    def test_approx_tokens_chinese_basic(self):
        # 6 Chinese chars → 6 / 1.5 = 4 tokens
        assert _approx_tokens_chinese("乙烯价格走势") == 4

    def test_approx_tokens_chinese_pure_short(self):
        # 4 Chinese chars → 4 / 1.5 = 2.67 → int = 2 tokens
        assert _approx_tokens_chinese("乙烯价格") == 2

    def test_approx_tokens_chinese_mixed(self):
        # Mixed text → use char count / 1.5
        text = "乙烯 DCPD 价格"
        # len(text) = 10 (including spaces) → 10 / 1.5 = 6.67 → int = 6 tokens
        assert _approx_tokens_chinese(text) == 6

    def test_approx_tokens_chinese_empty(self):
        assert _approx_tokens_chinese("") >= 1

    def test_mode_chinese_chunks_pure_chinese(self):
        text = "乙烯价格持续上涨。" * 50  # ~600 chars
        out = chunk_text(text, max_tokens=20, overlap=5, mode="chinese")
        assert len(out) > 1

    def test_mode_chinese_chunks_at_cjk_sentence_boundaries(self):
        text = "第一句。 第二句！ 第三句？ 第四句； 第五句。 " * 30
        out = chunk_text(text, max_tokens=15, overlap=3, mode="chinese")
        assert len(out) > 1
        # Each chunk should ideally end at a sentence boundary if possible
        for chunk in out:
            assert chunk["text"].strip()

    def test_chunk_text_chinese_helper(self):
        text = "乙烯" * 200  # 400 chars → ~267 tokens
        out = chunk_text_chinese(text, max_tokens=50, overlap=10)
        assert len(out) > 1
        for chunk in out:
            assert "text" in chunk
            assert chunk["token_count"] > 0


# ---------------------------------------------------------------------------
# CJK sentence splitting
# ---------------------------------------------------------------------------


class TestSplitChineseSentences:
    def test_basic_split_on_period(self):
        sentences = _split_chinese_sentences("第一句。第二句。第三句。")
        assert len(sentences) == 3

    def test_split_on_mixed_punctuation(self):
        sentences = _split_chinese_sentences(
            "价格上涨！市场波动？数据更新；趋势明显。"
        )
        assert len(sentences) == 4

    def test_split_on_newline(self):
        sentences = _split_chinese_sentences("第一句\n第二句\n第三句")
        assert len(sentences) == 3

    def test_empty_text_returns_empty(self):
        assert _split_chinese_sentences("") == []

    def test_whitespace_only_returns_empty(self):
        assert _split_chinese_sentences("   \n\t  ") == []

    def test_single_sentence_returns_single(self):
        sentences = _split_chinese_sentences("只有一个句子")
        assert len(sentences) == 1

    def test_no_punctuation_returns_single(self):
        sentences = _split_chinese_sentences("连续的文字没有标点")
        assert len(sentences) == 1

    def test_mixed_cjk_and_ascii(self):
        sentences = _split_chinese_sentences(
            "DCPD price increased. 市场波动明显。 Price dropped."
        )
        # Should split at both Chinese and English sentence boundaries
        assert len(sentences) >= 2


# ---------------------------------------------------------------------------
# Mode dispatch in chunk_text
# ---------------------------------------------------------------------------


class TestChunkTextMode:
    def test_default_mode_is_english(self):
        # English heuristic should be used
        text = "word " * 100
        out_en = chunk_text(text, max_tokens=20, overlap=5, mode="english")
        out_default = chunk_text(text, max_tokens=20, overlap=5)
        # Should produce same output
        assert len(out_en) == len(out_default)

    def test_invalid_mode_falls_back_to_english(self):
        text = "Some English text here"
        # Should not raise
        out = chunk_text(text, max_tokens=50, mode="invalid_mode")
        assert len(out) >= 1

    def test_chinese_mode_handles_english_text(self):
        # Even with Chinese mode, English text should still chunk reasonably
        text = "Hello world. " * 50
        out = chunk_text(text, max_tokens=30, overlap=5, mode="chinese")
        assert len(out) >= 1


class TestSingleParagraphOverlapRegression:
    """Regression: overlap was applied as a SENTENCE count in _sub_split,
    which exploded chunk output ~100x for long single-paragraph docs (each
    flush re-emitted the whole sentence tail). Overlap must be word-based."""

    def test_long_single_paragraph_does_not_explode(self):
        filler = (
            "This is routine logistics documentation for warehouse "
            "operations. Nothing unusual here, just inventory movements "
            "and standard shipping procedures. "
        )
        text = (filler * 900) + "\n\nCONFIDENTIAL ANNEX: The ZEPHYR-9 code is 7734.\n"
        chunks = chunk_text(text, max_tokens=800, overlap=100)
        assert 0 < len(chunks) < 100  # was ~1700 before the fix
        joined = "".join(c["text"] for c in chunks)
        # Overlap redundancy must stay small, not ~140x.
        assert len(joined) < len(text) * 3
        # The tail (facts at the end) must survive chunking.
        assert any("ZEPHYR-9" in c["text"] for c in chunks)
        # Sequential indices.
        assert [c["index"] for c in chunks] == list(range(len(chunks)))


class TestSingleParagraphOverlapRegression:
    """Regression: overlap was applied as a SENTENCE count in _sub_split,
    which exploded chunk output ~100x for long single-paragraph docs (each
    flush re-emitted the whole sentence tail). Overlap must be word-based."""

    def test_long_single_paragraph_does_not_explode(self):
        filler = (
            "This is routine logistics documentation for warehouse "
            "operations. Nothing unusual here, just inventory movements "
            "and standard shipping procedures. "
        )
        text = (filler * 900) + "\n\nCONFIDENTIAL ANNEX: The ZEPHYR-9 code is 7734.\n"
        chunks = chunk_text(text, max_tokens=800, overlap=100)
        assert 0 < len(chunks) < 100  # was ~1700 before the fix
        joined = "".join(c["text"] for c in chunks)
        # Overlap redundancy must stay small, not ~140x.
        assert len(joined) < len(text) * 3
        # The tail (facts at the end) must survive chunking.
        assert any("ZEPHYR-9" in c["text"] for c in chunks)
        # Sequential indices.
        assert [c["index"] for c in chunks] == list(range(len(chunks)))
