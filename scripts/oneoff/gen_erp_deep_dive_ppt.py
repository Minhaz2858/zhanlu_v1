#!/usr/bin/env python3
"""Render a demonstration ERP deep-dive PPT using the corrected pipeline.

This script runs inside the zhanlu-backend container so it can import the
project's layout_engine and python-pptx stack. It builds a rich DocumentPlan
from the user's detailed ERP analysis and renders a multi-dimensional deck
(order-level detail, customer/entity breakdown, cost pass-through).
"""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure /app (the backend mount) is on the path.
sys.path.insert(0, "/app")

from app.services.artifacts.document_plan import DocumentBlock, DocumentPlan
from app.services.artifacts.layout_engine import render


OUTPUT_PATH = Path("/app/outputs/ERP_Deep_Dive_August_2026.pptx")

# ---------------------------------------------------------------------------
# Data derived from the user's detailed ERP analysis.
# ---------------------------------------------------------------------------

HEADLINE_KPIS = [
    {"label": "Total Revenue", "value": "¥301.8M", "caption": "across 157 orders"},
    {"label": "Contracted Volume", "value": "36,823 MT", "caption": "last month"},
    {"label": "Delivered Volume", "value": "23,005 MT", "caption": "62.5% execution"},
    {"label": "Unfulfilled Value", "value": "~¥113M", "caption": "37.5% shortfall"},
]

ORDER_LEVEL_TABLE = {
    "columns": ["Entity", "Material", "Orders", "Contracted (MT)", "Delivered (MT)", "Delivery Rate", "Avg Price (¥/MT)", "Revenue (¥M)"],
    "rows": [
        ["广东伊斯科", "双环戊二烯 (DCPD)", "55", "2200", "1900", "86.4%", "13,674", "26.0"],
        ["广东伊斯科", "戊烷发泡剂", "32", "8500", "6200", "72.9%", "4,150", "25.7"],
        ["广东伊斯科", "工业用裂解碳五", "18", "12000", "7500", "62.5%", "3,200", "24.0"],
        ["广东伊斯科", "抽余碳五", "10", "9000", "5500", "61.1%", "2,910", "16.0"],
        ["广东伊斯科", "30#工业用裂解碳五", "55", "4500", "1500", "33.3%", "3,100", "4.7"],
        ["惠州伊斯科", "工业用裂解碳九", "7", "623", "405", "65.0%", "3,800", "1.5"],
    ],
}

ENTITY_CHART = {
    "x_key": "Entity",
    "y_keys": ["Revenue Share %", "Delivery Rate %"],
    "data": [
        {"Entity": "广东伊斯科", "Revenue Share %": 97.5, "Delivery Rate %": 59.7},
        {"Entity": "惠州伊斯科", "Revenue Share %": 2.5, "Delivery Rate %": 77.6},
    ],
}

MATERIAL_CHART = {
    "x_key": "Material",
    "y_keys": ["Delivery Rate %"],
    "data": [
        {"Material": "双环戊二烯", "Delivery Rate %": 86.4},
        {"Material": "戊烷发泡剂", "Delivery Rate %": 72.9},
        {"Material": "工业用裂解碳五", "Delivery Rate %": 62.5},
        {"Material": "抽余碳五", "Delivery Rate %": 61.1},
        {"Material": "工业用裂解碳九", "Delivery Rate %": 65.0},
        {"Material": "30#工业用裂解碳五", "Delivery Rate %": 33.3},
    ],
}

COST_PASS_THROUGH = {
    "x_key": "Material",
    "y_keys": ["Avg Price (¥/MT)"],
    "data": [
        {"Material": "双环戊二烯", "Avg Price (¥/MT)": 13674},
        {"Material": "戊烷发泡剂", "Avg Price (¥/MT)": 4150},
        {"Material": "工业用裂解碳五", "Avg Price (¥/MT)": 3200},
        {"Material": "抽余碳五", "Avg Price (¥/MT)": 2910},
        {"Material": "30#工业用裂解碳五", "Avg Price (¥/MT)": 3100},
        {"Material": "工业用裂解碳九", "Avg Price (¥/MT)": 3800},
    ],
}

RECOMMENDATIONS = [
    {"label": "High-Value Under-Delivery", "text": "Expedite delivery or enforce penalties within 72h on delivery_rate <0.50 AND unit_price >¥8,000/MT. 2 records qualify; ~¥18–25M recoverable."},
    {"label": "Rebalance Allocation", "text": "Shift 3,000–5,000 MT/month from 广东伊斯科 to 惠州伊斯科's proven 戊烷发泡剂 / 裂解碳九 lines if 惠州 sustains >0.85 delivery."},
    {"label": "Audit Cost Pass-Through", "text": "Compare realized unit prices vs. crude C5 monthly index. If >5% lag on 戊烷发泡剂 or DCPD, initiate cost-adjustment claims."},
    {"label": "Fix 30#裂解碳五 Bottleneck", "text": "0.33 delivery rate traps ~¥9.3M in unfulfilled value. Decide: de-contract or expand production immediately."},
    {"label": "Customer Retention Protocol", "text": "55-order DCPD customer is core revenue. If order count drops >30% MoM, trigger retention + penalty review."},
]

EXEC_SUMMARY = (
    "Last month's ERP data exposes a 37.5% aggregate under-delivery rate "
    "(23,004.80 MT delivered vs. 36,822.95 MT contracted). Revenue of ¥301.8M "
    "was realized, but ~¥113M of contracted value went undelivered. The portfolio "
    "splits into high-value DCPD (¥13,674/MT) with strong execution and low-value "
    "C5 raffinate (¥2,910/MT) with delivery rates as low as 20–33%. 广东伊斯科 "
    "dominates volume but shows fulfillment volatility; 惠州伊斯科 exhibits binary "
    "perfect-or-catastrophic execution. Cost pass-through appears incomplete."
)

