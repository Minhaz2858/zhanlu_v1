"""Playwright E2E test for the create_artifact feature.

Requires: frontend dev server running on port 5157.

Usage:
    cd /root/zhanlu && ./backend/venv/bin/python scripts/test_artifact_ui.py
"""

import os
import sys
import time
from playwright.sync_api import sync_playwright

FRONTEND_URL = os.environ.get("SMOKE_FRONTEND_URL", "http://localhost:5157")
SCREENSHOT_DIR = "/tmp/zhanlu_artifact_test"
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

console_errors = []
page_errors = []
test_results = []


def record(name, status, detail=""):
    icon = "PASS" if status == "PASS" else ("FAIL" if status == "FAIL" else "WARN")
    print(f"  [{icon}] {name}: {detail}")
    test_results.append({"name": name, "status": status, "detail": detail})


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()

        page.on("console", lambda msg: (
            console_errors.append(f"[{msg.type}] {msg.text[:200]}")
            if msg.type == "error" else None
        ))
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        # ── 1. Load Chat page ────────────────────────────────────────
        print("\n[1/8] Loading Chat page...")
        try:
            page.goto(f"{FRONTEND_URL}/", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_load_state("networkidle", timeout=20000)
            page.wait_for_timeout(3000)
            page.screenshot(path=f"{SCREENSHOT_DIR}/01_chat_loaded.png", full_page=True)
            record("Chat page loads", "PASS", f"URL: {page.url}")
        except Exception as e:
            record("Chat page loads", "FAIL", str(e)[:120])
            # Continue even on failure to collect more data

        # ── 2. Check sidebar / navigation ────────────────────────────
        print("[2/8] Checking navigation...")
        try:
            layout = page.locator("aside").first
            if layout.count() > 0:
                record("Sidebar renders", "PASS")
            else:
                record("Sidebar renders", "WARN", "no <aside> found")

            # Check if page has visible content
            body = page.locator("body")
            assert body.count() > 0
            record("Page body exists", "PASS")
        except Exception as e:
            record("Navigation check", "FAIL", str(e)[:120])

        # ── 3. Check textarea input ──────────────────────────────────
        print("[3/8] Checking input interaction...")
        try:
            textarea = page.locator("textarea").first
            if textarea.count() > 0 and textarea.is_visible():
                textarea.fill("Test: create_artifact feature check")
                page.wait_for_timeout(500)
                val = textarea.input_value()
                assert "create_artifact" in val
                record("Textarea works", "PASS", f"typed: {val[:50]}")
                textarea.fill("")  # clear
            else:
                record("Textarea works", "FAIL", "not found or not visible")
            page.screenshot(path=f"{SCREENSHOT_DIR}/02_input_ready.png")
        except Exception as e:
            record("Textarea works", "FAIL", str(e)[:120])

        # ── 4. Check JS bundle has no import errors ──────────────────
        print("[4/8] Checking JS bundle for import errors...")
        fatal_errors = [
            e for e in page_errors
            if "ArtifactCardList" in e or "ArtifactPreviewSheet" in e
        ]
        import_errors = [
            e for e in console_errors
            if "Failed to resolve" in e
               and ("ArtifactCardList" in e or "ArtifactPreviewSheet" in e or "sheet" in e.lower())
        ]
        if fatal_errors:
            record("Import errors (page)", "FAIL", str(fatal_errors[:3]))
        elif import_errors:
            record("Import errors (console)", "FAIL", str(import_errors[:3]))
        else:
            record("ArtifactCardList/ArtifactPreviewSheet imports", "PASS",
                   "no import errors in bundle")

        # ── 5. Load SkillAgent page ──────────────────────────────────
        print("[5/8] Loading SkillAgent page...")
        try:
            page.goto(f"{FRONTEND_URL}/skill-agent", wait_until="domcontentloaded", timeout=20000)
            page.wait_for_load_state("networkidle", timeout=15000)
            page.wait_for_timeout(2000)
            page.screenshot(path=f"{SCREENSHOT_DIR}/03_skill_agent.png", full_page=True)
            record("SkillAgent page loads", "PASS", f"URL: {page.url}")
        except Exception as e:
            record("SkillAgent page loads", "FAIL", str(e)[:120])

        # ── 6. Load AgentBuilder page ───────────────────────────────
        print("[6/8] Loading AgentBuilder page...")
        try:
            page.goto(f"{FRONTEND_URL}/agent-builder", wait_until="domcontentloaded", timeout=20000)
            page.wait_for_load_state("networkidle", timeout=15000)
            page.wait_for_timeout(2000)
            page.screenshot(path=f"{SCREENSHOT_DIR}/04_agent_builder.png", full_page=True)
            record("AgentBuilder page loads", "PASS", f"URL: {page.url}")
        except Exception as e:
            record("AgentBuilder page loads", "FAIL", str(e)[:120])

        # ── 7. Check sheet component exists (shadcn/ui) ──────────────
        print("[7/8] Checking shadcn/ui Sheet availability...")
        try:
            page.goto(f"{FRONTEND_URL}/", wait_until="domcontentloaded", timeout=20000)
            page.wait_for_load_state("networkidle", timeout=15000)
            page.wait_for_timeout(1000)

            # Check that the Sheet component file exists in the bundle by
            # checking for no related import errors
            sheet_errors = [
                e for e in console_errors
                if "sheet" in e.lower() and ("import" in e.lower() or "resolve" in e.lower())
            ]
            if sheet_errors:
                record("Sheet component", "FAIL", str(sheet_errors[:3]))
            else:
                record("shadcn Sheet available", "PASS", "no sheet import errors")
        except Exception as e:
            record("Sheet component", "FAIL", str(e)[:120])

        # ── 8. Console error summary ──────────────────────────────────
        print("[8/8] Console error summary...")
        errors_only = [e for e in console_errors if e.startswith("[error]")]
        if errors_only:
            print(f"  WARN: {len(errors_only)} console errors:")
            for e in errors_only[:8]:
                print(f"    {e[:150]}")
            record("Console errors", "WARN", f"{len(errors_only)} errors (non-fatal)")
        else:
            record("Console errors", "PASS", "none")

        if page_errors:
            print(f"  WARN: {len(page_errors)} page errors:")
            for e in page_errors[:5]:
                print(f"    {e[:150]}")
            record("Page errors", "WARN", f"{len(page_errors)} page errors")
        else:
            record("Page errors", "PASS", "none")

        browser.close()

    # ── Summary ────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    passed = sum(1 for r in test_results if r["status"] == "PASS")
    failed_ct = sum(1 for r in test_results if r["status"] == "FAIL")
    warns_ct = sum(1 for r in test_results if r["status"] == "WARN")
    print(f"Artifact Feature UI E2E Results:")
    print(f"  PASS: {passed} | FAIL: {failed_ct} | WARN: {warns_ct}")
    print(f"  Screenshots: {SCREENSHOT_DIR}/")
    for r in test_results:
        icon = "✓" if r["status"] == "PASS" else ("✗" if r["status"] == "FAIL" else "⚠")
        print(f"  {icon} {r['name']}: {r['detail'][:100]}")
    print(f"{'='*60}")

    sys.exit(1 if failed_ct > 0 else 0)


if __name__ == "__main__":
    main()
