#!/usr/bin/env python3
"""Build + publish + smoke-run the Business Manager Command Center workflow.

Complex agentic workflow: trigger_chat -> orchestrator LLM (classify) ->
2-switch cascade (6 intents) -> specialist AGENT branches, each fed by a
deterministic SQL db_query node against demo_e2e (the company warehouse) ->
Manager Synthesis (KPI-authoritative) -> output brief.

Run from the host (uses localhost:5002 API). Requires /tmp/zhanlu_token.txt.
"""
import json
import sys
import time
import urllib.request

BASE = "http://localhost:5002/api/apps/default-app"
TOKEN = open("/tmp/zhanlu_token.txt").read().strip()

# ---- entity ids (verified live) ----
KB_DEMO = "kb-demo-001"  # Demo E2E PostgreSQL -> demo_e2e (customers/orders/products)
AGENTS = {
    "sales": "e4d0ba0e-bb4c-464d-97c0-11fbd1c1c935",          # Sales Agent
    "finance": "ecd92353-ffdd-4d6c-a85c-579086e3dfd4",        # Finance Agent
    "customers": "862c8afc-2738-4938-806c-8bef3424b0b5",      # Customer Support Agent
    "ops": "cebb33aa-23e9-4d00-bfaa-8f3d3f9daefc",            # Business Operations Agent
    "writer": "ae8b9b1e-caa5-4548-bd0f-c03c5a28b977",         # Report Writer
    "general": "81dcaadb-ff96-427a-9ffc-1d4466b39789",        # general_assistant
}

# ---- deterministic SQL (Postgres demo_e2e) ----
SQL_KPI = """SELECT
 (SELECT COUNT(*) FROM customers) AS total_customers,
 (SELECT COUNT(*) FROM products) AS total_products,
 (SELECT COUNT(*) FROM orders) AS total_orders,
 (SELECT ROUND(SUM(revenue),2) FROM customers) AS total_revenue,
 (SELECT ROUND(AVG(margin)*100,2) FROM orders) AS avg_margin_pct,
 (SELECT SUM(qty) FROM orders) AS total_qty"""
SQL_SALES = "SELECT o.region, COUNT(*) AS orders, SUM(o.qty) AS qty, ROUND(AVG(o.margin)*100,2) AS avg_margin_pct FROM orders o GROUP BY o.region ORDER BY orders DESC"
SQL_CUSTOMERS = "SELECT c.name, c.region, c.revenue, ROUND(100*c.revenue/(SELECT SUM(revenue) FROM customers),1) AS pct_of_total FROM customers c ORDER BY c.revenue DESC"
SQL_PRODUCTS = "SELECT p.name, p.volume, COALESCE(SUM(o.qty),0) AS ordered_qty FROM products p LEFT JOIN orders o ON o.product_id=p.id GROUP BY p.name, p.volume ORDER BY p.volume DESC"
SQL_FINANCIAL = "SELECT c.region, ROUND(SUM(c.revenue),2) AS customer_revenue, COUNT(o.id) AS orders FROM customers c LEFT JOIN orders o ON o.customer_id=c.id GROUP BY c.region ORDER BY customer_revenue DESC"
SQL_REPORT = "SELECT region, COUNT(*) AS orders, SUM(qty) AS qty FROM orders GROUP BY region ORDER BY orders DESC"

CLASSIFIER_PROMPT = """You classify a manager's question into one category. Reply with ONLY the category token.

Scan the question for keywords in this exact order and pick the FIRST match:
1. customer, client, account, who buys -> CUSTOMERS
2. product, sku, item, catalog, volume -> PRODUCTS
3. margin, profit, financial, cost, expense -> FINANCIAL
4. report, brief, overview, summary, everything, full picture -> REPORT
5. sales, revenue, order, deal, region, selling, bookings -> SALES
6. nothing matched, or greeting/chitchat -> GENERAL

Examples:
"top customers by revenue?" -> CUSTOMERS   (customer found at step 1)
"which products sell the most?" -> PRODUCTS
"average margin last month?" -> FINANCIAL
"write a full overview" -> REPORT
"our sales by region?" -> SALES
"good morning!" -> GENERAL

Reply with ONLY the token.

Question: {{inputs.n1.text}}
Reply:"""

