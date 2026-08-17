"""Unit tests for think-tag stripping (Ollama Qwen3 thinking mode).

Covers the stateful ``_ThinkTagDetector`` (streaming, chunk-spanning tags) and
the ``_strip_think_tags`` helper (non-streaming, full-message). These ensure
that ``<think>...</think>`` blocks embedded inside ``delta.content`` are routed
to the reasoning channel instead of leaking into visible content — the root
cause of "empty output" for qwen3.5-27b-tools.
"""

import unittest

from app.routers.agents import (
    _ChatMLTagDetector,
    _ThinkTagDetector,
    _strip_chatml_tags,
    _strip_think_tags,
)


def _collect(feed_chunks):
    """Feed a sequence of raw chunks into a fresh detector and return the
    final routed (content, reasoning) strings."""
    detector = _ThinkTagDetector()
    routed = []
    for chunk in feed_chunks:
        routed.extend(detector.feed(chunk))
    routed.extend(detector.flush())

    content_parts = []
    reasoning_parts = []
    for channel, piece in routed:
        if channel == "reasoning":
            reasoning_parts.append(piece)
        else:
            content_parts.append(piece)
    return "".join(content_parts), "".join(reasoning_parts)


class TestStripThinkTags(unittest.TestCase):
    def test_empty_content(self):
        self.assertEqual(_strip_think_tags(""), ("", ""))

    def test_no_think_tags_passthrough(self):
        content, reasoning = _strip_think_tags("Hello, world!")
        self.assertEqual(content, "Hello, world!")
        self.assertEqual(reasoning, "")

    def test_single_think_block(self):
        content, reasoning = _strip_think_tags(
            "<think>I should check the schema first</think>Here is the answer"
        )
        self.assertEqual(content, "Here is the answer")
        self.assertEqual(reasoning, "I should check the schema first")

    def test_think_block_only(self):
        content, reasoning = _strip_think_tags("<think>internal chain of thought</think>")
        self.assertEqual(content, "")
        self.assertEqual(reasoning, "internal chain of thought")

    def test_multiple_think_blocks(self):
        text = (
            "Intro<think>first thought</think>Middle<think>second thought</think>Outro"
        )
        content, reasoning = _strip_think_tags(text)
        self.assertEqual(content, "IntroMiddleOutro")
        self.assertEqual(reasoning, "first thoughtsecond thought")

    def test_think_block_at_start_and_end(self):
        content, reasoning = _strip_think_tags(
            "<think>a</think>visible<think>b</think>"
        )
        self.assertEqual(content, "visible")
        self.assertEqual(reasoning, "ab")

    def test_unclosed_think_tag_left_as_is(self):
        # Degrade gracefully: an unclosed open tag stays as visible text.
        content, reasoning = _strip_think_tags("some <think>unclosed text")
        self.assertEqual(content, "some <think>unclosed text")
        self.assertEqual(reasoning, "")


class TestThinkTagDetector(unittest.TestCase):
    def test_empty_chunk(self):
        self.assertEqual(_collect([""]), ("", ""))

    def test_single_chunk_with_think_block(self):
        content, reasoning = _collect(
            ["<think>thinking here</think>visible answer"]
        )
        self.assertEqual(content, "visible answer")
        self.assertEqual(reasoning, "thinking here")

    def test_tag_split_across_chunks_open(self):
        # "<thi" + "nk>reasoning</think>answer"
        content, reasoning = _collect(
            ["hello <thi", "nk>reasoning</think>answer"]
        )
        self.assertEqual(content, "hello answer")
        self.assertEqual(reasoning, "reasoning")

    def test_tag_split_across_chunks_close(self):
        content, reasoning = _collect(
            ["<think>reasoning</thi", "nk>answer"]
        )
        self.assertEqual(content, "answer")
        self.assertEqual(reasoning, "reasoning")

    def test_tag_split_char_by_char(self):
        # Stress: feed one character at a time through both tags.
        raw = "<think>step by step</think>done"
        content, reasoning = _collect(list(raw))
        self.assertEqual(content, "done")
        self.assertEqual(reasoning, "step by step")

    def test_whitespace_preserved_around_tags(self):
        content, reasoning = _collect(["a <think>x</think> b"])
        self.assertEqual(content, "a  b")
        self.assertEqual(reasoning, "x")

    def test_partial_open_tag_at_end_flushed_as_content(self):
        # Dangling "<thi" with no completion → ordinary visible text.
        content, reasoning = _collect(["answer <thi"])
        self.assertEqual(content, "answer <thi")
        self.assertEqual(reasoning, "")

    def test_partial_close_tag_at_end_flushed_as_reasoning(self):
        # Dangling "</thi" while inside → reasoning text.
        content, reasoning = _collect(["<think>why </thi"])
        self.assertEqual(content, "")
        self.assertEqual(reasoning, "why </thi")


