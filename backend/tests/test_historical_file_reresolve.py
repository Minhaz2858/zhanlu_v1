"""2026-08-31: historical file re-read for follow-up turns (Kimi/GPT parity).

Regression for the "i need more details about this file" failure: the
frontend only sends ``file_urls`` for the CURRENT turn, so a follow-up
request lost access to files uploaded earlier — the agent would claim it
"can't re-read" the upload (observed live: agent invented file paths and
failed). Fix: ``collect_historical_file_urls`` re-scans ``conv.messages``
and ``assemble_context`` / the legacy stream loop re-inject the extracted
text so uploads stay readable for the whole conversation.

Covers:
1. collect_historical_file_urls — dedupe, prefix filter, exclude, bounds
2. assemble_context — re-injects historical file text into the manifest
3. assemble_context — does NOT duplicate the current turn's attachments
4. agents.py legacy loop — source-level wiring check for the merge
"""

from __future__ import annotations

import ast
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

from app.services.synexia.context_assembler import (  # noqa: E402
    collect_historical_file_urls,
)


class _FakeQuery:
    """Minimal query-chain stub: filter/order_by/limit are no-ops."""

    def __init__(self, first_result=None, all_result=None):
        self._first = first_result
        self._all = all_result

    def filter(self, *a, **kw):
        return self

    def order_by(self, *a, **kw):
        return self

    def limit(self, *a, **kw):
        return self

    def first(self):
        return self._first

    def all(self):
        return self._all or []


class _FakeConv:
    def __init__(self, messages):
        self.messages = messages


class _FakeDB:
    """Routes .query(model) to the right fake result by model __name__."""

    def __init__(self, conv=None):
        self._conv = conv

    def query(self, model):
        name = getattr(model, "__name__", "")
        if name == "AgentConversation":
            return _FakeQuery(first_result=self._conv)
        return _FakeQuery()


# ---------------------------------------------------------------------------
# 1. collect_historical_file_urls
# ---------------------------------------------------------------------------


class CollectHistoricalFileUrlsTest(unittest.TestCase):
    def _msg(self, role, content, file_urls=None):
        m = {"id": "x", "role": role, "content": content}
        if file_urls is not None:
            m["file_urls"] = file_urls
        return m

    def test_collects_from_prior_user_messages_newest_first(self):
        conv = [
            self._msg("user", "first", ["/api/uploads/aaa.pdf"]),
            self._msg("assistant", "summarised"),
            self._msg("user", "follow-up"),  # current turn, no urls
        ]
        urls = collect_historical_file_urls(conv, exclude=[])
        self.assertEqual(urls, ["/api/uploads/aaa.pdf"])

    def test_skips_current_turn_urls_via_exclude(self):
        conv = [
            self._msg("user", "first", ["/api/uploads/aaa.pdf"]),
            self._msg("assistant", "summarised"),
            self._msg("user", "follow-up", ["/api/uploads/bbb.pdf"]),
        ]
        # Current turn already carries bbb — exclude must drop it.
        urls = collect_historical_file_urls(conv, exclude=["/api/uploads/bbb.pdf"])
        self.assertEqual(urls, ["/api/uploads/aaa.pdf"])

    def test_dedupes_across_messages(self):
        conv = [
            self._msg("user", "first", ["/api/uploads/aaa.pdf"]),
            self._msg("user", "re-upload", ["/api/uploads/aaa.pdf", "/api/uploads/ccc.txt"]),
        ]
        # Newest-first: the re-upload message is scanned first, so its
        # ordering wins; the duplicate aaa from the older message is dropped.
        urls = collect_historical_file_urls(conv, exclude=[])
        self.assertEqual(urls, ["/api/uploads/aaa.pdf", "/api/uploads/ccc.txt"])

    def test_rejects_non_upload_urls(self):
        # Path-traversal attempt must never be re-injected.
        conv = [
            self._msg("user", "x", ["/api/uploads/ok.pdf", "/etc/passwd", "C:\\evil.txt"]),
        ]
        urls = collect_historical_file_urls(conv, exclude=[])
        self.assertEqual(urls, ["/api/uploads/ok.pdf"])

    def test_ignores_assistant_messages_and_missing_urls(self):
        conv = [
            self._msg("assistant", "no urls here"),
            self._msg("user", "plain message"),
        ]
        self.assertEqual(collect_historical_file_urls(conv, exclude=[]), [])

    def test_tolerates_single_string_file_urls(self):
        conv = [
            {"role": "user", "content": "x", "file_urls": "/api/uploads/single.pdf"},
        ]
        urls = collect_historical_file_urls(conv, exclude=[])
        self.assertEqual(urls, ["/api/uploads/single.pdf"])

    def test_returns_empty_for_empty_or_none(self):
        self.assertEqual(collect_historical_file_urls(None, exclude=[]), [])
        self.assertEqual(collect_historical_file_urls([], exclude=[]), [])

    def test_bounded_scan(self):
        # 20 user messages each with a unique url — only the newest
        # _HISTORICAL_MAX_USER_MESSAGES (8) are scanned.
        conv = [
            self._msg("user", f"m{i}", [f"/api/uploads/u{i:02d}.txt"])
            for i in range(20)
        ]
        urls = collect_historical_file_urls(conv, exclude=[])
        self.assertLessEqual(len(urls), 8)
        # Newest first: the last message in the list is scanned first.
        self.assertEqual(urls[0], "/api/uploads/u19.txt")


