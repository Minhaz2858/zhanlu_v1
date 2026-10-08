# CEO Command Center v2 — Design

Date: 2026-09-01
Status: approved (Approach A + CEO financials/customer-revenue addition)

## Goal

Upgrade the "CEO Command Center" workflow (AgentApp da71da10-082e-4b35-ad1c-f7943f32ccfe)
from 12 → 18 nodes: 7-intent routing, 5 grounded NL detail branches, 4 always-on
deterministic SQL pipelines, 2-stage analysis (trend analyst → CEO synthesis).

## Why (evidence)

- v1 KPI snapshot only ran on ERP intent; trends only on REPORT intent → most
  answers lacked authoritative totals/trend context.
- Proven pattern (db-answering-workflow-pattern.md): KPI snapshot must be
  always-on (fan-out from trigger), synthesis must carry an AUTHORITATIVE RULE.
- Switch registry caps at 3 named cases + default → 7 intents need a 2-switch
  cascade (n3 → n6).

## Node map (18)

```
n1 trigger_chat
n2 llm classifier (SALES|CUSTOMERS|PRODUCT|INVENTORY|FINANCIAL|REPORT|GENERAL, exact-case)
n3 switch I: SALES→n4, CUSTOMERS→n5, PRODUCT→n7, default→n6
n4 db_query NL Sales detail          (continueOnFail)
n5 db_query NL Customer revenue      (continueOnFail)
n6 switch II: INVENTORY→n8, FINANCIAL→n9, REPORT→n10, default→n11
n7 db_query NL Product detail        (continueOnFail)
n8 db_query NL Inventory/backlog     (continueOnFail)
n9 db_query NL Financial (tax/AOV/price-per-ton; explicit no-invent-margins) (continueOnFail)
n10 agent Weekly Report Writer (feeds from n14 + n15)
n11 agent General Assistant
n12 db_query SQL KPI Snapshot   (always-on, fan-out from n1)   ~22s
n13 db_query SQL Top Rankings   (always-on, fan-out from n1)   ~3s
n14 db_query SQL 8-Week Trend   (always-on, fan-out from n1)   ~1s
n15 db_query SQL 12-Month Trend (always-on, fan-out from n1)   ~1s
n16 llm Trend & Anomaly Analyst (inputs n14+n15; grounded deltas only)
n17 llm CEO Synthesis (AUTHORITATIVE RULE: KPI snapshot wins for totals)
n18 output markdown
```

All timed against the ERP connector (host 10.10.10.49): every node < 30s node timeout.
The 15-subselect single-node KPI (35s) was rejected; split into KPI subset + Top
Rankings single scan.

## SQL (grounded, v_sale_order)

- KPI Snapshot: totals (orders/customers/materials/qty/amount), pretax, tax
  (FTAXAMOUNT), undelivered (FREMAINOUTQTY), AOV (FALLAMOUNT / DISTINCT FID).
- Top Rankings: single GROUP BY org_name,CUST_NAME,material_name ORDER BY
  SUM(FALLAMOUNT) DESC LIMIT 3 (one scan → 3.4s).
- 8-Week: DATE_FORMAT(PLANDATE,'%x-W%v') window 8 weeks (proven v1 n12 SQL).
- 12-Month: DATE_FORMAT(PLANDATE,'%Y-%m') window 12 months.

## Guards

- NL branches continueOnFail=true — a failed branch never kills the brief.
- Classifier emits exact-case tokens; switch uses case-sensitive `contains`.
- Synthesis prompt: KPI snapshot + Top Rankings are the single source of truth;
  branches detail-only; never invent numbers.
- Financial branch explicitly refuses to invent profit/margin if columns absent.

## Verification

API runs for 7 intents; per-node step statuses (completed/skipped); outputs
cross-checked vs ground truth (¥18,410,129,637.66 / 11,855 orders / top org
示例工厂 / top customer 中国石化...华中 / top product 包装材料 / tax
¥2,109,301,218.07 / undelivered 118,630.03).

**RESULTS (2026-09-01, all 7 intents completed):**

| Intent | Time | Branch ran (others skipped) | Grounded? |
|---|---|---|---|
| SALES | 86.0s | n4 sales (raw ERP rows) | ✓ ¥18.41B / 11,855 / 736 |
| CUSTOMERS | 87.3s | n5 (top cust ¥2,558M, 13.9% share) | ✓ |
| PRODUCT | 74.5s | n7 (包装材料 ¥4,010M, 21.8%) | ✓ |
| INVENTORY | 79.0s | n8 (undelivered per material) | ✓ |
| FINANCIAL | 80.9s | n9 (tax ¥2,109M, AOV ¥1.55M, ¥6,555/ton) | ✓ exact KPI match |
| REPORT | 48.5s | n10 (weekly report W36) | ✓ |
| GENERAL | 46.3s | n11 (assistant) | ✓ |

Every run: n12/n13/n14/n15 (always-on SQL) + n16 (trend) + n17 (synthesis)
all `completed`; exactly one branch `completed`, all others `skipped`; zero
failed/retrying steps.

## Files

- docs/superpowers/specs/ceo_cc_v1_flow_backup.json — v1 flow backup
- docs/superpowers/specs/build_ceo_v2_flow.py — flow builder (reproducible)
