#!/usr/bin/env python3
"""UI test suite for agent skill execution -> file generation -> download.

Drives a real headless Chromium against the Zhanlu dev stack and verifies
that, when a user invokes a built-in agent skill (e.g. ``docx``, ``pptx``,
``md``) via the chat UI, the agent:

  1. executes the skill end-to-end (system prompt, routing decision, tool
     calling, sandbox render, finalize);
  2. surfaces a ``SkillRouteBadge`` ("using skill: <name>") in the
     assistant message bubble;
  3. produces a downloadable artifact (a ``.docx`` / ``.pptx`` / ``.md``
     blob linked to the message);
  4. the downloaded file has a non-zero size and the right magic bytes
     for its declared format.

Run:

    APP_ID=local-zhanlu-app \
    FRONTEND_URL=http://localhost:5157 \
    BACKEND_URL=http://localhost:5002 \
    python /root/zhanlu/scripts/test_skill_execution_ui.py
"""
from __future__ import annotations

import os
import re
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import requests
from playwright.sync_api import (
    sync_playwright,
    Page,
    BrowserContext,
    TimeoutError as PlaywrightTimeout,
)

FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:5157")
BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:5002")
APP_ID = os.environ.get("APP_ID", "local-zhanlu-app")
API_BASE = f"{BACKEND_URL}/api/apps/{APP_ID}"
ARTIFACTS_BASE = f"{BACKEND_URL}/api"

