import sys
print("STARTING", flush=True)

from playwright.sync_api import sync_playwright

URL = "http://zhanlu.ai:8000"

try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.set_default_timeout(10000)

        for label, path in [
            ("My Space", "/my-space"),
            ("Agent Builder", "/agent-builder"),
            ("Market", "/market"),
        ]:
            print(f"\n=== {label} ({path}) ===", flush=True)
            try:
                page.goto(f"{URL}{path}", timeout=8000)
                page.wait_for_timeout(2000)
            except Exception as e:
                print(f"  GOTO ERROR: {e}", flush=True)
                continue

            html = page.content()
            for check in ["Core Capabilities", "核心能力", "Tools", "工具"]:
                found = check in html
                print(f"  '{check}': {found}", flush=True)

        browser.close()
        print("\nDONE", flush=True)
except Exception as e:
    print(f"FATAL: {e}", flush=True)
    import traceback
    traceback.print_exc()
