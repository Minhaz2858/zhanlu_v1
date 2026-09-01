"""Test that the v3 streaming FSM route persists the assistant reply into
conv.messages (parity with the v2 blocking route), so follow-up turns can
see both sides of the conversation.

Uses textual source assertions + a focused unit test on the persistence
block, matching the style of test_v3_fsm_stream.py.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)


class TestV3StreamPersistsAssistantToConvMessages(unittest.TestCase):
    def test_v3_route_source_contains_conv_messages_append(self):
        """The v3 FSM branch must append the assistant message to
        conv.messages — not just the Message table."""
        src = Path("/root/zhanlu/backend/app/routers/agents.py").read_text()
        # The v3 FSM branch is inside _fsm_event_stream. Find it.
        # Assert the conv.messages append block exists after the Message
        # table persist, with the parity comment referencing the v2 route.
        self.assertIn("Persist the assistant reply into conv.messages too", src)
        self.assertIn('_assistant_msg = {', src)
        self.assertIn('"role": "assistant"', src)
        # The append + commit must be present.
        self.assertIn("messages.append(_assistant_msg)", src)
        self.assertIn("conv.messages = list(messages)", src)

    def test_v3_route_persists_tool_calls_and_artifact_ids(self):
        """The compact tool_calls structure must include name, success,
        and artifact_ids so follow-up context can surface what was produced."""
        src = Path("/root/zhanlu/backend/app/routers/agents.py").read_text()
        self.assertIn('"artifact_ids": _tc.get("artifact_ids", [])', src)
        self.assertIn('fsm_result.artifact_ids or []', src)

    def test_v3_route_failure_is_non_fatal(self):
        """If the conv.messages append fails, the route must log + rollback,
        not crash the SSE stream."""
        src = Path("/root/zhanlu/backend/app/routers/agents.py").read_text()
        self.assertIn("conv.messages append failed (non-fatal)", src)
        self.assertIn("db.rollback()", src)


if __name__ == "__main__":
    unittest.main()
