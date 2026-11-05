# Zhanlu Agentic Platform — Implementation Roadmap (Phases 0–7)

**Date:** 2026-10-31 · Phase 0 is COMPLETE (benchmark + audit + this plan). Sequencing follows
the mission's dependency rule; overlapping only where safe.

## Phase 0 — Research and ground-truth audit ✅ COMPLETE
- All 11 references fetched/read in full → `zhanlu-manus-chatgpt-capability-benchmark.md`.
- Full architecture audit with per-feature classification → benchmark §3 matrix.
- Gap list + prioritized recommendations R1–R9 → benchmark §4–§8.
- Architecture designs → `zhanlu-general-assistant-intelligence-architecture.md`,
  `zhanlu-hybrid-multi-agent-execution-design.md`.

## Phase 1 — GA intelligence (next up)
| Item | Design ref | Priority |
|---|---|---|
| Intent classification in plan payload (`intent_type`, `objective_statement`; schema-validated, template path unchanged) | GA-arch §2.1 | P1 |
| Plan enrichment: constraints, acceptance criteria, risk level, required approvals (map to existing gates) | GA-arch §2.2 | P1 |
| Reviewer-verdict-driven replan criteria | GA-arch §2.3 | P2 |
| Delegation-decision rationale recorded for eval | Hybrid H4 | P2 |

## Phase 2 — Adaptive execution
| Item | Priority |
|---|---|
| **Lifecycle consistency fix (Hybrid H1 / mission §23)** — truthful child terminal states, running-row at delegation start, no fabricated completion | **P1 — treat as defect (TDD)** |
| Generic tool-call healing (extend SQL-heal pattern to all tools, arg-reformulation with cap) | P2 |
| Enable post-mortem learning with sampling per project | P3 |

## Phase 3 — Hybrid collaboration
- Contract enrichment (H3), peer-collab UX parity, evidence aggregation polish. Broker/budgets
  verified; only enrichment remains.

## Phase 4 — Research, files and artifacts
- Web-research hardening (provider health checks, failover verification) — P2.
- Runtime verification of browser/computer-use tools — P2.
- Interactive code-execution loop in ReAct (sandbox session workspace + iterative outputs) — P1
  (benchmark R3). Follow-up artifact editing ("update an existing report") via artifact
  versioning — P2.

## Phase 5 — Long-running work and continuity
- Run-completion webhooks for chat-initiated runs + automations (benchmark R2) — P1.
- Durable queue behind `automation_executor` (feature-flagged; in-process fallback) — P1.
- ReAct checkpoint-resume (benchmark R5) — P2. Notification surfacing in chat — P2.

## Phase 6 — Execution UX
- Model selector in chat (benchmark R1) — P1 (part of Phase 1 UI scope).
- Rich approval workbench (R6) — P2. Evidence-presentation polish — P3.
- Lifecycle UI parity with H1 (tree shows truthful partial/failed/cancelled).

## Phase 7 — Evaluation and production hardening
- Scenario-based evaluation harness across the 8 departments + 5 cross-department missions
  (mission §24) driven by the existing P1 scenario fixtures — P1.
- Populate `eval_results` (quality dimensions of mission §25) — P2.
- Security/perf/recovery test sweep — P2. Live provider integration — **BLOCKED externally**
  (funded provider; prior 429 quota exhaustion). Deployment verification — requires running stack.

## Dependency notes
- H1 (lifecycle) precedes Phase 6 UI parity — the UI must display what the backend now
  truthfully records.
- R4 (queue) gates Phase 5's webhook/notification promises at scale.
- Scenario harness (Phase 7) depends on Phase 1–3 enrichment only for coverage, not
  correctness — it can start once H1 lands.

## Constraints honored throughout
Reuse ReAct/broker/contracts/governance/discovery/ontology/NL2SQL/artifacts/visualization;
feature-flag risky changes; backward compatible; no unnecessary migrations; no second
orchestrator/registry; authorization never bypassed; no unverified claims.

## Live-acceptance blocker (standing)
All LIVE-VERIFIED claims remain gated on a funded LLM provider + running deployment
(historic 429 insufficient_quota). Mocked/fixture tests are integration evidence only.
