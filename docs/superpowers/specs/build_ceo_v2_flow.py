#!/usr/bin/env python3
"""Build CEO Command Center v2 flow JSON and PUT it to the workflow API.

Run INSIDE the backend container (has app imports) OR as a plain script that
POSTs the JSON to the API from the host. We use the API path (save_workflow
normalizes edge ids + validates).
"""
import json
import sys

KB = "b1b9145d-5b6b-4c0e-ba82-919dde4620d7"
REPORT_AGENT = "ae8b9b1e-caa5-4548-bd0f-c03c5a28b977"
GENERAL_AGENT = "81dcaadb-ff96-427a-9ffc-1d4466b39789"

CLASSIFIER_PROMPT = """You are the CEO orchestrator for an enterprise command center. Classify the CEO's question into exactly one category and reply with ONLY the category token (no punctuation, no explanation, no markdown):
SALES — revenue, sales amount, orders, order volume, quantities, sales trends
CUSTOMERS — customer revenue, top customers, customer ranking, concentration, customer accounts
PRODUCT — products, materials, product mix, product ranking, material performance
INVENTORY — inventory, stock, backlog, undelivered, remaining quantities
FINANCIAL — tax, margins, profit, average order value, prices, financial KPIs
REPORT — weekly report, monthly report, sales report, generate a report
GENERAL — anything else
Question: {{inputs.n1.text}}"""

TREND_ANALYST_PROMPT = """You are the CEO's trend analyst. Using ONLY the two tables below, compute week-over-week and month-over-month changes, identify the strongest and weakest periods, and flag anomalies (drops or spikes greater than ~30%). If a change cannot be computed from the data, say so. Never invent numbers.

8-week trend:
{{inputs.n14.text}}

12-month trend:
{{inputs.n15.text}}

Reply with a concise trend analysis with exact figures."""

SYNTHESIS_PROMPT = """You are the CEO's chief of staff. Merge all sections into ONE executive brief.
AUTHORITATIVE RULE: The KPI snapshot and Top Rankings nodes are the single source of truth for company-wide totals and rankings. Branch sections add detail only and must NEVER contradict the snapshot; if they differ, trust the snapshot.
Sections:
[KPI Snapshot] {{inputs.n12.text}}
[Top Rankings] {{inputs.n13.text}}
[Trend Analysis] {{inputs.n16.text}}
[Sales] {{inputs.n4.text}}
[Customers] {{inputs.n5.text}}
[Products] {{inputs.n7.text}}
[Inventory] {{inputs.n8.text}}
[Financial] {{inputs.n9.text}}
[Report] {{inputs.n10.text}}
[General] {{inputs.n11.text}}
Output a CEO-grade brief with these sections: Verdict, Key Numbers (grounded in the snapshot), Trend, Risks & Concentration, Recommended Actions. Only include sections with content; empty sections render as blank. Every number must come from the section data above — never invent figures."""

KPI_SQL = """SELECT (SELECT COUNT(DISTINCT FID) FROM erp_v_sale_orderentry) AS total_orders, (SELECT COUNT(DISTINCT FCUSTID) FROM erp_v_sale_orderentry) AS total_customers, (SELECT COUNT(DISTINCT material_name) FROM erp_v_sale_orderentry) AS total_materials, (SELECT ROUND(SUM(FQTY_ORIGIN),2) FROM erp_v_sale_orderentry) AS total_qty, (SELECT ROUND(SUM(FALLAMOUNT),2) FROM erp_v_sale_orderentry) AS total_amount, (SELECT ROUND(SUM(FAMOUNT),2) FROM erp_v_sale_orderentry) AS pretax_amount, (SELECT ROUND(SUM(FTAXAMOUNT),2) FROM erp_v_sale_orderentry) AS tax_amount, (SELECT ROUND(SUM(FREMAINOUTQTY),2) FROM erp_v_sale_orderentry) AS undelivered_qty, (SELECT ROUND(SUM(FALLAMOUNT)/COUNT(DISTINCT FID),2) FROM erp_v_sale_orderentry) AS avg_order_value"""

TOP3_SQL = """SELECT org_name, CUST_NAME, material_name, ROUND(SUM(FALLAMOUNT),2) AS amount, ROUND(SUM(FQTY_ORIGIN),2) AS qty FROM erp_v_sale_orderentry GROUP BY org_name, CUST_NAME, material_name ORDER BY amount DESC LIMIT 3"""

