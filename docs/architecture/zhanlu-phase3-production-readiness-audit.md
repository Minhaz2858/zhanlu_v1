# Zhanlu Phase 3 — Production-Readiness Audit (2026-10-31)

Investigation-first audit per mission Part A. Evidence classification follows
the three-state rule: implemented ≠ tested ≠ production-ready.

## 1. Gap Matrix

| Area | Existing implementation | Verified gaps | Proposed changes (→ done in Phase 3) | Priority |
|---|---|---|---|---|
| Durable plan/task state | `PlanNode` (DB-backed, status/result/seq), `ObservationRecord` (JSON result_data), `AgentRunStep` checkpoints, `Execution` tree — all durable (audit + §16 report) | No resume-eligibility decision layer; no idempotency keys; no stale-task TTL; ObservationRecord lacked evidence refs | `durable_execution.py` (resume decisions + idempotency keys + stale TTL); migration 115 `observation_records.evidence_ids`; `_extract_evidence_ids` persisted in capability_router | P0 |
| Crash recovery | `recover_orphaned_invocations` scheduled (running>1h → failed+recovery, never completed, idempotent) | No node-level recovery ownership decision | resume_decision layers ON TOP of orphan recovery (no rebuild) | P0 |
| Agent discovery | `_list_available_agents` (published-only), `AgentPortfolioEntry` profile, Phase-2 lexical fallback | Lexical-only matching (paraphrase-blind); no capability profile reuse in selection | `semantic_capability_match.py` (local embeddings, published+department prefilter, threshold, lexical degrade) | P1 |
| Multilingual | SSE templates bilingual; classify_failure English-only | Chinese error strings → `unknown_error`; CJK keywords unmatched | zh markers merged into `_MARKERS`; `localized_recovery_hint`; CJK bigram tokenizer | P1 |
| Evaluation | golden-eval runner exists | No platform-level reliability metrics; provider state unverified | `reliability_metrics.py` + LIVE probe (see §3) | P1 |
| Observability | AgentInvocation/AgentRunStep/PolicyDecision/ApprovalRequest/CostLedger all org-scoped; `/governance/cost` point-in-time | No org-level operational dashboard; no tool-failure distribution; no per-agent rollup | `usage_analytics.py` + `/api/usage/*` admin endpoints + AdminConsole Usage tab | P1 |
| Schedulers/queues | BackgroundScheduler + scheduled_tasks; durable queue deferred (Phase 2 decision) | — | unchanged (no second queue) | — |
| Security | org isolation wall (TimestampedBase.org_id), published-only discovery, approval gates, delegation depth/cycle guards | Recovery path could theoretically re-drive approval-gated/mutating work | explicit tests: recovery never bypasses approval; fallback never selects unpublished; department wall honored | P0 |

Classifications: **Already implemented** — invocation lifecycle, orphan
recovery, checkpoints, execution tree, governance store, cost service,
evidence registry. **Partially implemented** — capability discovery (lexical
only), failure classification (English-only), observability (point-in-time
only). **Missing** — resume decision layer, idempotency keys, semantic
matching, reliability metrics, org dashboard. **Implemented but not
verified** — live multi-agent E2E (blocked). **Blocked by infrastructure** —
live evaluation (see §3).

## 2. Existing mechanisms reused (nothing rebuilt)

- Durable state: PlanNode + ObservationRecord + AgentRunStep + Execution (unchanged; one additive column).
- Orphan recovery: `recover_orphaned_invocations` + BackgroundScheduler (resume_decision composes with it).
- Embeddings: `memory_advanced.embeddings.get_embeddings_batch` (LOCAL, cached).
- Lexical matcher: Phase-2 `capability_fallback.select_alternatives` (degrade path inside semantic matching).
- Telemetry: AgentInvocation/AgentRunStep/PolicyDecision/ApprovalRequest (aggregated, never duplicated).
- Reviewer + evidence registry + cost service + governance store: unchanged.

## 3. Provider state (LIVE evidence, 2026-10-31)

One out-of-band probe (single 1-token call):

```json
{"configured": true, "available": false, "probed": true,
 "unavailable_reason": "HTTP 401: ... invalid_api_key (help.aliyun.com/zh/model-studio)"}
```

**The configured provider key is INVALID (HTTP 401), not merely quota-exhausted.**
Phase-3.4 live multi-agent evaluation therefore remains **BLOCKED**. The
measurement framework (`reliability_metrics.platform_reliability_report`) is
implemented and integration-tested so live runs can begin the moment a funded,
valid key is configured. No API keys are exposed anywhere in payloads or logs.

## 4. Deferred work

- Durable queue + webhooks (Phase 2 decision stands — separate phase).
- Node-level in-place crash-resume of partially-executed Plans (resume_decision
  provides the decision layer; wiring an automatic resumer into the scheduler is
  future work — today recovery surfaces decisions to the orchestrator/scheduler
  honestly rather than fabricating continuation).
- Historical-success-based capability matching (needs reliable outcome data volume).
- Cost anomaly detection (needs live token data volume).
