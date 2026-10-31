"""EBOM browser-level production validation (Workstream A).

Drives the REAL frontend + backend through all six experiences, exercising
every directive scenario: governed KPIs + provenance, entity navigation,
situation investigation, blocked-then-approved execution, governed ERP
write, outcome measurement + verification, lifecycle trace, operational
health. Screenshots land in docs/evidence/ebom-browser/.
"""
import json
import os
import sys
import time

import requests
from playwright.sync_api import sync_playwright

FRONTEND = "http://localhost:5157"
API = "http://localhost:5157/api"  # via vite proxy
SHOT_DIR = ("/Users/minhazulislam/Desktop/zhanlu_n8n_main_version/"
            "docs/evidence/ebom-browser")
os.makedirs(SHOT_DIR, exist_ok=True)

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'} — {name}" + (f" ({detail})" if detail else ""))


def shot(page, name):
    page.screenshot(path=f"{SHOT_DIR}/{name}.png", full_page=True)


# ---- API fixture setup: decision records + outcome (deterministic seeds) ----
login = requests.post(f"{API}/apps/default-app/auth/login",
                      json={"email": "admin@zhanlu.dev", "password": "admin123"})
assert login.ok, login.text
TOKEN = login.json()["access_token"]
H = {"Authorization": f"Bearer {TOKEN}"}
check("api login", True)

# A governed decision trail to exercise D and E through the UI:
# escalation (problem) -> approval -> approved PO draft -> governed action.
import pg_helper  # noqa: E402  (sets DATABASE_URL env before app imports)

with pg_helper.session() as s:
    from app.models.escalation import Escalation
    from app.models.governance import ApprovalRequest
    from app.models.ontology import OntologyAction
    from app.models.procurement import ProcurementPoDraft

    if not s.query(Escalation).filter_by(id="esc-browser").first():
        s.add(Escalation(
            id="esc-browser", org_id="org-sandbox",
            summary="Model X margin declining 140 in 2026-10 — mitigation PO required",
            status="open", sentiment="asks_human", object_name="VehicleModel"))
    if not s.query(ProcurementPoDraft).filter_by(id="pd-browser").first():
        s.add(ProcurementPoDraft(
            id="pd-browser", org_id="org-sandbox",
            supplier_name="Acme Components", product="Model X repricing",
            quantity=1, unit="lot", est_amount=50.0, currency="USD",
            status="approved", approval_id="apr-browser"))
    if not s.query(OntologyAction).filter_by(id="act-browser").first():
        s.add(OntologyAction(
            id="act-browser", org_id="org-sandbox", object_id="obj-x",
            name="erp_submit", tool_name="erp_submit_po", risk_level="write",
            is_active=True, is_deleted=False))
    if not s.query(ApprovalRequest).filter_by(id="apr-browser").first():
        s.add(ApprovalRequest(
            id="apr-browser", org_id="org-sandbox", status="approved",
            action_type="ontology_write", reviewed_by="user-sandbox-admin",
            action_description="Submit Model X mitigation PO to the sandbox ERP",
            context_json={"action_id": "act-browser",
                          "tool_name": "erp_submit_po",
                          "params": {"draft_id": "pd-browser"},
                          "escalation_id": "esc-browser"}))
    s.commit()
check("decision fixture seeded", True)

# Record the expected outcome (org-admin governance act) via the real endpoint.
o = requests.post(f"{API}/ontology/outcomes", headers=H, json={
    "metric_ref": "met-gross-margin", "direction": "increase",
    "baseline_period": "2026-09", "observed_period": "2026-10",
    "expected_impact": 150.0, "approval_id": "apr-browser",
    "escalation_id": "esc-browser", "draft_id": "pd-browser"})
check("outcome recorded via API", o.ok and o.json().get("success"), o.text[:120])
OUTCOME_ID = o.json()["outcome"]["id"]