def _collect_chatml(feed_chunks):
    """Feed raw chunks into a fresh ChatML detector and return the joined
    visible content (ChatML tokens silently dropped)."""
    detector = _ChatMLTagDetector()
    routed = []
    for chunk in feed_chunks:
        routed.extend(detector.feed(chunk))
    routed.extend(detector.flush())
    return "".join(routed)


class TestStripChatMLTags(unittest.TestCase):
    def test_empty_content(self):
        self.assertEqual(_strip_chatml_tags(""), "")

    def test_no_chatml_tags_passthrough(self):
        self.assertEqual(_strip_chatml_tags("Hello, world!"), "Hello, world!")

    def test_im_start_assistant_stripped(self):
        self.assertEqual(
            _strip_chatml_tags("<|im_start|>assistant\nHello"), "Hello"
        )

    def test_im_end_stripped(self):
        self.assertEqual(
            _strip_chatml_tags("Hello<|im_end|>"), "Hello"
        )

    def test_full_chatml_frame_stripped(self):
        raw = (
            "<|im_start|>system\nYou are helpful<|im_end|>"
            "<|im_start|>user\nHi<|im_end|>"
            "<|im_start|>assistant\nHello there<|im_end|>"
        )
        self.assertEqual(
            _strip_chatml_tags(raw),
            "You are helpfulHiHello there",
        )

    def test_im_start_without_role(self):
        self.assertEqual(
            _strip_chatml_tags("<|im_start|>Hello"), "Hello"
        )

    def test_no_newline_after_role(self):
        self.assertEqual(
            _strip_chatml_tags("<|im_start|>assistantHello"), "Hello"
        )


class TestChatMLTagDetector(unittest.TestCase):
    def test_empty_chunk(self):
        self.assertEqual(_collect_chatml([""]), "")

    def test_no_chatml_passthrough(self):
        self.assertEqual(_collect_chatml(["Hello, world!"]), "Hello, world!")

    def test_im_start_assistant_stripped(self):
        self.assertEqual(
            _collect_chatml(["<|im_start|>assistant\nHello"]), "Hello"
        )

    def test_im_end_stripped(self):
        self.assertEqual(_collect_chatml(["Hello<|im_end|>"]), "Hello")

    def test_im_start_split_across_chunks(self):
        # "<|im_sta" + "rt|>assistant\nHello"
        self.assertEqual(
            _collect_chatml(["<|im_sta", "rt|>assistant\nHello"]), "Hello"
        )

    def test_im_end_split_across_chunks(self):
        # "Hello<|im_en" + "d|>world"
        self.assertEqual(
            _collect_chatml(["Hello<|im_en", "d|>world"]), "Helloworld"
        )

    def test_role_split_across_chunks(self):
        # "<|im_start|>assista" + "nt\nHello"
        self.assertEqual(
            _collect_chatml(["<|im_start|>assista", "nt\nHello"]), "Hello"
        )

    def test_char_by_char(self):
        raw = "<|im_start|>assistant\nvisible<|im_end|>"
        self.assertEqual(_collect_chatml(list(raw)), "visible")

    def test_user_role_with_content_after(self):
        self.assertEqual(
            _collect_chatml(["<|im_start|>user\nWhat is 2+2?"]), "What is 2+2?"
        )

    def test_dangling_partial_flushed_as_content(self):
        # Dangling "<|im_st" that never completes → ordinary visible text.
        self.assertEqual(_collect_chatml(["answer <|im_st"]), "answer <|im_st")

    def test_interleaved_with_think_passthrough(self):
        # ChatML detector must not disturb think tags (handled downstream).
        raw = "<|im_start|>assistant\n<think>reasoning</think>answer"
        self.assertEqual(
            _collect_chatml([raw]), "<think>reasoning</think>answer"
        )


if __name__ == "__main__":
    unittest.main()