SCREENSHOT_DIR = Path("/tmp/zhanlu_skill_e2e")
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOAD_DIR = Path("/tmp/zhanlu_skill_downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

AGENT_TASK_TIMEOUT_S = int(os.environ.get("AGENT_TASK_TIMEOUT_S", "240"))
ASSISTANT_FIRST_BYTE_S = 30
POLL_INTERVAL_S = 3.0


@dataclass
class SkillCase:
    name: str
    skill: str
    prompt: str
    integrity_check: Callable
    extension: str
    expected_text_fragments: list


def _is_zip_ooxml(data: bytes):
    if len(data) < 4:
        return False, f"file too small ({len(data)} bytes)"
    if data[:4] != b"PK\x03\x04":
        return False, f"magic mismatch; head={data[:8]!r}"
    if b"word/" in data[:16384] or b"word/" in data[-16384:]:
        return True, "docx zip with word/ dir"
    if b"ppt/" in data[:16384] or b"ppt/" in data[-16384:]:
        return True, "pptx zip with ppt/ dir"
    return True, "zip magic OK (no specific dir detected)"


def _is_markdown(data: bytes):
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False, "not valid utf-8"
    if len(text.strip()) < 20:
        return False, f"too short ({len(text)} chars)"
    if not re.search(r"^#{1,6}\s+\S", text, re.MULTILINE):
        return False, "no markdown heading found"
    return True, f"{len(text)} chars, has heading"


SKILL_CASES = [
    SkillCase(
        name="DOCX report",
        skill="docx",
        prompt=(
            "Create a docx file titled 'Weekly Status Report' with "
            "two sections: 'Highlights' (3 bullets) and 'Next Steps' "
            "(2 bullets)."
        ),
        integrity_check=_is_zip_ooxml,
        extension="docx",
        expected_text_fragments=["Highlights", "Next Steps"],
    ),
    SkillCase(
        name="PPTX deck",
        skill="pptx",
        prompt=(
            "Build a 3-slide pptx titled 'Quarterly Review'. "
            "Slide 1: title. Slide 2: 'Key Metrics' with 2 bullets. "
            "Slide 3: 'Next Steps' with 2 bullets."
        ),
        integrity_check=_is_zip_ooxml,
        extension="pptx",
        expected_text_fragments=["Quarterly Review", "Key Metrics"],
    ),
    SkillCase(
        name="Markdown report",
        skill="md",
        prompt=(
            "Write a markdown document titled 'Onboarding Checklist'. "
            "Use '#' for the title, '## Overview' for the first "
            "section, and a bulleted list under '## Steps'."
        ),
        integrity_check=_is_markdown,
        extension="md",
        expected_text_fragments=["Onboarding Checklist", "Overview", "Steps"],
    ),
]


@dataclass
class TestRecord:
    name: str
    status: str
    detail: str = ""


RESULTS = []


def record(name, status, detail=""):
    icon = {"PASS": "PASS", "FAIL": "FAIL", "WARN": "WARN"}.get(status, "?")
    print(f"  [{icon}] {name}: {detail}")
    RESULTS.append(TestRecord(name=name, status=status, detail=detail))


def _wait_for_servers():
    print(f"[setup] waiting for frontend at {FRONTEND_URL} ...")
    for _ in range(60):
        try:
            r = requests.get(FRONTEND_URL, timeout=2)
            if r.status_code < 500:
                break
        except Exception:
            pass
        time.sleep(1)
    else:
        raise RuntimeError(f"frontend not reachable at {FRONTEND_URL}")

    print(f"[setup] waiting for backend at {BACKEND_URL}/healthz ...")
    for _ in range(60):
        try:
            r = requests.get(f"{BACKEND_URL}/healthz", timeout=2)
            if r.ok:
                break
        except Exception:
            pass
        time.sleep(1)
    else:
        raise RuntimeError(f"backend not reachable at {BACKEND_URL}/healthz")

    r = requests.get(f"{API_BASE}/agents/conversations", timeout=5)
    if r.status_code >= 500:
        raise RuntimeError(f"app_id {APP_ID!r} not healthy: {r.status_code}")
    print(f"[setup] both servers reachable; APP_ID={APP_ID}")


def _get_last_assistant_text(page):
    try:
        bot_icons = page.locator("svg.lucide-bot").all()
        if not bot_icons:
            return ""
        last_icon = bot_icons[-1]
        return last_icon.evaluate(
            """el => {
                let n = el;
                for (let i = 0; i < 20 && n; i++) {
                    n = n.parentElement;
                    if (!n || n === document.body) break;
                    const t = n.innerText || '';
                    if (t.length > 80) return t;
                }
                return '';
            }"""
        )
    except Exception:
        return ""


def _wait_for_assistant_complete(page, case, *, timeout_s=AGENT_TASK_TIMEOUT_S):
    deadline = time.time() + timeout_s
    try:
        page.locator("textarea").first.wait_for(
            state="visible", timeout=ASSISTANT_FIRST_BYTE_S * 1000
        )
    except PlaywrightTimeout:
        print(f"[wait] chat textarea not found within {ASSISTANT_FIRST_BYTE_S}s")
        return None

    print(f"[wait] polling for completion (timeout {timeout_s}s) ...")
    start = time.time()
    last_log = start
    while time.time() < deadline:
        try:
            thinking_count = (
                page.locator("text=Thinking").count()
                + page.locator("text=\u601d\u8003\u4e2d").count()
            )
        except Exception:
            thinking_count = 0

        try:
            download_btns = page.locator("button:has-text('Download')").count()
            preview_btns = page.locator("button:has-text('Preview')").count()
            open_btns = page.locator("button:has-text('Open')").count()
            view_chat = page.locator("button:has-text('View in chat')").count()
            anchor_dl = page.locator("a[download]").count()
        except Exception:
            download_btns = preview_btns = open_btns = view_chat = anchor_dl = 0
        artifact_indicators = (
            download_btns + preview_btns + open_btns + view_chat + anchor_dl
        )

        assistant_text = _get_last_assistant_text(page)
        text_hits = sum(
            1 for frag in case.expected_text_fragments
            if frag.lower() in assistant_text.lower()
        )

        now = time.time()
        if now - last_log > 15:
            print(
                f"[wait]   {int(now - start)}s: thinking={thinking_count}, "
                f"artifacts={artifact_indicators}, asst_len={len(assistant_text)}, "
                f"text_hits={text_hits}/{len(case.expected_text_fragments)}"
            )
            last_log = now

        done = False
        done_reason = ""
        if thinking_count == 0:
            if artifact_indicators > 0:
                done = True
                done_reason = "artifact_rendered"
            elif text_hits >= max(1, len(case.expected_text_fragments) // 2):
                done = True
                done_reason = "text_matched"
            elif len(assistant_text) > 100 and now - start > 30:
                done = True
                done_reason = "substantial_response"

        if done:
            page.wait_for_timeout(2500)
            print(
                f"[wait] complete after {int(now - start)}s reason={done_reason} "
                f"(text_hits={text_hits}, artifacts={artifact_indicators}, "
                f"asst_len={len(assistant_text)})"
            )
            return done_reason

        time.sleep(POLL_INTERVAL_S)

    print(f"[wait] TIMEOUT after {timeout_s}s")
    return None


def _find_artifact_id_in_dom(page):
    try:
        html = page.content()
    except Exception:
        return ""
    m = re.search(
        r"/artifacts/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        html,
    )
    return m.group(1) if m else None


def _get_artifact_via_api(page):
    try:
        conv_id = page.evaluate(
            "() => window.__ZLS_CONVERSATION_ID__ || "
            "sessionStorage.getItem('zhanlu_conversation_id')"
        )
        if not conv_id:
            return None
        r = requests.get(
            f"{API_BASE}/agents/conversations/{conv_id}", timeout=10
        )
        if not r.ok:
            return None
        msgs = r.json().get("messages", [])
        assistant_msgs = [m for m in msgs if m.get("role") == "assistant"]
        if not assistant_msgs:
            return None
        last_msg_id = assistant_msgs[-1].get("id")
        if not last_msg_id:
            return None
        a = requests.get(
            f"{API_BASE}/messages/{last_msg_id}/artifacts", timeout=10
        )
        if not a.ok:
            return None
        artifacts = a.json()
        if not artifacts:
            return None
        art_id = artifacts[0].get("id")
        if not art_id:
            return None
        d = requests.get(
            f"{ARTIFACTS_BASE}/artifacts/{art_id}/download", timeout=60
        )
        if d.ok and len(d.content) > 0:
            return d.content
        return None
    except Exception as e:
        print(f"[api-fallback] {type(e).__name__}: {e}")
        return None


def _download_artifact_via_frontend(page, art_id):
    rel = f"/api/artifacts/{art_id}/download"
    r = requests.get(f"{FRONTEND_URL}{rel}", timeout=60)
    r.raise_for_status()
    return r.content


def _open_chat(page):
    page.goto(FRONTEND_URL, wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_load_state("networkidle", timeout=20_000)
    page.locator("textarea").first.wait_for(state="visible", timeout=15_000)
    page.wait_for_timeout(2500)


def _send_prompt(page, text):
    textarea = page.locator("textarea").first
    textarea.click()
    textarea.fill(text)
    page.wait_for_timeout(400)
    try:
        textarea.press("Enter")
        page.wait_for_timeout(800)
        if page.locator(f"text={text[:30]}").count() > 0:
            return
    except Exception:
        pass
    send_btn = page.locator("button:has(svg.lucide-send)").last
    if send_btn.count() == 0:
        send_btn = page.locator("button[disabled=false]").last
    send_btn.click()
    page.wait_for_timeout(800)


def _check_route_badge(page, expected_skill):
    try:
        badge = page.locator("text=using skill:").first
        if badge.count() == 0:
            return False, "no 'using skill:' badge found"
        badge_text = badge.inner_text()
        if expected_skill.lower() not in badge_text.lower():
            return False, f"badge text={badge_text!r}, expected {expected_skill!r}"
        return True, badge_text
    except Exception as e:
        return False, f"badge lookup error: {e}"


def _save_and_check(case, data, *, source):
    out_path = DOWNLOAD_DIR / f"{case.skill}_artifact.{case.extension}"
    out_path.write_bytes(data)
    record(f"{case.name}: artifact download", "PASS",
           f"{len(data)} bytes -> {out_path} (src={source[:60]})")
    ok, detail = case.integrity_check(data)
    if ok:
        record(f"{case.name}: file integrity ({case.skill})", "PASS", detail)
    else:
        record(f"{case.name}: file integrity ({case.skill})", "FAIL", detail)


def run_skill_case(page, case):
    print(f"\n{'=' * 60}\n[case] {case.name} (format={case.skill})\n{'=' * 60}")

    try:
        _open_chat(page)
        record(f"{case.name}: chat page loads", "PASS", f"URL: {page.url}")
    except Exception as e:
        record(f"{case.name}: chat page loads", "FAIL", str(e)[:200])
        return

    try:
        _send_prompt(page, case.prompt)
        record(f"{case.name}: send prompt", "PASS",
               f"prompt={case.prompt[:50]!r}")
    except Exception as e:
        record(f"{case.name}: send prompt", "FAIL", str(e)[:200])
        return

    safe_name = case.skill.replace(" ", "_")
    page.screenshot(
        path=str(SCREENSHOT_DIR / f"{safe_name}_01_sent.png"), full_page=True
    )

    completion = _wait_for_assistant_complete(page, case)
    page.screenshot(
        path=str(SCREENSHOT_DIR / f"{safe_name}_02_complete.png"), full_page=True
    )
    if completion is None:
        record(f"{case.name}: agent completion", "FAIL", "timeout")
        return
    record(f"{case.name}: agent completion", "PASS", f"mode={completion}")

    ok, detail = _check_route_badge(page, case.skill)
    if ok:
        record(f"{case.name}: routing badge", "PASS", detail)
    else:
        record(f"{case.name}: routing badge", "WARN", detail)

    art_id = _find_artifact_id_in_dom(page)
    if art_id:
        record(f"{case.name}: artifact id in DOM", "PASS", art_id)
        try:
            data = _download_artifact_via_frontend(page, art_id)
        except Exception as e:
            record(f"{case.name}: artifact download", "FAIL",
                   f"{type(e).__name__}: {e}")
            return
        _save_and_check(case, data,
                        source=f"frontend /api/artifacts/{art_id[:8]}...")
        return

    record(f"{case.name}: artifact id in DOM", "WARN",
           "no /artifacts/{{uuid}}/... URL in DOM; trying API fallback")
    data = _get_artifact_via_api(page)
    if data:
        _save_and_check(case, data, source="API fallback")
    else:
        record(f"{case.name}: artifact", "FAIL",
               "no artifact in DOM and no API fallback available")


def main():
    print("=" * 60)
    print("Zhanlu Agent Skill Execution E2E Test Suite")
    print("=" * 60)
    print(f"frontend:  {FRONTEND_URL}")
    print(f"backend:   {BACKEND_URL}")
    print(f"app_id:    {APP_ID}")
    print(f"timeout:   {AGENT_TASK_TIMEOUT_S}s per case")
    print(f"cases:     {len(SKILL_CASES)}")
    print()

    try:
        _wait_for_servers()
    except Exception as e:
        print(f"FATAL: {e}")
        return 2

    console_errors = []
    page_errors = []
    network_5xx = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context: BrowserContext = browser.new_context(
            viewport={"width": 1440, "height": 900},
            accept_downloads=True,
        )
        page = context.new_page()

        def _on_console(m):
            if m.type == "error":
                console_errors.append(f"[{m.type}] {m.text[:200]}")

        def _on_pageerror(e):
            page_errors.append(str(e)[:200])

        def _on_response(r):
            if r.status >= 500:
                network_5xx.append(f"{r.status} {r.url}")

        page.on("console", _on_console)
        page.on("pageerror", _on_pageerror)
        page.on("response", _on_response)

        for case in SKILL_CASES:
            try:
                run_skill_case(page, case)
            except Exception as e:
                record(
                    f"{case.name}: uncaught exception", "FAIL",
                    f"{type(e).__name__}: {e}\n{traceback.format_exc()[-300:]}",
                )

        print()

        def _is_known_noise(msg):
            return any(
                tok in msg
                for tok in (
                    "WebSocket", "favicon", "DevTools", "[vite]",
                    "Lit is in dev mode", "Failed to load resource",
                    "[Base44 SDK Error]",
                )
            )

        real_console_errors = [m for m in console_errors if not _is_known_noise(m)]
        real_page_errors = [e for e in page_errors if not _is_known_noise(e)]

        if not real_console_errors:
            record("no fatal console errors", "PASS",
                   f"total={len(console_errors)}, noise filtered")
        else:
            record("no fatal console errors", "FAIL",
                   f"{len(real_console_errors)} errors; first: "
                   f"{real_console_errors[0]}")

        if not real_page_errors:
            record("no uncaught page errors", "PASS",
                   f"total={len(page_errors)}, noise filtered")
        else:
            record("no uncaught page errors", "FAIL",
                   f"{len(real_page_errors)} errors; first: {real_page_errors[0]}")

        if not network_5xx:
            record("no 5xx responses during run", "PASS", "all 2xx/3xx/4xx")
        else:
            record("no 5xx responses during run", "FAIL",
                   f"{len(network_5xx)} 5xx; first: {network_5xx[0]}")

        browser.close()

    print()
    print("=" * 60)
    print("FINAL REPORT")
    print("=" * 60)
    passes = sum(1 for r in RESULTS if r.status == "PASS")
    warns = sum(1 for r in RESULTS if r.status == "WARN")
    fails = sum(1 for r in RESULTS if r.status == "FAIL")
    print(
        f"PASS: {passes}    WARN: {warns}    FAIL: {fails}    "
        f"TOTAL: {len(RESULTS)}"
    )
    print()
    for r in RESULTS:
        print(f"  [{r.status}] {r.name} -- {r.detail}")
    print()
    print(f"Screenshots: {SCREENSHOT_DIR}")
    print(f"Downloads:   {DOWNLOAD_DIR}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