# A second, still-pending measurement on a DIFFERENT governed metric so the
# browser can exercise Measure/Verify live (the margin outcome is frozen
# once verified — correct system behavior).
o2 = requests.post(f"{API}/ontology/outcomes", headers=H, json={
    "metric_ref": "met-total-cost", "direction": "decrease",
    "baseline_period": "2026-09", "observed_period": "2026-10",
    "expected_impact": 200.0, "approval_id": "apr-browser",
    "escalation_id": "esc-browser", "draft_id": "pd-browser"})
check("second outcome recorded", o2.ok and o2.json().get("success"), o2.text[:120])

# ------------------------------- browser ------------------------------------
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1000})

    # 1-2. Login through the UI and open Enterprise Profile.
    page.goto(f"{FRONTEND}/login?app_id=default-app")
    page.wait_for_load_state("networkidle")
    page.get_by_role("button", name="密码").first.click()  # switch to password mode
    time.sleep(1)
    page.fill("#email", "admin@zhanlu.dev")
    page.fill("#password", "admin123")
    page.get_by_role("button", name="登录").click()
    page.wait_for_load_state("networkidle")
    time.sleep(2)
    check("ui login", "base44_access_token" in page.evaluate(
        "() => Object.keys(localStorage)"), "")

    page.goto(f"{FRONTEND}/admin/enterprise-profile?app_id=default-app")
    page.wait_for_load_state("networkidle")

    # 3-4. Experience A: governed KPIs + provenance.
    page.wait_for_selector("text=Governed Business KPIs", timeout=20000)
    time.sleep(2)  # KPI cards compute via /compare + /trend
    body = page.inner_text("body")
    check("A: KPI section renders", "Governed Business KPIs" in body)
    check("A: governed gross margin value", "1,880" in body or "1880" in body)
    check("A: provenance shown", "sales_transactions" in body)
    # Expand the KPI definition popover: prior period + SQL trace live there.
    page.locator("summary").first.click()
    time.sleep(1)
    import re
    body = page.inner_text("body")
    check("A: prior-period delta in definition",
          bool(re.search(r"prior period: \d{4}-\d{2} = [\d.,]+", body)))
    check("A: governed SQL trace", "SELECT" in body.upper())
    shot(page, "A-executive-overview")

    # 5-6. Experience C: business explorer — navigate an entity.
    page.get_by_role("button", name="Object Explorer").click()
    page.wait_for_selector("text=Business Object Explorer", timeout=15000)
    time.sleep(1.5)
    shot(page, "C-object-explorer-default")
    obj_btns = page.locator("ul li button")
    if obj_btns.count():
        obj_btns.first.click()
        time.sleep(1.5)
        body = page.inner_text("body")
        # Panel headers are CSS-uppercased — inner_text returns them uppercased.
        check("C: entity detail opens", "RELATIONSHIPS" in body)
        check("C: governed metrics panel", "GOVERNED METRICS" in body)
        check("C: evidence panel", "DOCUMENT EVIDENCE" in body)
    shot(page, "C-object-explorer-entity")

    # 7-9. Experience B: situation room — select an entity, ask the agent.
    page.get_by_role("button", name="Situation Room").click()
    page.wait_for_selector("text=Investigate a business question", timeout=15000)
    page.wait_for_selector("ul li button", timeout=10000)  # entity list loaded
    page.locator("ul li button").first.click()
    time.sleep(1)
    page.fill("textarea",
              "Why is Vehicle Model X profitability declining?")
    page.click("text=Ask the Zhanlu Agent")
    # The agent harness may take up to ~2 min (LLM sub-loop); wait honestly.
    try:
        page.wait_for_selector(
            "text=Ask the Zhanlu Agent >> nth=0", state="visible", timeout=3000)
    except Exception:
        pass
    answered = False
    for _ in range(48):  # up to 4 min
        time.sleep(5)
        body = page.inner_text("body")
        if "synthesis via the existing Agent Harness" in body:
            btn = page.locator("button:has-text('Ask the Zhanlu Agent')")
            if btn.is_disabled():
                continue
        if "Model" in body and ("margin" in body.lower() or "declin" in body.lower()):
            answered = True
            break
        if "failed" in body.lower() or "unavailable" in body.lower():
            break
    body = page.inner_text("body")
    check("B: agent investigation returned", answered,
          "LLM synthesis or honest error" if not answered else "")
    check("B: verified findings panel", "VERIFIED FINDINGS" in body)
    check("B: hypotheses separated", "HYPOTHESES" in body)
    shot(page, "B-situation-room")

    # 10-14. Experience D: blocked without authorization, then approved.
    page.get_by_role("button", name="Decisions").click()
    try:
        page.wait_for_selector("text=Decision & Action Center", timeout=15000)
    except Exception:
        body = page.inner_text("body")
        print("D-DEBUG body head:", body[:300].replace("\n", " | "))
        raise
    time.sleep(1.5)
    body = page.inner_text("body")
    check("D: escalation visible", "Model X margin declining" in body)
    check("D: approval-gated action listed", "erp_submit" in body)
    shot(page, "D-decision-center")
    # Execute with a WRONG approval id -> honest refusal.
    page.click("button:has-text('Execute…')")
    page.fill('input[placeholder*="approval id"]', "apr-wrong-id")
    page.click("button:has-text('Execute')")
    time.sleep(2)
    body = page.inner_text("body")
    check("D: unauthorized execution refused", "Refused" in body, "")
    shot(page, "D-execution-blocked")
    # Now with the real approval id -> governed success.
    page.fill('input[placeholder*="approval id"]', "apr-browser")
    page.click("button:has-text('Execute')")
    time.sleep(4)
    body = page.inner_text("body")
    check("D: approved execution succeeds", "Executed under approval" in body)
    check("D: sandbox ERP ref recorded", "SANDBOX-PO-" in body)
    shot(page, "D-execution-approved")

    # 15-16. Experience E: measure, verify, trace the lifecycle.
    page.get_by_role("button", name="Outcomes").click()
    page.wait_for_selector("text=Business Outcome Tracking", timeout=15000)
    time.sleep(1.5)
    body = page.inner_text("body")
    check("E: outcome card shows expected vs measured",
          "EXPECTED" in body and "MEASURED (REALIZED)" in body)
    check("E: lifecycle timeline", "Problem detected" in body)
    shot(page, "E-outcomes-before-measure")
    body = page.inner_text("body")
    check("E: measured realized impact (-80)", "-80" in body)
    # The pending revenue measurement: Measure via the UI.
    page.locator("button:has-text('Measure')").first.click()
    time.sleep(3)
    body = page.inner_text("body")
    check("E: pending outcome measured via UI", "not yet measured" not in body)
    shot(page, "E-outcomes-measured")
    page.locator("button:has-text('Verify')").first.click()
    time.sleep(3)
    body = page.inner_text("body")
    check("E: verified badge", "verified" in body.lower())
    shot(page, "E-outcomes-verified")
    # Lifecycle trace from the escalation anchor.
    page.fill('input[placeholder*="escalation"]', "esc-browser")
    page.click("button:has-text('Trace')")
    time.sleep(2.5)
    body = page.inner_text("body")
    check("E: full lifecycle traced", "Outcome verified" in body
          and "SANDBOX-PO-" in body)
    shot(page, "E-lifecycle-traced")

    # 17-18. Experience F: operational health.
    page.get_by_role("button", name="Health").click()
    page.wait_for_selector("text=Operational Health & Governance", timeout=15000)
    time.sleep(2)
    body = page.inner_text("body")
    check("F: totals render", "DOMAINS" in body and "OBJECTS LIVE" in body)
    check("F: coverage map", "Coverage Map" in body)
    shot(page, "F-operational-health")

    browser.close()

print("\n==== SUMMARY ====")
failed = [r for r in results if not r[1]]
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
for name, ok, detail in failed:
    print(f"FAILED: {name} {detail}")
sys.exit(1 if failed else 0)