SYNTHESIS_PROMPT = """You are the Business Manager's chief of staff. Merge the relevant sections into ONE crisp executive brief written for a business manager.
AUTHORITATIVE RULE: The KPI snapshot section is the single source of truth for company-wide totals. Specialist sections add detail only and must NEVER contradict the KPI snapshot.
If a section is empty or absent, skip it — do not invent content.

KPI SNAPSHOT:
{{inputs.n12.text}}

SALES:
{{inputs.n5.text}}

CUSTOMERS:
{{inputs.n8.text}}

PRODUCTS:
{{inputs.n10.text}}

FINANCIAL:
{{inputs.n14.text}}

REPORT:
{{inputs.n16.text}}

GENERAL:
{{inputs.n11.text}}

Write the final brief in markdown with short sections and bullet points."""


def node(nid, ntype, label, config, x, y):
    return {"id": nid, "type": ntype, "position": {"x": x, "y": y},
            "data": {"label": label, "config": config}}


def edge(eid, src, tgt, sh="source-main", th="target-main"):
    return {"id": eid, "source": src, "sourceHandle": sh, "target": tgt, "targetHandle": th}


def build_flow():
    # ---- stage layout: input | classifier | route | branch-data | agent | synthesis | output
    n = []
    n.append(node("n1", "trigger_chat", "Manager Question", {}, 0, 460))
    n.append(node("n2", "llm", "Intent Classifier", {"prompt": CLASSIFIER_PROMPT, "temperature": 0}, 320, 460))
    n.append(node("n12", "db_query", "KPI Snapshot", {"kb_id": KB_DEMO, "mode": "sql", "sql": SQL_KPI, "limit": 50, "continueOnFail": False}, 320, 40))
    n.append(node("n3", "switch", "Route Intent I", {
        "cases": [
            {"field": "{{inputs.n2.text}}", "op": "contains", "value": "SALES"},
            {"field": "{{inputs.n2.text}}", "op": "contains", "value": "CUSTOMERS"},
            {"field": "{{inputs.n2.text}}", "op": "contains", "value": "PRODUCTS"},
        ], "fallback_label": "more intents"}, 640, 460))
    n.append(node("n6", "switch", "Route Intent II", {
        "cases": [
            {"field": "{{inputs.n2.text}}", "op": "contains", "value": "FINANCIAL"},
            {"field": "{{inputs.n2.text}}", "op": "contains", "value": "REPORT"},
        ], "fallback_label": "general"}, 640, 820))

    # ---- branch data (deterministic SQL) ----
    n.append(node("n4", "db_query", "Sales Detail", {"kb_id": KB_DEMO, "mode": "sql", "sql": SQL_SALES, "limit": 50, "continueOnFail": True}, 960, 40))
    n.append(node("n7", "db_query", "Customer Detail", {"kb_id": KB_DEMO, "mode": "sql", "sql": SQL_CUSTOMERS, "limit": 50, "continueOnFail": True}, 960, 240))
    n.append(node("n9", "db_query", "Product Detail", {"kb_id": KB_DEMO, "mode": "sql", "sql": SQL_PRODUCTS, "limit": 50, "continueOnFail": True}, 960, 440))
    n.append(node("n13", "db_query", "Financial Detail", {"kb_id": KB_DEMO, "mode": "sql", "sql": SQL_FINANCIAL, "limit": 50, "continueOnFail": True}, 960, 640))
    n.append(node("n15", "db_query", "Report Data", {"kb_id": KB_DEMO, "mode": "sql", "sql": SQL_REPORT, "limit": 50, "continueOnFail": True}, 960, 840))

    # ---- specialist agents (each grounded with its branch data) ----
    n.append(node("n5", "agent", "Sales Manager", {"agent_id": AGENTS["sales"], "input_template": "User request: {{inputs.n1.text}}\n\nSales data by region:\n{{inputs.n4.text}}\n\nAnswer the manager's SALES question using ONLY this data. Never invent numbers — if data is missing say so."}, 1280, 40))
    n.append(node("n8", "agent", "Customer Specialist", {"agent_id": AGENTS["customers"], "input_template": "User request: {{inputs.n1.text}}\n\nCustomer revenue ranking:\n{{inputs.n7.text}}\n\nAnswer the manager's CUSTOMER question using ONLY this data. Never invent numbers."}, 1280, 240))
    n.append(node("n10", "agent", "Product Specialist", {"agent_id": AGENTS["ops"], "input_template": "User request: {{inputs.n1.text}}\n\nProduct volume data:\n{{inputs.n9.text}}\n\nAnswer the manager's PRODUCT question using ONLY this data. Never invent numbers."}, 1280, 440))
    n.append(node("n14", "agent", "Finance Specialist", {"agent_id": AGENTS["finance"], "input_template": "User request: {{inputs.n1.text}}\n\nFinancial data by region:\n{{inputs.n13.text}}\n\nAnswer the manager's FINANCIAL question using ONLY this data. Never invent numbers — if true profit/margin is not derivable, say so explicitly."}, 1280, 640))
    n.append(node("n16", "agent", "Report Writer", {"agent_id": AGENTS["writer"], "input_template": "User request: {{inputs.n1.text}}\n\nCompany overview data:\n{{inputs.n15.text}}\n\nWrite the manager's REPORT using ONLY this data. Never invent figures."}, 1280, 840))
    n.append(node("n11", "agent", "General Assistant", {"agent_id": AGENTS["general"], "input_template": "{{inputs.n1.text}}"}, 1280, 1040))

    # ---- synthesis + output ----
    n.append(node("n17", "llm", "Manager Synthesis", {"prompt": SYNTHESIS_PROMPT, "temperature": 0.3}, 1600, 460))
    n.append(node("n18", "output", "Manager Brief", {"format": "markdown"}, 1920, 460))

    e = []
    e.append(edge("e1", "n1", "n2"))
    e.append(edge("e2", "n1", "n12"))
    e.append(edge("e3", "n2", "n3"))
    # switch I: SALES->n4, CUSTOMERS->n7, PRODUCTS->n9, default->n6
    e.append(edge("e4", "n3", "n4"))
    e.append(edge("e5", "n3", "n7", "source-main-1"))
    e.append(edge("e6", "n3", "n9", "source-main-2"))
    e.append(edge("e7", "n3", "n6", "source-main-3"))
    # switch II: FINANCIAL->n13, REPORT->n15, default->n11
    e.append(edge("e8", "n6", "n13"))
    e.append(edge("e9", "n6", "n15", "source-main-1"))
    e.append(edge("e10", "n6", "n11", "source-main-3"))
    # data -> agent
    e.append(edge("e11", "n4", "n5"))
    e.append(edge("e12", "n7", "n8"))
    e.append(edge("e13", "n9", "n10"))
    e.append(edge("e14", "n13", "n14"))
    e.append(edge("e15", "n15", "n16"))
    # agent/data -> synthesis
    e.append(edge("e16", "n5", "n17"))
    e.append(edge("e17", "n8", "n17"))
    e.append(edge("e18", "n10", "n17"))
    e.append(edge("e19", "n14", "n17"))
    e.append(edge("e20", "n16", "n17"))
    e.append(edge("e21", "n11", "n17"))
    e.append(edge("e22", "n12", "n17"))
    # synthesis -> output
    e.append(edge("e23", "n17", "n18"))
    return {"version": 1, "nodes": n, "edges": e}


