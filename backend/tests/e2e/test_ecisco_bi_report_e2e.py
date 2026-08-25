"""End-to-end test: does the Ecisco BI agent actually answer a report query?

This drives the REAL backend over HTTP and triggers the REAL LLM + ERP tools:

    1. login as admin
    2. create a fresh conversation for ``ecisco_bi_assistant``
    3. stream a user message through the v3 SSE endpoint
    4. assert the turn completes with a substantive answer and NO error bubble

The test is skipped unless ``E2E_RUN=1`` is set, so the regular regression
suite never touches the live server / model.

Typical run:
    cd backend && E2E_RUN=1 venv/bin/python -m pytest tests/e2e/test_ecisco_bi_report_e2e.py -s -q

Env vars:
    E2E_RUN           (required) set to "1" to actually run
    E2E_BASE_URL      default http://localhost:5002
    E2E_ADMIN_EMAIL   default admin@zhanlu.dev
    E2E_ADMIN_PASSWORD default admin123
    E2E_APP_ID        default default-app
"""
from __future__ import annotations

import json
import os
import sys
import time

import httpx
import pytest

E2E_RUN = os.environ.get("E2E_RUN") == "1"
BASE_URL = os.environ.get("E2E_BASE_URL", "http://localhost:5002").rstrip("/")
ADMIN_EMAIL = os.environ.get("E2E_ADMIN_EMAIL", "admin@zhanlu.dev")
ADMIN_PASSWORD = os.environ.get("E2E_ADMIN_PASSWORD", "admin123")
APP_ID = os.environ.get("E2E_APP_ID", "default-app")

AGENT_NAME = "ecisco_bi_assistant"
PROJECT_ID = "e5ac337b-469a-480d-822b-f6a3155e652c"  # Ecisco BI

REPORT_QUERY = (
    "Make a report about Sales & ERP performance — revenue, margin, and "
    "inventory — for July 2026."
)

# Known failure markers — if the final answer is any of these, the turn failed.
FAILURE_MARKERS = [
    "Sorry, I hit an error while responding",
    "Sorry, the AI service is temporarily unavailable",
    "I gathered some information but had trouble putting it all together",
    "ran out of steps",
]
FALLBACK_PREFIXES = (
    "Data retrieved (",
    "(The requested",
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not E2E_RUN,
        reason="set E2E_RUN=1 to hit the live backend + model",
    ),
]


