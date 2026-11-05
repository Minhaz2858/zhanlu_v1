# Phase 3.2 — Semantic Capability Discovery Design (2026-10-31)

## Root cause

Specialist selection was purely lexical (Phase 2 `capability_fallback`): a
task phrased "who builds our vehicles" never matched the production agent.
AgentApp + AgentPortfolioEntry already carry rich descriptions/departments —
the gap was matching, not metadata.

## Design

`app/services/semantic_capability_match.py`:

- `semantic_select_specialists(task, candidates, embedder="auto", top_k=3,
  threshold=0.35, exclude={}, department=None)`
- Structured prefilter FIRST: published-only (authority), exclusions
  (failed agent never matches itself), department wall when supplied.
- Ranking: embedding cosine (LOCAL `get_embeddings_batch`, lazy, cached —
  no new provider). Query + candidate text (name + description +
  capabilities).
- Threshold: below 0.35 the list is EMPTY — "our own tools suffice" is the
  honest answer; no unnecessary delegation is forced.
- Rationale recorded per match: `{matcher, score, threshold}` — auditable.
- Degrade: embedder unavailable/failing → Phase-2 lexical matcher with
  `(matcher=lexical, embedding infra unavailable)` in the rationale. Never
  guesses.
- `embedder=None` forces the lexical path (testable); `"auto"` is production.

## Wiring

`delegate_tool._delegate_task` failure path now calls
`semantic_select_specialists` (auto) instead of raw lexical — alternative
capability suggestions are semantically relevant and still published-only.

## Guarantees kept

No second agent registry; published gate never bypassed; unauthorized
agents never selected; selection rationale always recorded; empty result is
a legitimate outcome.
