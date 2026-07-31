"""Verify "Core Capabilities" → "Tools" rename across the Zhanlu UI."""
from playwright.sync_api import sync_playwright

URL = "http://zhanlu.ai:8000"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 900})

    results = []

    # --- 1. Marketplace page ---
    print("=== 1. Marketplace Page ===")
    page.goto(f"{URL}/marketplace", wait_until="networkidle")
    page.wait_for_timeout(2000)
    page.screenshot(path="/tmp/verify_marketplace.png", full_page=True)

    # Check for old labels
    old_en = page.locator("text=Core Capabilities").count()
    old_zh = page.locator("text=核心能力").count()
    new_en = page.locator("text=Tools").count()
    new_zh = page.locator("text=工具").count()

    print(f"  'Core Capabilities' (old EN): {old_en}")
    print(f"  '核心能力' (old ZH): {old_zh}")
    print(f"  'Tools' (new EN): {new_en}")
    print(f"  '工具' (new ZH): {new_zh}")
    results.append(("Marketplace", old_en == 0 and old_zh == 0, new_en, new_zh))

    # --- 2. Agent Detail page (find an agent card and click) ---
    print("\n=== 2. Agent Detail Page ===")
    # Try to click the first agent card
    agent_cards = page.locator(".agent-card, [class*='agent-card'], [class*='AgentCard']")
    card_count = agent_cards.count()
    print(f"  Agent cards found: {card_count}")
    
    if card_count > 0:
        agent_cards.first.click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2000)
        page.screenshot(path="/tmp/verify_detail.png", full_page=True)

        old_en = page.locator("text=Core Capabilities").count()
        old_zh = page.locator("text=核心能力").count()
        new_en = page.locator("text=Tools").count()
        new_zh = page.locator("text=工具").count()

        print(f"  'Core Capabilities' (old EN): {old_en}")
        print(f"  '核心能力' (old ZH): {old_zh}")
        print(f"  'Tools' (new EN): {new_en}")
        print(f"  '工具' (new ZH): {new_zh}")
        results.append(("Detail", old_en == 0 and old_zh == 0, new_en, new_zh))
    else:
        print("  WARNING: No agent cards found on marketplace page")
        results.append(("Detail", False, 0, 0))

    # --- 3. Agent Builder page ---
    print("\n=== 3. Agent Builder / Config Page ===")
    page.goto(f"{URL}/agent-builder", wait_until="networkidle")
    page.wait_for_timeout(2000)
    page.screenshot(path="/tmp/verify_builder.png", full_page=True)

    old_en = page.locator("text=Core Capabilities").count()
    old_zh = page.locator("text=核心能力").count()
    new_en = page.locator("text=Tools").count()
    new_zh = page.locator("text=工具").count()

    print(f"  'Core Capabilities' (old EN): {old_en}")
    print(f"  '核心能力' (old ZH): {old_zh}")
    print(f"  'Tools' (new EN): {new_en}")
    print(f"  '工具' (new ZH): {new_zh}")
    results.append(("AgentBuilder", old_en == 0 and old_zh == 0, new_en, new_zh))

    # --- 4. Full site scan ---
    print("\n=== 4. Full Site Scan ===")
    urls_to_check = [
        ("/", "Home"),
        ("/marketplace", "Marketplace"),
    ]
    for path, name in urls_to_check:
        page.goto(f"{URL}{path}", wait_until="networkidle")
        page.wait_for_timeout(1500)
        old_count = page.locator("text=Core Capabilities").count()
        print(f"  {name} ({path}): 'Core Capabilities' count = {old_count}")

    # --- Summary ---
    print("\n" + "=" * 50)
    print("SUMMARY")
    print("=" * 50)
    all_pass = True
    for name, passed, en_count, zh_count in results:
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_pass = False
        print(f"  {name}: {status} (Tools EN={en_count}, Tools ZH={zh_count})")

    if all_pass:
        print("\n ALL CHECKS PASSED - 'Core Capabilities' has been renamed to 'Tools' everywhere!")
    else:
        print("\n SOME CHECKS FAILED - See details above.")

    browser.close()
