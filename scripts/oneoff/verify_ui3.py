"""Deep verification - navigate into agent detail and config where labels appear."""
from playwright.sync_api import sync_playwright

URL = "http://zhanlu.ai:8000"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    page.set_default_timeout(15000)

    # Navigate to marketplace
    print("=== Marketplace ===")
    page.goto(f"{URL}/marketplace", timeout=10000)
    page.wait_for_timeout(3000)

    body = page.locator("body").inner_text()
    print(f"Body ({len(body)} chars): {body[:300]}")

    # Check for old labels in entire HTML
    html = page.content()
    has_old_en = "Core Capabilities" in html
    has_old_zh = "核心能力" in html
    has_new_en = "Tools" in html
    has_new_zh = "工具" in html

    print(f"  'Core Capabilities' in HTML: {has_old_en}")
    print(f"  '核心能力' in HTML: {has_old_zh}")
    print(f"  'Tools' in HTML: {has_new_en}")
    print(f"  '工具' in HTML: {has_new_zh}")

    # Try clicking on different elements to find agent cards
    # Look for any clickable cards or links
    print("\n=== Looking for agent cards/links ===")
    all_links = page.locator("a[href*='/agent/'], a[href*='/detail/'], [class*='card'], [class*='Card']")
    count = all_links.count()
    print(f"  Found {count} card/link elements")

    # Also check if there's a left sidebar or nav
    nav_items = page.locator("nav a, nav button, [class*='sidebar'] a").all()
    print(f"  Nav items: {len(nav_items)}")
    for item in nav_items[:5]:
        try:
            print(f"    - {item.inner_text()[:50]}")
        except:
            pass

    # Try to navigate directly to create or view agent config
    print("\n=== Try direct URL to agent detail ===")
    page.goto(f"{URL}/agent/new", timeout=10000)
    page.wait_for_timeout(3000)
    body = page.locator("body").inner_text()
    print(f"Body ({len(body)} chars): {body[:500]}")
    html = page.content()
    has_new_en = "Tools" in html
    has_new_zh = "工具" in html
    has_old_en = "Core Capabilities" in html
    print(f"  'Tools' in HTML: {has_new_en}")
    print(f"  '工具' in HTML: {has_new_zh}")
    print(f"  'Core Capabilities' in HTML: {has_old_en}")

    # Check for any existing agents via API
    print("\n=== Check API for agents ===")
    try:
        import urllib.request, json
        req = urllib.request.Request("http://zhanlu.ai:8000/api/agents")
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read())
        print(f"  API returned type: {type(data).__name__}")
        if isinstance(data, list):
            print(f"  Agents count: {len(data)}")
            for agent in data[:3]:
                ag_id = agent.get("id", "?")
                print(f"    Agent: {ag_id}")
        elif isinstance(data, dict):
            print(f"  Keys: {list(data.keys())}")
    except Exception as e:
        print(f"  API error: {e}")

    browser.close()