class E2EState:
    def __init__(self) -> None:
        self.token: str | None = None
        self.conversation_id: str | None = None
        self.client = httpx.Client(base_url=BASE_URL, timeout=httpx.Timeout(900.0, connect=15.0))

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def login(self) -> None:
        resp = self.client.post(
            f"/api/apps/{APP_ID}/auth/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        )
        assert resp.status_code == 200, (
            f"login failed: HTTP {resp.status_code} {resp.text[:300]}"
        )
        data = resp.json()
        self.token = data.get("access_token") or data.get("token")
        assert self.token, f"no access_token in login response: {list(data)}"
        print(f"\n[login] OK as {ADMIN_EMAIL}")

    def create_conversation(self) -> None:
        resp = self.client.post(
            f"/api/apps/{APP_ID}/agents/conversations",
            headers=self._headers(),
            json={
                "agent_name": AGENT_NAME,
                "metadata": {
                    "name": "e2e-model-check",
                    "project_id": PROJECT_ID,
                },
            },
        )
        assert resp.status_code in (200, 201), (
            f"create conversation failed: HTTP {resp.status_code} {resp.text[:300]}"
        )
        self.conversation_id = resp.json().get("id")
        assert self.conversation_id, f"no conversation id: {resp.text[:300]}"
        print(f"[conversation] created {self.conversation_id}")

    def stream_message(self, content: str) -> dict:
        """POST a message to the v3 SSE endpoint; return parsed transcript."""
        assert self.conversation_id, "create_conversation() first"
        transcript: dict = {
            "events": [],
            "tool_calls": [],
            "deltas": [],
            "error_events": [],
            "done_content": "",
            "done_meta": None,
        }
        url = (
            f"/api/apps/{APP_ID}/agents/conversations/"
            f"v3/{self.conversation_id}/messages/stream"
        )
        started = time.time()
        print(f"\n[stream] sending query ({len(content)} chars)...")
        with self.client.stream(
            "POST",
            url,
            headers=self._headers(),
            json={
                "content": content,
                "role": "user",
                "project_id": PROJECT_ID,
            },
        ) as resp:
            print(f"[stream] HTTP {resp.status_code}")
            assert resp.status_code == 200, resp.text[:300]
            buf = ""
            for chunk in resp.iter_text():
                buf += chunk
                while "\n\n" in buf:
                    block, buf = buf.split("\n\n", 1)
                    for line in block.splitlines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if not payload:
                            continue
                        try:
                            event = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        transcript["events"].append(event)
                        self._ingest_event(transcript, event)
        elapsed = time.time() - started
        transcript["elapsed_s"] = round(elapsed, 1)
        print(f"[stream] done in {transcript['elapsed_s']}s; "
              f"{len(transcript['events'])} events")
        return transcript

    @staticmethod
    def _ingest_event(transcript: dict, event: dict) -> None:
        etype = event.get("type", "?")
        if etype == "error":
            transcript["error_events"].append(event)
            print(f"[event] ERROR: {event.get('message', event)[:300]}")
        elif etype == "tool_progress":
            for tc in event.get("tool_calls", []) or []:
                if isinstance(tc, dict) and tc.get("name"):
                    transcript["tool_calls"].append(
                        (tc.get("name"), tc.get("status", ""))
                    )
                    print(f"[event] tool_progress: {tc.get('name')} "
                          f"{tc.get('status', '')}")
        elif etype == "delta":
            transcript["deltas"].append(event.get("content", ""))
        elif etype == "done":
            content = event.get("content", "")
            transcript["done_content"] = content
            transcript["done_meta"] = event
            print(f"[event] done: content len={len(content or '')}")
        elif etype in ("heartbeat", "ping"):
            pass
        else:
            print(f"[event] {etype}: {str(event)[:160]}")

    def extract_approval_id(self, transcript: dict) -> str | None:
        """Find the approval_id from the paused event's conversation messages."""
        for event in transcript["events"]:
            if event.get("type") != "paused":
                continue
            conv = event.get("conversation") or {}
            for msg in conv.get("messages") or []:
                if not isinstance(msg, dict):
                    continue
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("approval_id"):
                        return tc["approval_id"]
        return None

    def approve(self, approval_id: str) -> None:
        resp = self.client.post(
            f"/api/governance/approvals/{approval_id}/approve",
            headers=self._headers(),
            json={},
        )
        assert resp.status_code == 200, (
            f"approve failed: HTTP {resp.status_code} {resp.text[:300]}"
        )
        print(f"[approve] {approval_id} approved")

    def resume(self) -> dict:
        """Resume a paused conversation; returns the final conversation dict."""
        resp = self.client.post(
            f"/api/apps/{APP_ID}/agents/conversations/{self.conversation_id}/resume",
            headers=self._headers(),
        )
        assert resp.status_code == 200, (
            f"resume failed: HTTP {resp.status_code} {resp.text[:300]}"
        )
        conv = resp.json()
        print(f"[resume] status={conv.get('status')}")
        return conv

    @staticmethod
    def last_assistant_content(conv: dict) -> str:
        """Extract the most recent non-empty assistant content."""
        last = ""
        for msg in conv.get("messages") or []:
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                last = content
        return last

    def close(self) -> None:
        self.client.close()


def _assert_not_failed(state: E2EState, transcript: dict) -> None:
    """Shared assertions: no error bubble, no fallback placeholder."""
    assert not transcript["error_events"], (
        f"turn hit {len(transcript['error_events'])} error event(s): "
        f"{transcript['error_events'][0]}"
    )
    content = (transcript.get("done_content") or "").strip()
    assert content, "done event carried empty content"
    for marker in FAILURE_MARKERS:
        assert marker.lower() not in content.lower(), (
            f"final answer contains failure marker: {marker!r}\n"
            f"---- content ----\n{content[:800]}"
        )
    assert not content.startswith(FALLBACK_PREFIXES), (
        f"final answer is a data-fallback placeholder:\n{content[:400]}"
    )


@pytest.fixture(scope="module")
def state() -> E2EState:
    s = E2EState()
    try:
        s.login()
        s.create_conversation()
        yield s
    finally:
        s.close()


def test_model_responds_to_basic_prompt(state: E2EState) -> None:
    """Warm-up: the model must answer a trivial question through the real loop."""
    transcript = state.stream_message(
        "Reply with just the single word: OK"
    )
    _assert_not_failed(state, transcript)
    content = transcript["done_content"].strip()
    assert "OK" in content.upper(), (
        f"model did not answer the warm-up query. content={content[:300]!r}"
    )
    print(f"[WARMUP-PASS] model answered in {transcript['elapsed_s']}s: "
          f"{content[:120]!r}")


def test_ecisco_bi_report_full_turn(state: E2EState) -> None:
    """The original failing query must complete with a real report.

    The agent may pause for human approval (e.g. before ``execute_code``);
    the test auto-approves and resumes so the full report is produced.
    """
    transcript = state.stream_message(REPORT_QUERY)

    # 1) No error bubble / fallback placeholder at any point.
    assert not transcript["error_events"], (
        f"turn hit {len(transcript['error_events'])} error event(s): "
        f"{transcript['error_events'][0]}"
    )

    # 2) The model must have used data tools (the whole point of this chain).
    tool_names = [name for name, _ in transcript["tool_calls"]]
    assert tool_names, "no tool_progress events — model didn't call any tool"
    print(f"[tools] executed: {', '.join(tool_names)}")

    # 3) Complete any paused-for-approval steps, then read the final answer.
    content = transcript["done_content"].strip()
    for _ in range(5):
        approval_id = state.extract_approval_id(transcript)
        if approval_id:
            state.approve(approval_id)
            conv = state.resume()
            if conv.get("status") == "awaiting_approval":
                # resume paused again — re-drive from the fresh transcript.
                transcript["events"] = []
                transcript["tool_calls"] = []
                transcript["done_content"] = ""
                transcript["events"] = [{"type": "paused", "conversation": conv}]
                continue
            content = state.last_assistant_content(conv)
            break
        content = transcript["done_content"].strip()
        break

    # 4) Final answer must be substantive prose, not a stub / fallback.
    content = (content or "").strip()
    assert content, "turn ended without any final assistant content"
    for marker in FAILURE_MARKERS:
        assert marker.lower() not in content.lower(), (
            f"final answer contains failure marker: {marker!r}\n"
            f"---- content ----\n{content[:800]}"
        )
    assert not content.startswith(FALLBACK_PREFIXES), (
        f"final answer is a data-fallback placeholder:\n{content[:400]}"
    )
    assert len(content) >= 40, f"final answer too short: {content!r}"

    print("\n===== FINAL ANSWER (first 1500 chars) =====")
    print(content[:1500])
    print("============================================\n")

    print(f"\n[E2E-PASS] full report turn OK in {transcript['elapsed_s']}s; "
          f"{len(transcript['tool_calls'])} tool call(s), "
          f"answer={len(content)} chars")