def api(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Authorization", "Bearer " + TOKEN)
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    else:
        data = None
    try:
        with urllib.request.urlopen(req, data=data, timeout=300) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read().decode()[:800]}


def main():
    flow = build_flow()
    # positions unique check
    pos = [(x["position"]["x"], x["position"]["y"]) for x in flow["nodes"]]
    assert len(set(pos)) == len(pos), "duplicate positions"
    # create
    wf = api("POST", "/workflows")
    if "_http_error" in wf:
        print("CREATE FAIL", wf); return 1
    wid = wf["id"]
    print("created:", wid)
    # save
    saved = api("PUT", f"/workflows/{wid}", {"name": "Business Manager Command Center",
                                             "description": "Agentic business-manager workflow: orchestrator classifies the manager's request, routes to specialist agents (sales/customers/products/financial/report/general), each grounded by deterministic SQL on the demo_e2e warehouse, then synthesizes one executive brief.",
                                             "flow": flow})
    if "_http_error" in saved:
        print("SAVE FAIL", saved); return 1
    # validate
    val = api("POST", f"/workflows/{wid}/validate")
    print("validate:", json.dumps(val, ensure_ascii=False)[:400])
    if val.get("valid") is not True:
        print("VALIDATION ERRORS"); return 1
    # publish
    pub = api("POST", f"/workflows/{wid}/publish")
    if "_http_error" in pub:
        print("PUBLISH FAIL", pub); return 1
    print("published:", pub.get("status"))
    print("WORKFLOW_ID=" + wid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
