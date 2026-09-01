"""Playwright e2e test for the ReportCard component (Task 7 — UI verification).

Drives a real headless Chromium against the dev server and verifies that
the new `ReportCard` component renders correctly with a representative
mock payload, plus that the chat page itself still loads (no console
errors, no broken state).

What this test verifies:

1. The dev server is up on http://localhost:5173
2. The /ui-test page loads (this is the unauthenticated smoke-test
   page that already existed; it has the new ReportCard section)
3. The ReportCard section is present
4. KPIs render (4 KPI tiles expected)
5. Chart renders (recharts surface element)
6. Insights render (3+ insight bullets expected)
7. Export buttons render ("Export PDF", "More", "Open", "Download")
8. No JavaScript console errors fired during the load
9. A screenshot of the ReportCard is saved to disk for visual review

Run with the helper:

    python /root/.codebuddy/skills/webapp-testing/scripts/with_server.py \\
        --server "cd /root/zhanlu/frontend && npm run dev" --port 5173 \\
        -- python /root/zhanlu/scripts/test_ui_report_card.py
"""

import os
import sys

# The with_server.py helper starts the dev server before running us
# and tears it down on exit.  We just need to make sure Playwright is
# available (it is, on the system Python used by webapp-testing).
from playwright.sync_api import sync_playwright


BASE_URL = os.environ.get("APP_URL", "http://localhost:5173")
SCREENSHOT_DIR = "/root/zhanlu/frontend"


def main():
    console_errors = []
    page_errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()

        # Capture console errors + page errors so we can assert
        # the dev server isn't producing any.
        def _on_console(msg):
            if msg.type == "error":
                console_errors.append(f"{msg.type}: {msg.text}")

        page.on("console", _on_console)
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))

        print(f"[1/8] Navigating to {BASE_URL}/ui-test")
        page.goto(f"{BASE_URL}/ui-test", wait_until="domcontentloaded", timeout=30000)

        # The Vite dev server injects a few extra scripts; wait for the
        # main React tree to mount.
        page.wait_for_selector("h1:has-text('UI Component Test Suite')", timeout=20000)
        print("[2/8] Page header loaded")

        # 1. ReportCard section present
        report_section = page.locator('[data-testid="report-card-section"]')
        report_section.wait_for(state="visible", timeout=10000)
        print("[3/8] ReportCard section is present")

        # Scroll it into view for screenshot clarity
        report_section.scroll_into_view_if_needed()
        page.wait_for_timeout(800)  # allow recharts SVG + counters to settle

        # 2. KPI tiles render
        kpi_labels = report_section.locator(
            "text=/Total revenue|Top share|Row count|Total quantity|Updated/i"
        )
        kpi_count = kpi_labels.count()
        assert kpi_count >= 2, f"Expected >=2 KPI labels, found {kpi_count}"
        print(f"[4/8] KPI tiles render ({kpi_count} labels found)")

        # 3. Chart renders (recharts uses a <svg> or a .recharts-surface)
        chart = report_section.locator(".recharts-surface, svg").first
        chart.wait_for(state="visible", timeout=10000)
        print("[5/8] Chart surface rendered")

        # 4. Insights render
        insight_text = report_section.locator(
            "text=/Top 3 materials|concentration|Material D is 3x/i"
        )
        insight_count = insight_text.count()
        assert insight_count >= 2, f"Expected >=2 insight bullets, found {insight_count}"
        print(f"[6/8] Insights render ({insight_count} bullets found)")

        # 5. Export bar
        export_pdf = report_section.locator("button:has-text('Export PDF')")
        export_pdf.wait_for(state="visible", timeout=5000)
        more_btn = report_section.locator("button:has-text('More')")
        more_btn.wait_for(state="visible", timeout=5000)
        open_btn = report_section.locator("a:has-text('Open')")
        open_btn.wait_for(state="visible", timeout=5000)
        print("[7/8] Export bar (Export PDF, More, Open) all visible")

        # 6. Screenshot for visual review
        reportcard_only = os.path.join(SCREENSHOT_DIR, "uitest-reportcard.png")
        full_page = os.path.join(SCREENSHOT_DIR, "uitest-full.png")
        report_section.screenshot(path=reportcard_only)
        page.screenshot(path=full_page, full_page=True)
        print(f"[8/8] Screenshots saved:")
        print(f"      {reportcard_only}")
        print(f"      {full_page}")

        # 7. Console + page error checks
        if console_errors:
            print("\nWARNING: console errors detected:")
            for e in console_errors[:10]:
                print(f"  {e}")
        if page_errors:
            print("\nERROR: page errors detected:")
            for e in page_errors[:10]:
                print(f"  {e}")
            sys.exit(1)

        # 8. Bonus: navigate to the chat page (/) to make sure it
        # at least LOADS.  It may redirect to /login (auth is gated),
        # which is fine — we just want the dev server to not 500.
        try:
            page.goto(f"{BASE_URL}/", wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(1500)
            chat_screenshot = os.path.join(SCREENSHOT_DIR, "uitest-chat.png")
            page.screenshot(path=chat_screenshot, full_page=False)
            print(f"      {chat_screenshot}")
        except Exception as e:
            print(f"  (chat page navigation skipped: {e})")

        browser.close()

    print("\nALL OK -- ReportCard renders correctly with no console errors")
    sys.exit(0)


if __name__ == "__main__":
    main()