WK_SQL = """SELECT DATE_FORMAT(PLANDATE, '%x-W%v') AS week, COUNT(DISTINCT FID) AS orders, COUNT(DISTINCT FCUSTID) AS customers, ROUND(SUM(FQTY_ORIGIN), 2) AS qty, ROUND(SUM(FALLAMOUNT), 2) AS amount FROM erp_v_sale_orderentry WHERE PLANDATE >= DATE_SUB(CURDATE(), INTERVAL 8 WEEK) GROUP BY DATE_FORMAT(PLANDATE, '%x-W%v') ORDER BY week"""

MO_SQL = """SELECT DATE_FORMAT(PLANDATE, '%Y-%m') AS month, COUNT(DISTINCT FID) AS orders, ROUND(SUM(FQTY_ORIGIN),2) AS qty, ROUND(SUM(FALLAMOUNT),2) AS amount FROM erp_v_sale_orderentry WHERE PLANDATE >= DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 12 MONTH), '%Y-%m-01') GROUP BY DATE_FORMAT(PLANDATE, '%Y-%m') ORDER BY month"""


def node(nid, ntype, config, label):
    return {
        "id": nid, "type": ntype,
        "position": {"x": 0, "y": 0},
        "data": {"label": label, "config": config},
    }


def nl_branch(nid, label, hint):
    return node(nid, "db_query", {
        "kb_id": KB, "mode": "nl", "question": hint,
        "limit": 50, "retryOnFail": 0, "continueOnFail": True,
    }, label)


def sql_node(nid, label, sql, limit=50):
    return node(nid, "db_query", {
        "kb_id": KB, "mode": "sql", "sql": sql, "question": "",
        "limit": limit, "retryOnFail": 0, "continueOnFail": False,
    }, label)


nodes = [
    node("n1", "trigger_chat", {}, "CEO Question"),
    node("n2", "llm", {"prompt": CLASSIFIER_PROMPT, "temperature": 0}, "Intent Classifier"),
    node("n3", "switch", {
        "cases": [
            {"field": "{{inputs.n2.text}}", "op": "contains", "value": "SALES"},
            {"field": "{{inputs.n2.text}}", "op": "contains", "value": "CUSTOMERS"},
            {"field": "{{inputs.n2.text}}", "op": "contains", "value": "PRODUCT"},
        ],
        "fallback_label": "more intents",
    }, "Route Intent I"),
    nl_branch("n4", "Sales Detail", "{{inputs.n1.text}} — Answer using the ERP sales order view erp_v_sale_orderentry. Report revenue, quantities, orders and date-bucketed trends where asked. Columns available: CUST_NAME, material_name, org_name, FQTY_ORIGIN, FDELIQTY, FREMAINOUTQTY, FAMOUNT, FALLAMOUNT, FTAXAMOUNT, PLANDATE. Do NOT compute company-wide totals — the KPI snapshot node is the single source of truth for totals; add detail only."),
    nl_branch("n5", "Customer Revenue Detail", "{{inputs.n1.text}} — Focus on CUSTOMER REVENUE: group by CUST_NAME, report revenue (FALLAMOUNT), quantity, ranking, customer count and concentration (share of total). Use the ERP sales order view erp_v_sale_orderentry. Detail only — company-wide totals come from the KPI snapshot node."),
    node("n6", "switch", {
        "cases": [
            {"field": "{{inputs.n2.text}}", "op": "contains", "value": "INVENTORY"},
            {"field": "{{inputs.n2.text}}", "op": "contains", "value": "FINANCIAL"},
            {"field": "{{inputs.n2.text}}", "op": "contains", "value": "REPORT"},
        ],
        "fallback_label": "general",
    }, "Route Intent II"),
    nl_branch("n7", "Product Detail", "{{inputs.n1.text}} — Focus on PRODUCTS: group by material_name, report revenue (FALLAMOUNT), quantity, ranking, share and unit prices (FPRICE). Use the ERP sales order view erp_v_sale_orderentry. Detail only — company-wide totals come from the KPI snapshot node."),
    nl_branch("n8", "Inventory / Backlog Detail", "{{inputs.n1.text}} — Focus on INVENTORY and BACKLOG: report undelivered/remaining quantities using FREMAINOUTQTY, delivered FDELIQTY, ordered FQTY_ORIGIN, and the inventory-context columns instock_qty / produce_qty / plan_qty where present, grouped by material_name or CUST_NAME. Use the ERP sales order view erp_v_sale_orderentry. Detail only — totals come from the KPI snapshot node."),
    nl_branch("n9", "Financial Detail", "{{inputs.n1.text}} — Focus on FINANCIAL KPIs from erp_v_sale_orderentry: tax (FTAXAMOUNT), pre-tax amount (FAMOUNT), tax-inclusive amount (FALLAMOUNT), average order value (FALLAMOUNT per distinct FID), price per ton (FALLAMOUNT / FQTY_ORIGIN), tax rate (FTAXRATE). If a metric (e.g. true profit or margin) is not derivable from these columns, say so explicitly — never invent figures. Detail only — totals come from the KPI snapshot node."),
    node("n10", "agent", {
        "agent_id": REPORT_AGENT,
        "input_template": "User request: {{inputs.n1.text}}\n\nWeekly sales data (ERP, latest 8 weeks):\n{{inputs.n14.text}}\n\nMonthly trend (12 months):\n{{inputs.n15.text}}\n\nWrite the weekly sales report using ONLY this data.",
    }, "Weekly Report Writer"),
    node("n11", "agent", {"agent_id": GENERAL_AGENT, "input_template": "{{inputs.n1.text}}"}, "General Assistant"),
    sql_node("n12", "KPI Snapshot", KPI_SQL),
    sql_node("n13", "Top Rankings", TOP3_SQL),
    sql_node("n14", "8-Week Trend", WK_SQL),
    sql_node("n15", "12-Month Trend", MO_SQL),
    node("n16", "llm", {"prompt": TREND_ANALYST_PROMPT, "temperature": 0.2}, "Trend & Anomaly Analyst"),
    node("n17", "llm", {"prompt": SYNTHESIS_PROMPT, "temperature": 0.3}, "CEO Synthesis"),
    node("n18", "output", {"format": "markdown"}, "CEO Brief"),
]