# ---------------------------------------------------------------------------
# 2. assemble_context — re-injects historical file text
# ---------------------------------------------------------------------------


class AssembleContextHistoricalTest(unittest.TestCase):
    def _run_assemble(self, conv_messages, current_attachments=None):
        conv = _FakeConv(conv_messages)
        db = _FakeDB(conv=conv)
        with patch(
            "app.services.document_ingestion.service.prepare_for_context",
            side_effect=self._fake_prepare,
        ):
            from app.services.synexia.context_assembler import assemble_context
            ctx = assemble_context(
                db=db,
                conversation_id="conv-1",
                agent_name="general_assistant",
                user_message="tell me more about that file",
                task_spec={},
                attachments=current_attachments or [],
            )
        return ctx

    @staticmethod
    def _fake_prepare(file_url, **kwargs):
        name = file_url.rsplit("/", 1)[-1]
        return {
            "file_url": file_url,
            "file_name": name,
            "file_type": "pdf",
            "text": f"CONTENT-OF-{name}",
            "is_image": False,
            "local_path": None,
            "truncated": False,
            "error": None,
        }

    def test_reinjects_historical_file_text(self):
        conv = [
            {"id": "u1", "role": "user", "content": "what is in this file",
             "file_urls": ["/api/uploads/6952f469dba7445f9e7637b011eb01a6.pdf"]},
            {"id": "a1", "role": "assistant", "content": "It is a CV..."},
            {"id": "u2", "role": "user", "content": "i need more details"},
        ]
        ctx = self._run_assemble(conv)
        items = ctx["items"]
        attach_items = [i for i in items if i["type"] == "user_attachments"]
        self.assertTrue(attach_items, "expected a user_attachments item")
        block = attach_items[-1]["content"]
        self.assertIn("uploaded earlier", block)
        self.assertIn("6952f469dba7445f9e7637b011eb01a6.pdf", block)
        self.assertIn("CONTENT-OF-6952f469dba7445f9e7637b011eb01a6.pdf", block)

    def test_does_not_duplicate_current_turn_attachments(self):
        conv = [
            {"id": "u1", "role": "user", "content": "what is in this file",
             "file_urls": ["/api/uploads/aaa.pdf"]},
            {"id": "a1", "role": "assistant", "content": "..."},
            {"id": "u2", "role": "user", "content": "more please"},
        ]
        ctx = self._run_assemble(conv, current_attachments=["/api/uploads/aaa.pdf"])
        attach_items = [i for i in ctx["items"] if i["type"] == "user_attachments"]
        # The current-turn block (1b) carries the file; the historical
        # block (1c) must NOT re-add the same url.
        blocks = "".join(i["content"] for i in attach_items)
        self.assertEqual(blocks.count("CONTENT-OF-aaa.pdf"), 1)

    def test_no_historical_item_when_nothing_uploaded(self):
        conv = [
            {"id": "u1", "role": "user", "content": "hello"},
            {"id": "a1", "role": "assistant", "content": "hi"},
        ]
        ctx = self._run_assemble(conv)
        attach_items = [i for i in ctx["items"] if i["type"] == "user_attachments"]
        self.assertEqual(attach_items, [])


# ---------------------------------------------------------------------------
# 3. agents.py legacy loop — source-level wiring check
# ---------------------------------------------------------------------------


class LegacyLoopWiringTest(unittest.TestCase):
    """Pin the legacy stream loop's historical-file merge via source AST.

    Same rationale as the frontend source-text contract tests: the merge
    is inline in a 17k-line router, so a refactor that quietly drops it
    must fail loudly.
    """

    def test_legacy_loop_merges_historical_file_urls(self):
        path = os.path.join(_BACKEND_ROOT, "app", "routers", "agents.py")
        src = open(path, "r", encoding="utf-8").read()
        self.assertIn("collect_historical_file_urls", src)
        self.assertIn("_all_attachment_urls", src)
        self.assertIn("uploaded earlier", src)
        # The merge must exclude current-turn urls (dedupe).
        self.assertIn("exclude=file_urls", src)


if __name__ == "__main__":
    unittest.main()
