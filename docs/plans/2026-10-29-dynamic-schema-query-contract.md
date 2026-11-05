# Dynamic-Schema Query Semantics — Per-Turn Query Contract
**Date:** 2026-10-29 · **Status:** design for approval · **Scope:** NL2SQL correctness for ANY connected database

## Problem (evidence: conv e70fa696, 2026-10-29)

One turn = 11 independent NL2SQL nodes. Each node independently interpreted the
dynamic schema and picked **different amount columns** (`FAMOUNT` for the product
ranking, `FALLAMOUNT` for the headline total). The synthesized report blended
figures from two measures that do not reconcile (¥105.90M headline vs a product
table summing to ¥93.87M-basis) — the model disclosed it in fine print, but the
report was internally inconsistent. Same turn took 483s.

Root cause class: **per-node schema re-interpretation on dynamic databases.**
The system supports arbitrary user-connected databases, so there is no hardcoded
schema truth to fall back on — each node guesses, and different nodes guess
differently.

## Design principle

**Zero schema knowledge in code.** The engine never names a column. All
semantics come from runtime artifacts built per connected datasource. Works for
database #1, #2, … #N, any engine, any tenant.

## Components

### A. DatasourceProfile (cached per datasource_id)
Built once at connect time (and refreshable):
- tables + columns via the connection layer's existing metadata discovery
- per column: inferred role `measure | date | dimension | id | text` from
  **generic statistics** — type, sample values, null rate, distinct count,
  numeric range plausibility (money-like vs count-like) — no name assumptions
  (names are a secondary signal, see B)
- stored in the datasource catalog; invalidated on re-connect/schema change

### B. QueryContract (resolved ONCE per turn, before decomposition)
- concept lexicon (small, generic, multilingual): revenue/amount/sales/金额…,
  date/日期…, quantity/数量… — a constant table, NOT per-database
- deterministic scorer: lexicon similarity of column names (fuzzy, bilingual)
  **+ profile statistics** (measure-role, coverage, range) → binding
  `{concept → table.column}` + date grain for the asked period
- ambiguity rule: if two candidates score within threshold → ASK THE USER to
  confirm the binding (honest, dynamic-safe); never silently pick
- binding is keyed by (datasource_id, turn) — no cross-datasource leakage;
  reconnect/schema-change → fresh resolution

### C. Binding enforcement (deterministic, per NL2SQL node)
- contract injected into every node's prompt as a mandatory constraint
- post-SQL validator: extract column references per concept from the node's
  SQL; if a bound concept uses a non-bound column → node fails validation with
  a retryable "use the contract binding" error (no LLM arbitration)

### D. Reconciliation gate (synthesis)
- fingerprint the measure expression behind each consumed result
- two different fingerprints for the same concept → re-query with the binding,
  or disclose the conflict explicitly — never blend

### E. Planner consolidation
- report-class turns capped at ~3–4 nl2sql nodes (also the latency fix)

## Implementation phases (TDD, sqlite fixtures with synthetic arbitrary schemas)

| Phase | Deliverable | Proof |
|---|---|---|
| A | DatasourceProfile builder + cache | profiler on 2 synthetic schemas with disjoint column naming infers correct roles; cache isolation per datasource_id |
| B | QueryContract resolver | same question resolves DIFFERENT bindings on 2 different schemas; tie → user-clarification; bilingual lexicon |
| C | Enforcement in FSM nodes | node using a non-bound measure column is deterministically rejected; compliant node passes |
| D | Reconciliation gate | conflicting-fingerprint results → re-query or disclosure; blending impossible |
| E | Planner cap | report turn ≤ 4 nodes |
| F | Live validation | real warehouse re-run: totals reconcile with product table; second synthetic datasource e2e |

## Non-goals
- No per-database code, no column whitelists, no schema hardcoding.
- No change to datasource connection layer (already dynamic).
- Latency gains are a by-product of E; the deep latency work (pipeline retries,
  parallel nodes) remains separate follow-up.