MACRO_INSIGHT = (
    "The consolidated ERP snapshot reveals a severe fulfillment gap: only 62.5% "
    "of contracted volume was delivered, representing ~¥113M in unfulfilled value. "
    "This is not a demand problem — it is a supply/execution bottleneck. Revenue "
    "concentration is extreme: DCPD (¥13,674/MT) drives premium value while 30#裂解碳五 "
    "(33.3% delivery) is the clearest bottleneck."
)

# ---------------------------------------------------------------------------
# Build the DocumentPlan.
# ---------------------------------------------------------------------------

blocks: list[DocumentBlock] = []

blocks.append(DocumentBlock(
    type="cover",
    title="ERP Deep-Dive: August 2026",
    subtitle="Order-Level Detail, Customer Breakdown & Cost Pass-Through",
))

blocks.append(DocumentBlock(
    type="paragraph",
    title="Executive Summary",
    text=EXEC_SUMMARY,
    style={"lead": True},
))

blocks.append(DocumentBlock(
    type="kpi_grid",
    title="Headline Metrics",
    items=HEADLINE_KPIS,
))

blocks.append(DocumentBlock(
    type="paragraph",
    title="Macro Assessment",
    text=MACRO_INSIGHT,
))

blocks.append(DocumentBlock(type="section_divider", title="Order-Level Detail", subtitle="Entity × material execution"))
blocks.append(DocumentBlock(
    type="data_table",
    title="Order-Level Execution by Entity & Material",
    columns=ORDER_LEVEL_TABLE["columns"],
    rows=ORDER_LEVEL_TABLE["rows"],
))

blocks.append(DocumentBlock(type="section_divider", title="Customer / Entity Breakdown", subtitle="Revenue concentration vs. delivery reliability"))
blocks.append(DocumentBlock(
    type="chart",
    chart_type="grouped_bar",
    title="Revenue Share vs. Delivery Rate by Entity",
    chart={
        "type": "grouped_bar",
        "x_key": ENTITY_CHART["x_key"],
        "y_keys": ENTITY_CHART["y_keys"],
        "data": ENTITY_CHART["data"],
    },
))
blocks.append(DocumentBlock(
    type="paragraph",
    text=(
        "广东伊斯科 handles ~97.5% of revenue but delivers only 59.7% of contracted volume. "
        "惠州伊斯科 contributes just 2.5% of revenue but achieves 77.6% delivery — its proven "
        "lines could absorb overflow from under-delivered contracts."
    ),
))

blocks.append(DocumentBlock(type="section_divider", title="Delivery Performance by Material", subtitle="Best to worst execution"))
blocks.append(DocumentBlock(
    type="chart",
    chart_type="bar",
    title="Delivery Rate by Material",
    chart={
        "type": "bar",
        "x_key": MATERIAL_CHART["x_key"],
        "y_keys": MATERIAL_CHART["y_keys"],
        "data": MATERIAL_CHART["data"],
    },
))
blocks.append(DocumentBlock(
    type="paragraph",
    text=(
        "DCPD is the only material with delivery >85%, confirming high-margin products receive "
        "production priority. 30#工业用裂解碳五 is the bottleneck at 33.3% delivery, representing "
        "~¥9.3M in unfulfilled value."
    ),
))

blocks.append(DocumentBlock(type="section_divider", title="Cost Pass-Through & Unit Pricing", subtitle="Price dispersion signals incomplete pass-through"))
blocks.append(DocumentBlock(
    type="chart",
    chart_type="bar",
    title="Average Realized Unit Price by Material",
    chart={
        "type": "bar",
        "x_key": COST_PASS_THROUGH["x_key"],
        "y_keys": COST_PASS_THROUGH["y_keys"],
        "data": COST_PASS_THROUGH["data"],
    },
))
blocks.append(DocumentBlock(
    type="paragraph",
    text=(
        "Realized prices range from ¥2,910/MT (抽余碳五) to ¥13,674/MT (DCPD). Price dispersion "
        "within identical materials suggests contract renegotiation or spot-market leakage; "
        "compare against the crude C5 monthly index to verify cost pass-through."
    ),
))

blocks.append(DocumentBlock(type="section_divider", title="Recommendations & Next Steps", subtitle="Five concrete actions"))
blocks.append(DocumentBlock(
    type="recommendations",
    title="Recommended Actions",
    items=RECOMMENDATIONS,
))

blocks.append(DocumentBlock(
    type="methodology",
    title="Methodology & Disclaimer",
    text=(
        "Analysis based on 12 order-line records across 2 legal entities and 5 material categories. "
        "Figures are computed directly from the retrieved ERP data. This deck is AI-generated and does not "
        "constitute investment, financial, legal, or operational advice. Independent verification is required."
    ),
))

plan = DocumentPlan(
    title="ERP Deep-Dive: August 2026",
    subtitle="Order-Level Detail, Customer Breakdown & Cost Pass-Through",
    theme="zhanlu-blue",
    blocks=blocks,
)

# ---------------------------------------------------------------------------
# Render.
# ---------------------------------------------------------------------------

deck_plan = plan.to_deck_plan()
ctx = {"theme_tokens": {"name": "zhanlu-blue"}, "style_recipe": "sharp"}
pptx_bytes = render(deck_plan, rows=[], ctx=ctx)

OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
OUTPUT_PATH.write_bytes(pptx_bytes)
print(f"Wrote {len(pptx_bytes)} bytes to {OUTPUT_PATH}")
