"""E2E: re-export uses the cached execution — the data tool is never re-run.

Drives the REAL backend over HTTP + REAL LLM + ERP tools, asserting the
architectural invariant of the session-cached re-export pipeline:

    Turn 1  "Contract Performance for last month"
        -> calls >= 1 data tool  (result cached into DataExecution)
    Turn 2  "give me in docx" (same data, new format)
        -> calls 0 data tools   <-- THE INVARIANT
        -> calls create_artifact
        -> produces a non-empty DOCX artifact

Skipped unless ``E2E_RUN=1`` (same convention as
``test_ecisco_bi_report_e2e.py``), so the regular regression suite never
touches the live server / model.

Typical run:
    cd backend && E2E_RUN=1 venv/bin/python -m pytest tests/e2e/test_re_export_no_rerun.py -s -q

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
import time

import httpx
import pytest

E2E_RUN = os.environ.get("E2E_RUN") == "1"
BASE_URL = os.environ.get("E2E_BASE_URL", "http://localhost:5002").rstrip("/")
ADMIN_EMAIL = os.environ.get("E2E_ADMIN_EMAIL", "admin@zhanlu.dev")
ADMIN_PASSWORD=os.env...RD", "admin123")
APP_ID = os.environ.get("E2E_APP_ID", "default-app")

AGENT_NAME = "general_assistant"
PROJECT_ID = "e5ac337b-469a-480d-822b-f6a3155e652c"  # Ecisco BI

# Tool names that read data from the warehouse. Turn 2 must NOT call any.
DATA_TOOL_NAMES = frozenset({
    "ask_data_agent", "ask_erp_kpi", "ask_perception", "ask_forecast",
    "ask_pricing", "ask_decision", "query_composer", "fetch_data_batch",
    "execute_query", "ask_intelligence", "ask_rag_research",
})

# Turn 1 query (must exercise >= 1 data tool).
TURN1_QUERY = "Give me Contract Performance for last month"

# Turn 2 query (same data, new format -> must NOT re-run data tools).
TURN2_QUERY = "Now give me the same contract performance data as a docx"

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
        self.client = httpx.Client(
            base_url=BASE_URL, timeout=httpx.Timeout(900.0, connect=15.0)
        )

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
                    "name": "e2e-re-export-no-rerun",
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
            "artifact_urls": [],
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
            json={"content": content, "role": "user", "project_id": PROJECT_ID},
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
        elif etype in ("tool_result", "artifact", "artifact_created"):
            # Collect artifact file URLs wherever the backend emits them.
            for key in ("file_url", "url", "artifact_url"):
                value = event.get(key)
                if isinstance(value, str) and value:
                    transcript["artifact_urls"].append(value)
                    print(f"[event] {etype}: artifact {key}={value}")
        elif etype == "delta":
            pass
        elif etype == "done":
            content = event.get("content", "")
            transcript["done_content"] = content
            transcript["done_meta"] = event
            print(f"[event] done: content len={len(content or '')}")
        elif etype in ("heartbeat", "ping"):
            pass
        else:
            print(f"[event] {etype}: {str(event)[:160]}")

    def fetch_file(self, file_url: str) -> bytes:
        """Download an artifact file (handles relative /api/... paths)."""
        url = file_url if file_url.startswith("http") else (
            f"{BASE_URL}{file_url}" if file_url.startswith("/") else file_url
        )
        resp = self.client.get(url, headers=self._headers())
        assert resp.status_code == 200, (
            f"artifact download failed: HTTP {resp.status_code} {resp.text[:300]}"
        )
        return resp.content

    def close(self) -> None:
        self.client.close()


def _data_tool_names(transcript: dict) -> list[str]:
    return [
        name for name, _ in transcript.get("tool_calls", [])
        if name in DATA_TOOL_NAMES
    ]


@pytest.fixture(scope="module")
def session() -> E2EState:
    s = E2EState()
    try:
        s.login()
        s.create_conversation()
        yield s
    finally:
        s.close()


def test_re_export_does_not_rerun_data_tool(session: E2EState) -> None:
    # ---- Turn 1: data analysis (caches the execution) ---------------------
    t1 = session.stream_message(TURN1_QUERY)
    assert not t1["error_events"], (
        f"turn 1 hit {len(t1['error_events'])} error event(s): "
        f"{t1['error_events'][0]}"
    )
    t1_data = _data_tool_names(t1)
    assert t1_data, "Turn 1 must call at least one data tool"
    print(f"[T1] data tools: {', '.join(t1_data)}")
    assert (t1.get("done_content") or "").strip(), (
        "Turn 1 ended without assistant content"
    )

    # ---- Turn 2: re-export in a new format --------------------------------
    t2 = session.stream_message(TURN2_QUERY)
    assert not t2["error_events"], (
        f"turn 2 hit {len(t2['error_events'])} error event(s): "
        f"{t2['error_events'][0]}"
    )

    # THE INVARIANT: zero data tools on the export turn.
    t2_data = _data_tool_names(t2)
    assert not t2_data, (
        "Turn 2 re-ran a data tool (cache not honored): "
        f"{', '.join(t2_data)}"
    )

    # Turn 2 must produce the artifact instead.
    t2_names = [name for name, _ in t2["tool_calls"]]
    assert "create_artifact" in t2_names, (
        "Turn 2 must call create_artifact. tools: "
        f"{', '.join(t2_names) or '(none)'}"
    )
    print(f"[T2] tools: {', '.join(t2_names)}")

    # Turn 2 must produce a non-empty DOCX artifact.
    assert t2["artifact_urls"], (
        "no artifact file_url surfaced in turn 2 events "
        "(create_artifact ran but result not emitted?)"
    )
    for url in t2["artifact_urls"]:
        content = session.fetch_file(url)
        assert content, f"artifact {url} is empty"
        print(f"[T2] artifact OK: {url} ({len(content)} bytes)")

    content = (t2.get("done_content") or "").strip()
    assert content, "Turn 2 ended without final assistant content"
    for marker in FAILURE_MARKERS:
        assert marker.lower() not in content.lower(), (
            f"final answer contains failure marker: {marker!r}\n"
            f"---- content ----\n{content[:800]}"
        )
    assert not content.startswith(FALLBACK_PREFIXES), (
        f"final answer is a data-fallback placeholder:\n{content[:400]}"
    )

    print(f"\n[E2E-PASS] Turn1 data tools={len(t1_data)}, "
          f"Turn2 data tools=0 (invariant holds), "
          f"create_artifact=1, docx non-empty, "
          f"total {t1['elapsed_s'] + t2['elapsed_s']}s")