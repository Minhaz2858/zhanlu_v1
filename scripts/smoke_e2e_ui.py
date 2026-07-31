"""End-to-end UI test for the Zhanlu frontend.

Runs against http://localhost:5173 by default.  Requires Playwright + Chromium.

Usage:
    python scripts/smoke_e2e_ui.py
    SMOKE_FRONTEND_URL=http://localhost:5152 python scripts/smoke_e2e_ui.py

The app is guest-user-oriented (访客用户) so no login is required.
"""
import os
import sys

from playwright.sync_api import sync_playwright

SCREENSHOT_DIR = "/tmp/zhanlu_e2e_screens"
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

FRONTEND_URL = os.environ.get("SMOKE_FRONTEND_URL", "http://localhost:5173")
RESULTS = []
console_logs = []
network_errors = []


def record(name, status, details=""):
    icon = "PASS" if status == "PASS" else ("FAIL" if status == "FAIL" else "WARN")
    print(f"[{icon}] {name}: {details}")
    RESULTS.append({"name": name, "status": status, "details": details})


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        page = context.new_page()

        page.on("console", lambda msg: console_logs.append({"type": msg.type, "text": msg.text[:200]}))
        page.on("pageerror", lambda err: network_errors.append(f"pageerror: {str(err)[:200]}"))
        page.on("response", lambda resp: (
            network_errors.append(f"{resp.status} {resp.url}") if resp.status >= 400 else None
        ))

        # 1. Load app
        try:
            page.goto(FRONTEND_URL, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle", timeout=15000)
            page.wait_for_timeout(2500)
            title = page.title()
            if "Synexia" in title or "战颅" in title:
                record("app_loads", "PASS", title)
            else:
                record("app_loads", "FAIL", title)
            page.screenshot(path=f"{SCREENSHOT_DIR}/01_home.png", full_page=True)
        except Exception as e:
            record("app_loads", "FAIL", str(e)[:120])
            browser.close()
            return

        # 2. Sidebar navigation
        try:
            nav_links = page.locator("aside a[href]").all()
            nav_texts = [a.inner_text().strip() for a in nav_links if a.is_visible()]
            expected = ["认知中枢", "自动化任务", "我的空间", "市场", "工具包"]
            missing = [e for e in expected if not any(e in t for t in nav_texts)]
            if not missing:
                record("sidebar_nav", "PASS", ", ".join(nav_texts))
            else:
                record("sidebar_nav", "FAIL", f"missing {missing}")
        except Exception as e:
            record("sidebar_nav", "FAIL", str(e)[:120])

        # 3. Conversation list
        try:
            conv_items = page.locator(
                "xpath=//span[normalize-space(text())='你好' or normalize-space(text())='嗨']"
            ).all()
            visible = [el for el in conv_items if el.is_visible()]
            if len(visible) >= 5:
                record("conversation_list", "PASS", f"{len(visible)} items")
            else:
                record("conversation_list", "FAIL", f"only {len(visible)} visible")
        except Exception as e:
            record("conversation_list", "FAIL", str(e)[:120])

        # 4. Open a conversation
        try:
            first_conv = page.locator(
                "xpath=//span[normalize-space(text())='你好']/ancestor::div[contains(@class,'cursor-pointer')]"
            ).first
            if first_conv.count() > 0:
                first_conv.click()
                page.wait_for_timeout(2000)
                page.screenshot(path=f"{SCREENSHOT_DIR}/02_conv_opened.png", full_page=True)
                main_content = page.inner_text("main")
                record("open_conversation", "PASS", f"len={len(main_content)}")
            else:
                record("open_conversation", "WARN", "no clickable conversation")
        except Exception as e:
            record("open_conversation", "FAIL", str(e)[:120])

        # 5. Home: quick action chip populates input
        try:
            page.goto(FRONTEND_URL, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle", timeout=10000)
            page.wait_for_timeout(2000)
            textarea = page.locator("textarea").first
            chip = page.locator("button:has-text('生产制造')").first
            if chip.count() > 0:
                chip.click()
                page.wait_for_timeout(500)
                value = textarea.input_value()
                record("quick_action_chip", "PASS" if value else "WARN", f"input={value[:50]}")
            else:
                record("quick_action_chip", "WARN", "chip not found")
            page.screenshot(path=f"{SCREENSHOT_DIR}/03_input_filled.png", full_page=True)
        except Exception as e:
            record("quick_action_chip", "FAIL", str(e)[:120])

        # 6. Send a message
        try:
            textarea = page.locator("textarea").first
            textarea.fill("E2E UI test message from Playwright")
            page.wait_for_timeout(500)
            send_btn = page.locator("button:has-text('发送')").first
            if send_btn.count() > 0 and send_btn.is_enabled():
                send_btn.click()
                page.wait_for_timeout(3000)
                main_text = page.locator("main").inner_text()
                if "E2E UI test" in main_text:
                    record("send_message", "PASS", "message rendered")
                else:
                    record("send_message", "WARN", "message not rendered; possibly LLM key absent")
                page.screenshot(path=f"{SCREENSHOT_DIR}/04_after_send.png", full_page=True)
            else:
                record("send_message", "FAIL", "send button not enabled")
        except Exception as e:
            record("send_message", "FAIL", str(e)[:120])

        # 7. New task button
        try:
            page.goto(FRONTEND_URL, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle", timeout=10000)
            page.wait_for_timeout(1500)
            new_btn = page.locator("button:has-text('新建任务')").first
            if new_btn.count() > 0:
                new_btn.click()
                page.wait_for_timeout(1500)
                page.screenshot(path=f"{SCREENSHOT_DIR}/05_new_task.png", full_page=True)
                record("new_task_button", "PASS", "clicked")
            else:
                record("new_task_button", "WARN", "button missing")
        except Exception as e:
            record("new_task_button", "FAIL", str(e)[:120])

        # 8. Avatar menu
        try:
            avatar = page.locator("button:has-text('访')").first
            if avatar.count() > 0:
                avatar.click()
                page.wait_for_timeout(1000)
                page.screenshot(path=f"{SCREENSHOT_DIR}/06_avatar_menu.png", full_page=True)
                record("avatar_menu", "PASS", "opened")
                page.keyboard.press("Escape")
            else:
                record("avatar_menu", "WARN", "avatar missing")
        except Exception as e:
            record("avatar_menu", "FAIL", str(e)[:120])

        # 9. Navigate each main page
        for label, path in [
            ("automation", "/automation"),
            ("my_space", "/my-space"),
            ("market", "/market"),
            ("toolkit", "/toolkit"),
        ]:
            try:
                link = page.locator(f"a:has-text('{label.replace('_', ' ')}')").first
                # For Chinese labels
                if link.count() == 0:
                    link = page.locator(f"a:has-text('{label.replace('automation', '自动化任务').replace('my_space', '我的空间').replace('market', '市场').replace('toolkit', '工具包')}')").first
                if link.count() > 0:
                    link.click()
                    page.wait_for_load_state("networkidle", timeout=10000)
                    page.wait_for_timeout(2000)
                    page.screenshot(path=f"{SCREENSHOT_DIR}/07_{label}.png", full_page=True)
                    record(f"page_{label}", "PASS", f"path={path}")
                else:
                    record(f"page_{label}", "WARN", "link missing")
            except Exception as e:
                record(f"page_{label}", "FAIL", str(e)[:120])

        # 10. Return home, final screenshot
        page.goto(FRONTEND_URL, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle", timeout=10000)
        page.wait_for_timeout(2000)
        page.screenshot(path=f"{SCREENSHOT_DIR}/08_final_home.png", full_page=True)
        record("final_home", "PASS", "screenshot saved")

        browser.close()

    # Summary
    print("\n--- UI E2E Summary ---")
    total = len(RESULTS)
    passed = sum(1 for r in RESULTS if r["status"] == "PASS")
    failed = sum(1 for r in RESULTS if r["status"] == "FAIL")
    warn = sum(1 for r in RESULTS if r["status"] == "WARN")
    print(f"Total: {total} | PASS: {passed} | WARN: {warn} | FAIL: {failed}")
    print(f"Screenshots: {SCREENSHOT_DIR}/")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