edges = [
    ("n1", "n2", "source-main", "target-main"),
    ("n1", "n12", "source-main", "target-main"),
    ("n1", "n13", "source-main", "target-main"),
    ("n1", "n14", "source-main", "target-main"),
    ("n1", "n15", "source-main", "target-main"),
    ("n2", "n3", "source-main", "target-main"),
    ("n3", "n4", "source-main", "target-main"),
    ("n3", "n5", "source-main-1", "target-main"),
    ("n3", "n7", "source-main-2", "target-main"),
    ("n3", "n6", "source-main-3", "target-main"),
    ("n6", "n8", "source-main", "target-main"),
    ("n6", "n9", "source-main-1", "target-main"),
    ("n6", "n10", "source-main-2", "target-main"),
    ("n6", "n11", "source-main-3", "target-main"),
    ("n4", "n17", "source-main", "target-main"),
    ("n5", "n17", "source-main", "target-main"),
    ("n7", "n17", "source-main", "target-main"),
    ("n8", "n17", "source-main", "target-main"),
    ("n9", "n17", "source-main", "target-main"),
    ("n10", "n17", "source-main", "target-main"),
    ("n11", "n17", "source-main", "target-main"),
    ("n12", "n17", "source-main", "target-main"),
    ("n13", "n17", "source-main", "target-main"),
    ("n14", "n16", "source-main", "target-main"),
    ("n15", "n16", "source-main", "target-main"),
    ("n16", "n17", "source-main", "target-main"),
    ("n17", "n18", "source-main", "target-main"),
]

flow = {
    "version": 1,
    "settings": {},
    "nodes": nodes,
    "edges": [
        {"id": f"e{i+1}", "source": s, "target": t, "sourceHandle": sh, "targetHandle": th}
        for i, (s, t, sh, th) in enumerate(edges)
    ],
}

if __name__ == "__main__":
    with open("/tmp/ceo_cc_v2_flow.json", "w") as f:
        json.dump(flow, f, ensure_ascii=False, indent=1)
    print("nodes:", len(nodes), "edges:", len(edges))
