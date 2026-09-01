"""_summarize_preview: first-N-sentences, never mid-sentence truncation."""
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services import automation_executor as ax


def test_short_text_returned_unchanged():
    assert ax._summarize_preview("All good.") == "All good."


def test_keeps_whole_sentences_up_to_cap():
    text = "Revenue grew 12% this week. " + ("Filler sentence here. " * 40)
    out = ax._summarize_preview(text, cap=120)
    assert out.startswith("Revenue grew 12% this week.")
    assert out.endswith("…")
    assert len(out) <= 130
    # never cut mid-word/mid-sentence
    assert out.rstrip(" …").endswith(".")


def test_single_long_sentence_falls_back_to_word_boundary():
    text = "word " * 300
    out = ax._summarize_preview(text, cap=100)
    assert len(out) <= 102
    assert out.endswith("…")
    assert not out[:-1].endswith("wor")


def test_cjk_sentence_endings():
    text = "连接正常。" + ("这是填充内容。" * 100)
    out = ax._summarize_preview(text, cap=30)
    assert out.startswith("连接正常。")


def test_empty_input():
    assert ax._summarize_preview("") == ""
    assert ax._summarize_preview(None) == ""
