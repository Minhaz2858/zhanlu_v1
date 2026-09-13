"""Verify 'Core Capabilities' → 'Tools' rename in Zhanlu UI."""
from playwright.sync_api import sync_playwright
import sys

URL = "http://zhanlu.ai:8000"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    page.set_default_timeout(15000)

    pages_to_check = [
        ("Home", "/"),
        ("Marketplace", "/marketplace"),
        ("Agent Builder", "/agent-builder"),
    ]

    all_ok = True

    for label, path in pages_to_check:
        print(f"\n{'='*50}")
        print(f"  {label} ({path})")
        print(f"{'='*50}")

        try:
            page.goto(f"{URL}{path}", timeout=10000)
            page.wait_for_timeout(3000)
        except Exception as e:
            print(f"  ERROR navigating: {e}")
            continue

        fname = path.replace("/", "_").strip("_") or "home"
        page.screenshot(path=f"/tmp/verify_{fname}.png", full_page=True)
        print(f"  Screenshot saved: /tmp/verify_{fname}.png")

        body = page.locator("body")
        try:
            body_text = body.inner_text(timeout=5000)
        except:
            body_text = ""
        print(f"  Body text length: {len(body_text)} chars")

        # Check old labels
        has_old_en = "Core Capabilities" in body_text
        has_old_zh = "核心能力" in body_text

        if has_old_en:
            print("  ❌ 'Core Capabilities' STILL PRESENT!")
            all_ok = False
        else:
            print("  ✅ 'Core Capabilities' not found")

        if has_old_zh:
            print("  ❌ '核心能力' STILL PRESENT!")
            all_ok = False
        else:
            print("  ✅ '核心能力' not found")

        # Check new labels
        has_new_en = "Tools" in body_text
        has_new_zh = "工具" in body_text

        if has_new_en:
            print("  ✅ 'Tools' found")
        else:
            print("  ⚠️  'Tools' not found in body text")

        if has_new_zh:
            print("  ✅ '工具' found")
        else:
            print("  ⚠️  '工具' not found in body text")

    print(f"\n{'='*50}")
    if all_ok:
        print("  RESULT: ALL CHECKS PASSED ✅")
    else:
        print("  RESULT: SOME LABELS STILL HAVE OLD TEXT ❌")
    print(f"{'='*50}")

    browser.close()
