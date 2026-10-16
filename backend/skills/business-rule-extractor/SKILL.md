---
name: business-rule-extractor
description: Extracts business rules for a data source from its catalog metadata — decode comments (0=否,1=是), measured value dictionaries, data windows, row grain, view semantics — and stages them as [auto] business-rule guidelines that feed the NL2SQL BUSINESS RULES prompt block after human review.
category: data
tags: [nl2sql, semantic-layer, business-rules, data-quality]
trigger: When a user asks to mine/extract business rules from a database, organize data-source knowledge, or improve NL2SQL accuracy with organizational constraints.
user_invocable: true
---

# Business Rule Extractor

Mine business rules hiding in a datasource's catalog metadata and stage them
as reviewable guidelines.

## When to use

- A datasource has cryptic coded columns (status/type flags) and the user
  wants their meanings captured as rules.
- NL2SQL answers keep missing organizational conventions (data windows,
  double-counting views, wrong aggregation grain).
- After a fresh catalog index, to bootstrap the BUSINESS RULES block.

## How to run

Call the extraction endpoint for the bound data source (KB):

```
POST /api/apps/{app_id}/knowledge_bases/{kb_id}/business-rules/extract
body: {"enable": false}
```

- `enable: false` (default) — rules are staged as `[auto]` guidelines that
  are DISABLED. Report them to the user and let them enable each one via
  `PATCH /api/apps/{app_id}/knowledge_bases/{kb_id}/guidelines/{guideline_id}`
  with `{"is_enabled": true}`.
- `enable: true` — for re-syncs the user has already reviewed; do not use it
  for a first extraction.

## What it extracts (source-proven rules only)

| Source | Rule example |
|---|---|
| Column decode comments | `t_sale.FSTATUS: 0=未审核,1=已审核` |
| Measured enums | `t_order.type only ever holds: normal, return (measured dictionary)` |
| Data window (coverage probe) | `t_sale.order_date data window: 2023-01-01 ~ 2026-09-30 — questions outside it return ZERO rows` |
| Grain | `t_sale: grain = one row per fbillno — never mix grains in one SUM` |
| Views | `v_contract_execution is a VIEW — joining it to base tables double-counts` |

## Honesty contract

- Never enable an extracted rule yourself; extraction is a PROPOSAL.
- Every rule cites its source (source_comment / measured_enum /
  coverage_probe / grain / table_type).
- Re-running is idempotent: unchanged rules are skipped, changed rules are
  updated in place, nothing is duplicated.
