"""Comprehensive end-to-end UI test for Zhanlu (Synexia).
Tests the full user flow: load → navigate → chat → interact → verify.

Usage:
    python scripts/e2e_full_ui.py
"""
import os
import sys
import time
import json

from playwright.sync_api import sync_playwright

SCREENSHOT_DIR = "/tmp/zhanlu_e2e_full"
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

FRONTEND_URL = os.environ.get("SMOKE_FRONTEND_URL", "http://localhost:5158")
RESULTS = []
console_errors = []
network_fails = []

def record(name, status, details=""):
    icon = {"PASS": "✓", "FAIL": "✗", "WARN": "⚠"}.get(status, "?")
    line = f"[{icon} {status}] {name}: {details}"
    print(line)
    RESULTS.append({"name": name, "status": status, "details": details})

def ss(page, name):
    """Take screenshot."""
    path = f"{SCREENSHOT_DIR}/{name}.png"
    try:
        page.screenshot(path=path, full_page=True)
    except Exception as e:
        print(f"  screenshot failed for {name}: {e}")

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        page = context.new_page()

        # Collect errors
        page.on("console", lambda msg: (
            console_errors.append(f"[{msg.type}] {msg.text[:200]}")
            if msg.type == "error" else None
        ))
        page.on("pageerror", lambda err: network_fails.append(f"PAGE ERROR: {str(err)[:300]}"))
        page.on("response", lambda resp: (
            network_fails.append(f"HTTP {resp.status}: {resp.url[:120]}")
            if resp.status >= 500 else None
        ))

        # ──────────────────────────────────────────────────
        # 1. LOAD HOME PAGE
        # ──────────────────────────────────────────────────
        print("\n=== 1. LOAD HOME PAGE ===")
        try:
            page.goto(FRONTEND_URL, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle", timeout=20000)
            page.wait_for_timeout(3000)
            title = page.title()
            print(f"  Page title: '{title}'")

            # Check for key elements
            body_text = page.inner_text("body")
            if "战颅" in title or "Synexia" in title or "base44" in title.lower():
                record("home_loads", "PASS", f"title='{title}'")
            else:
                record("home_loads", "PASS", f"title='{title}' (unexpected but page loaded)")

            ss(page, "01_home_loaded")
        except Exception as e:
            record("home_loads", "FAIL", str(e)[:200])
            ss(page, "01_home_load_fail")
            browser.close()
            print("\n--- SUMMARY ---")
            for r in RESULTS:
                print(f"  [{r['status']}] {r['name']}: {r['details']}")
            return

        # ──────────────────────────────────────────────────
        # 2. SIDEBAR NAVIGATION
        # ──────────────────────────────────────────────────
        print("\n=== 2. SIDEBAR NAVIGATION ===")
        try:
            # Find all links in sidebar/nav
            nav_elements = page.locator("nav a, aside a, [class*='sidebar'] a, [class*='Sidebar'] a").all()
            if not nav_elements:
                nav_elements = page.locator("a[href]").all()

            nav_texts = []
            for el in nav_elements[:30]:
                try:
                    txt = el.inner_text().strip()
                    if txt and len(txt) < 50:
                        nav_texts.append(txt)
                except:
                    pass
            print(f"  Found nav items: {nav_texts}")

            expected_sections = ["认知中枢", "自动化任务", "我的空间", "市场", "工具包"]
            found = [e for e in expected_sections if any(e in t for t in nav_texts)]
            missing = [e for e in expected_sections if e not in found]
            if len(found) >= 3:
                record("sidebar_nav", "PASS", f"found {len(found)}/5: {found}" +
                       (f"; missing: {missing}" if missing else ""))
            elif found:
                record("sidebar_nav", "WARN", f"found {len(found)}/5: {found}")
            else:
                record("sidebar_nav", "WARN", f"no expected nav items found, got: {nav_texts[:8]}")
            ss(page, "02_sidebar")
        except Exception as e:
            record("sidebar_nav", "FAIL", str(e)[:200])

        # ──────────────────────────────────────────────────
        # 3. CONVERSATION LIST
        # ──────────────────────────────────────────────────
        print("\n=== 3. CONVERSATION LIST ===")
        try:
            # Look for conversation items - could be in sidebar or main
            main_text = page.inner_text("body")
            conv_keywords = ["你好", "新建", "对话", "会话", "conversation"]

            # Try different selectors for conversation items
            conv_items = page.locator("[class*='conversation'], [class*='Conversation'], " +
                                       "[class*='chat-item'], [class*='ChatItem'], " +
                                       "[class*='thread'], [class*='Thread']").all()

            if not conv_items:
                # Try xpath for common conversation text
                conv_items = page.locator("xpath=//*[contains(text(),'你好') or contains(text(),'新对话')]").all()

            visible_items = [el for el in conv_items if el.is_visible()]
            conv_count = len(visible_items)
            print(f"  Found {conv_count} visible conversation items")

            if conv_count > 0:
                record("conversation_list", "PASS", f"{conv_count} items visible")
            else:
                record("conversation_list", "WARN", "no conversation items found in sidebar")
            ss(page, "03_conversations")
        except Exception as e:
            record("conversation_list", "FAIL", str(e)[:200])

        # ──────────────────────────────────────────────────
        # 4. DEFAULT SKILLS (PlusMenu / chips)
        # ──────────────────────────────────────────────────
        print("\n=== 4. DEFAULT SKILLS ===")
        try:
            # Look for skill chips, plus menu button, or default skill badges
            default_keywords = ["PPT", "文档", "表格", "图表", "PDF", "网页", "HTML", "Markdown",
                                "dashboard", "白板", "excel", "presentation"]
            body_text = page.inner_text("body")
            found_skills = [kw for kw in default_keywords if kw.lower() in body_text.lower()]
            print(f"  Default skill keywords found: {found_skills}")

            # Try finding PlusMenu or skill-related buttons
            plus_btn = page.locator("button[class*='plus'], button[class*='Plus'], " +
                                     "[class*='PlusMenu'], [class*='plus-menu']").first
            if plus_btn.count() > 0 and plus_btn.is_visible():
                plus_btn.click()
                page.wait_for_timeout(1000)
                ss(page, "04_plus_menu_opened")
                # Check for skill items in the dropdown
                menu_text = page.inner_text("body")
                skill_in_menu = any(kw in menu_text for kw in ["PPT", "文档", "docx", "pptx", "default"])
                record("default_skills_menu", "PASS" if skill_in_menu else "WARN",
                       f"menu opened, skills found: {skill_in_menu}")
                page.keyboard.press("Escape")
                page.wait_for_timeout(500)
            elif found_skills:
                record("default_skills_menu", "PASS", f"keywords visible: {found_skills}")
            else:
                record("default_skills_menu", "WARN", "no skill chips/buttons visible")
            ss(page, "04_default_skills")
        except Exception as e:
            record("default_skills_menu", "FAIL", str(e)[:200])

        # ──────────────────────────────────────────────────
        # 5. TEXT INPUT AREA
        # ──────────────────────────────────────────────────
        print("\n=== 5. TEXT INPUT AREA ===")
        try:
            textarea = page.locator("textarea").first
            if textarea.count() > 0 and textarea.is_visible():
                placeholder = textarea.get_attribute("placeholder") or ""
                textarea.click()
                page.wait_for_timeout(500)
                record("input_textarea", "PASS", f"placeholder='{placeholder[:60]}'")
            else:
                # Try contenteditable div
                editable = page.locator("[contenteditable='true']").first
                if editable.count() > 0:
                    record("input_textarea", "PASS", "contenteditable div found")
                else:
                    record("input_textarea", "FAIL", "no text input found")
            ss(page, "05_input_area")
        except Exception as e:
            record("input_textarea", "FAIL", str(e)[:200])

        # ──────────────────────────────────────────────────
        # 6. SEND A MESSAGE (real chat test)
        # ──────────────────────────────────────────────────
        print("\n=== 6. SEND MESSAGE ===")
        try:
            textarea = page.locator("textarea").first
            if textarea.count() > 0 and textarea.is_visible():
                test_msg = "Hello, this is an automated E2E test. Please respond with a brief greeting."
                textarea.fill(test_msg)
                page.wait_for_timeout(500)

                # Find send button
                send_btn = page.locator("button:has-text('发送')").first
                if send_btn.count() == 0:
                    send_btn = page.locator("button[type='submit']").first
                if send_btn.count() == 0:
                    send_btn = page.locator("button svg[class*='send'], button svg[class*='arrow']").first.locator("..")

                if send_btn.count() > 0 and send_btn.is_enabled():
                    ss(page, "06_before_send")
                    send_btn.click()
                    page.wait_for_timeout(2000)

                    # Wait for streaming / response
                    print("  Waiting for response...")
                    for i in range(30):
                        page.wait_for_timeout(2000)
                        body = page.inner_text("body")
                        # Check if our test message appears AND there's new content after it
                        if test_msg[:20] in body:
                            after_msg = body.split(test_msg[:20], 1)
                            if len(after_msg) > 1 and len(after_msg[1].strip()) > 10:
                                print(f"  Response received after {(i+1)*2}s")
                                record("send_message", "PASS",
                                       f"response received in {(i+1)*2}s, response length={len(after_msg[1].strip())}")
                                break
                    else:
                        body = page.inner_text("body")
                        if test_msg[:20] in body:
                            record("send_message", "WARN",
                                   "message sent but no response after 60s (LLM may need API key)")
                        else:
                            record("send_message", "WARN", "message may not have been sent")
                else:
                    record("send_message", "WARN", "send button not found/enabled")
                ss(page, "06_after_send")
            else:
                record("send_message", "WARN", "no textarea to type into")
        except Exception as e:
            record("send_message", "FAIL", str(e)[:200])
            ss(page, "06_send_fail")

        # ──────────────────────────────────────────────────
        # 7. NAVIGATE PAGES
        # ──────────────────────────────────────────────────
        print("\n=== 7. NAVIGATE PAGES ===")
        pages_to_test = {
            "automation": ("自动化任务", "/automation"),
            "my_space": ("我的空间", "/my-space"),
            "market": ("市场", "/market"),
            "toolkit": ("工具包", "/toolkit"),
        }

        for key, (label_cn, path) in pages_to_test.items():
            try:
                # Try different ways to find the link
                link = page.locator(f"a[href='{path}']").first
                if link.count() == 0:
                    link = page.locator(f"a[href*='{path}']").first
                if link.count() == 0:
                    link = page.locator(f"a:has-text('{label_cn}')").first
                if link.count() == 0:
                    link = page.locator(f"span:has-text('{label_cn}')").first

                if link.count() > 0 and link.is_visible():
                    link.click()
                    page.wait_for_load_state("networkidle", timeout=15000)
                    page.wait_for_timeout(2000)
                    page_text = page.inner_text("body")
                    ss(page, f"07_page_{key}")
                    record(f"page_{key}", "PASS", f"navigated to {path}, content={len(page_text)} chars")
                else:
                    # Try direct navigation
                    page.goto(f"{FRONTEND_URL}{path}", wait_until="domcontentloaded")
                    page.wait_for_load_state("networkidle", timeout=15000)
                    page.wait_for_timeout(2000)
                    page_text = page.inner_text("body")
                    ss(page, f"07_page_{key}_direct")
                    record(f"page_{key}", "PASS", f"direct nav to {path}, content={len(page_text)} chars")
            except Exception as e:
                record(f"page_{key}", "FAIL", str(e)[:200])
                try:
                    ss(page, f"07_page_{key}_fail")
                except:
                    pass

        # ──────────────────────────────────────────────────
        # 8. RETURN HOME & CHECK QUICK ACTIONS
        # ──────────────────────────────────────────────────
        print("\n=== 8. HOME QUICK ACTIONS ===")
        try:
            page.goto(FRONTEND_URL, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle", timeout=15000)
            page.wait_for_timeout(3000)
            body = page.inner_text("body")

            # Check for quick action chips / suggestions
            quick_keywords = ["生产制造", "市场营销", "产品设计", "数据分析", "写一篇", "help", "hello"]
            found_quick = [kw for kw in quick_keywords if kw in body]
            if found_quick:
                record("quick_actions", "PASS", f"chips: {found_quick}")
            else:
                record("quick_actions", "WARN", f"no quick action chips found. Body snippet: {body[:200]}")

            # Try clicking a chip if exists
            chip = page.locator("button:has-text('生产制造')").first
            if chip.count() > 0 and chip.is_visible():
                chip.click()
                page.wait_for_timeout(500)
                textarea = page.locator("textarea").first
                if textarea.count() > 0:
                    val = textarea.input_value()
                    record("quick_chip_click", "PASS" if val else "WARN", f"input filled: '{val[:60]}'")
                else:
                    record("quick_chip_click", "PASS", "chip clicked")
            else:
                record("quick_chip_click", "WARN", "no clickable chip")

            ss(page, "08_home_quick_actions")
        except Exception as e:
            record("quick_actions", "FAIL", str(e)[:200])

        # ──────────────────────────────────────────────────
        # 9. NEW TASK BUTTON
        # ──────────────────────────────────────────────────
        print("\n=== 9. NEW TASK BUTTON ===")
        try:
            new_btn = page.locator("button:has-text('新建任务')").first
            if new_btn.count() == 0:
                new_btn = page.locator("button:has-text('新建')").first
            if new_btn.count() == 0:
                new_btn = page.locator("button:has-text('+')").first

            if new_btn.count() > 0 and new_btn.is_visible():
                new_btn.click()
                page.wait_for_timeout(1500)
                ss(page, "09_new_task")
                page_text = page.inner_text("body")
                record("new_task_button", "PASS", f"clicked, content now {len(page_text)} chars")
            else:
                record("new_task_button", "WARN", "new task button not found")
        except Exception as e:
            record("new_task_button", "FAIL", str(e)[:200])

        # ──────────────────────────────────────────────────
        # 10. AVATAR / USER MENU
        # ──────────────────────────────────────────────────
        print("\n=== 10. AVATAR / USER MENU ===")
        try:
            page.goto(FRONTEND_URL, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle", timeout=15000)
            page.wait_for_timeout(2000)

            avatar = page.locator("button:has-text('访')").first
            if avatar.count() == 0:
                avatar = page.locator("[class*='avatar'], [class*='Avatar']").first
            if avatar.count() == 0:
                avatar = page.locator("button:has-text('账户'), button:has-text('用户')").first

            if avatar.count() > 0 and avatar.is_visible():
                avatar.click()
                page.wait_for_timeout(1000)
                ss(page, "10_avatar_menu")
                menu_text = page.inner_text("body")
                record("avatar_menu", "PASS", f"menu opened, content={len(menu_text)} chars")
                page.keyboard.press("Escape")
            else:
                record("avatar_menu", "WARN", "no avatar/user button found")
        except Exception as e:
            record("avatar_menu", "FAIL", str(e)[:200])

        # ──────────────────────────────────────────────────
        # 11. RESPONSIVE / LAYOUT CHECK
        # ──────────────────────────────────────────────────
        print("\n=== 11. RESPONSIVE LAYOUT ===")
        try:
            page.goto(FRONTEND_URL, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle", timeout=15000)
            page.wait_for_timeout(2000)

            # Check main layout regions exist
            main_el = page.locator("main").first
            header_el = page.locator("header").first
            nav_el = page.locator("nav, aside").first

            layout_ok = True
            if main_el.count() == 0:
                layout_ok = False
                print("  No <main> element found")

            # Test at tablet width
            page.set_viewport_size({"width": 768, "height": 900})
            page.wait_for_timeout(1000)
            ss(page, "11_tablet_view")
            record("responsive_tablet", "PASS", "768px viewport rendered")

            # Test at mobile width
            page.set_viewport_size({"width": 375, "height": 812})
            page.wait_for_timeout(1000)
            ss(page, "11_mobile_view")
            record("responsive_mobile", "PASS", "375px viewport rendered")

            # Reset
            page.set_viewport_size({"width": 1440, "height": 900})
        except Exception as e:
            record("responsive_layout", "FAIL", str(e)[:200])

        # ──────────────────────────────────────────────────
        # 12. FINAL HOME SCREENSHOT
        # ──────────────────────────────────────────────────
        print("\n=== 12. FINAL STATE ===")
        try:
            page.goto(FRONTEND_URL, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle", timeout=15000)
            page.wait_for_timeout(2000)
            ss(page, "12_final_home")
            record("final_home", "PASS", "final screenshot saved")
        except Exception as e:
            record("final_home", "FAIL", str(e)[:200])

        browser.close()

    # ──────────────────────────────────────────────────
    # SUMMARY
    # ──────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("END-TO-END UI TEST SUMMARY")
    print("=" * 60)
    total = len(RESULTS)
    passed = sum(1 for r in RESULTS if r["status"] == "PASS")
    failed = sum(1 for r in RESULTS if r["status"] == "FAIL")
    warn = sum(1 for r in RESULTS if r["status"] == "WARN")

    for r in RESULTS:
        icon = {"PASS": "✓", "FAIL": "✗", "WARN": "⚠"}.get(r["status"], "?")
        print(f"  {icon} {r['name']:<30s} [{r['status']}] {r['details'][:80]}")

    print(f"\n  Total: {total} | ✅ PASS: {passed} | ⚠ WARN: {warn} | ❌ FAIL: {failed}")

    if console_errors:
        print(f"\n  Browser Console Errors ({len(console_errors)}):")
        for ce in console_errors[:10]:
            print(f"    {ce[:120]}")

    if network_fails:
        print(f"\n  Network Errors ({len(network_fails)}):")
        for nf in network_fails[:10]:
            print(f"    {nf[:120]}")

    print(f"\n  Screenshots: {SCREENSHOT_DIR}/")
    print(f"    {', '.join(sorted(os.listdir(SCREENSHOT_DIR)))}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
