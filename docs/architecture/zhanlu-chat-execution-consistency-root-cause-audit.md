# Zhanlu — Chat Execution Consistency: Root-Cause Audit

2026-10-31 · Investigation-first audit per `ZHANLU — END-TO-END INVESTIGATION.md`.
Method: read-only code tracing + live `agent_invocations` row inspection on the
production dev DB (:5400). No LLM credits used. Phase 3 fixes are listed per
defect and implemented only where marked CONFIRMED-FIXED.

## 1. Architecture (as-built)

```
User message
  └─ POST /conversations/v3/{c}/messages/stream  (agents.py add_message_stream)
       ├─ route dispatch: workflow | cache | fsm | react (exclusive branches)
       ├─ react: turn-plan → toolset → ReAct loop (LLM ⇄ tools)
       │    ├─ tool results → tool_calls_for_frontend (in-memory list)
       │    ├─ delegation → delegate_tool → AgentInvocation(sub_agent, open→terminal)
       │    ├─ live events (_push_live_event → SSE + persisted activity stream)
       │    ├─ verify gate (inline, agents.py ~19034) → verify_passed/verify_failed
       │    ├─ finalize → done{content, answer_persisted, conversation}
       │    └─ record_invocation(type="conversation") — ONE row per turn
       └─ persistence: chat_messages row (authoritative answer)
Execution tree UI: AgentExecutionTree → GET /executions/tree?conversation_id
  → build_agent_tree() over ALL agent_invocations rows of the conversation.
Evidence: EvidenceRegistry (provide_evidence tool) → sub-agent output_json
  .evidence_ids → build_agent_tree evidence_count.
```

## 2. Confirmed defects (with root causes)

### D1 (P1) — Execution tree is conversation-scoped; every turn re-renders all prior turns
- **Symptom:** "Give me last week's sales data" (direct execution) showed
  "general_assistant — Completed" twice, no specialist.
- **Root cause:** `build_agent_tree` (agent_invocations.py:649-671) filters by
  `conversation_id` only — the schema has no turn identifier, the endpoint has
  no turn parameter, and the frontend renders this whole-conversation tree on
  EVERY assistant bubble (MessageBubble.jsx:560-566, AgentExecutionTree.jsx
  fetch). Live DB proof: conversation `830debd4…` holds exactly 2
  `invocation_type="conversation"` rows (14:17:37 + 14:18:14) — the "two GA
  nodes" are two real turns, both correct rows, wrongly aggregated into one
  view. **Not a phantom-count bug: an accounting/scope bug.**
- **Also CONFIRMED:** top-level `sub_agent` rows have `span_id=None`
  (agents.py never injects parent linkage) → `_rows_to_tree` (627-646) hoists
  them to roots: delegated specialists appear as SIBLINGS of the supervisor,
  not children — wrong tree shape, inflated root count.
- **Direct execution is CORRECT behavior** for "give me sales data" (mission
  B1 caveat): GA's own governed NL2SQL is sufficient; specialists are for
  analysis, not raw retrieval. Draft agents excluded (verified in the
  automotive Phase-E run: only published agents are discoverable).
- **Fix (implemented):** `build_agent_tree(started_after=…)` turn-window
  filter; `/executions/tree?started_after=` param; frontend anchors each
  bubble to its first persisted live-event timestamp (server clock); orphan
  `sub_agent` rows attach to the enclosing supervisor turn row in
  `_rows_to_tree`.

### D2 (P2) — Direct-execution turns can never have evidence
- **Symptom:** report full of numbers; execution panel "Evidence: 0".
- **Root cause:** the evidence pipeline is delegation-only:
  `provide_evidence` tool → EvidenceRegistry → collected into the CHILD's
  `output_json.evidence_ids` (delegate_tool.py:1207-1231, 737/798) →
  `build_agent_tree` counts only those. The direct-execution GA turn records
  its invocation with NO `output_json` (agents.py:21007-21026) and
  `query_database_tool.py` never touches the registry. Live DB proof: ALL 226
  invocation rows have `output_json=NULL`. `ObservationRecord` cannot be the
  vehicle for chat turns (NOT NULL FK to `executions.id`, which chat turns
  don't have).
- **Fix (implemented):** at turn end, successful DATA-tool results are stored
  as real EvidenceItems in the existing shared registry (provenance: tool,
  rows, source) and their ids referenced from the GA turn's
  `output_json.evidence_ids` → tree `evidence_count` reflects actual stored
  evidence; `/executions/evidence` resolves the ids. No counters invented.

### D3 (P2) — Verify gate is a pure OR over tool failures
- **Symptom:** one failed `read_file` (resource not found) alongside two
  successful queries → headline "Verification failed — some steps did not
  complete" on a fully grounded answer.
- **Root cause:** agents.py:19034-19041 — `_any_failed_tool` over ALL tool
  statuses; no grounding input, no weighting.
- **Fix (implemented):** extracted `_verification_verdict()` — verdict is
  FAILED only when failures exist AND no successful data call grounds the
  answer; grounded-but-with-failures emits verify_passed with
  `failed_steps` in the event params (honest exceptions, no silent ignore).
  Mission invariants preserved: a failed required operation with no data
  grounding still fails verification; failed steps are listed, never hidden.

### D4 (P0, FSM route) — Dual chat_messages persistence per FSM turn
- **Root cause:** `_persist_fsm_turn` (agents.py:1071-1085) INSERTs a new row
  with a fresh uuid while the client keeps its optimistic local row (different
  id) — an empty→filled same-text twin per FSM turn (`SYNEXIA_FSM_ENABLED`
  defaults TRUE, so production-reachable). The render filter hides empty rows,
  so the visible symptom is intermittent; the DB duplicate is real.
- **Status:** CONFIRMED code-path; fix deferred to a dedicated change (client
  id-adoption on done) — flagged P0 in the register, needs its own focused TDD
  cycle because the done/merge contract spans 3 files (see §5).

### D5 (P1, mitigated) — Mid-stream `content_replace ""` resets
- Server still streams empty content_replace on generator retries
  (agents.py:15309/15430/15507); the frontend guard (answerLifecycle.js:62-70)
  rejects empty resets, so the observed collapse is mitigated. Residual risk:
  ordering between the post-loop `_suppress_chat_deltas` replace (20927-20934)
  and the empty-bubble guarantee (20444-20465). CLASSIFICATION: mitigated,
  keep under watch; no change in this pass (would touch retry semantics).

## 3. Verified-correct behavior (no action)

- One `conversation` invocation row per turn, exactly 4 mutually-exclusive
  call sites (workflow/cache/fsm/react) — no duplicate supervisor rows within
  a turn.
- Two-phase sub-agent lifecycle closes the SAME row (no duplicate) — plus the
  2026-10-31 fallback guard (insert only when the start row is absent).
- SSE-synthesized tree never fabricates supervisor/specialist nodes.
- Draft agents excluded from discovery; department scoping works (Phase E
  verification, automotive workspace).
- Client watchdog (90s), `llm_retry`, heartbeats, `_guarantee_done` all
  present on the react path.
- Report data-quality items (D6, below).

## 4. Potential defects / pending live verification

- **D6 (P4) Report data quality** — "coverage ended Oct 29 vs period Oct 30",
  revenue-without-quantity rows, recommendation/quantity reconciliation.
  Requires the actual SQL + source rows of that turn (NL2SQL provenance) to
  distinguish inclusive/exclusive date-boundary defects from source-data
  gaps (nightly warehouse cut-off is a COMMON legitimate cause of T-1
  coverage). Classification: POTENTIAL — needs the turn's persisted SQL;
  recommended next step: enable NL2SQL query-lineage capture for report turns
  (already exists: P-SV2 lineage) and re-run one report with logging.
  No speculative fix applied (mission rule).
- **D7 (P5)** — automation-only polling re-merge window (chatPoll.js:51-57)
  could overwrite a just-finished streaming answer on automation sessions.
  POTENTIAL; low frequency.
- **D8 (P5)** — broker-blocked rows resolve agent_app_id=None →
  general_assistant fallback name (agent_invocations.py:53-64) — blocked
  delegation renders as a "general_assistant" supervisor node with
  status=blocked. Cosmetic mislabel; noted.

## 5. Severity / priority register

| ID | Defect | Sev | Pri | Status |
|----|--------|-----|-----|--------|
| D1 | Turn-scoped tree + orphan grouping | High | P1 | FIXED (TDD) |
| D2 | Direct-execution evidence | High | P2 | FIXED (TDD) |
| D3 | Verify-gate grounding | Medium | P2 | FIXED (TDD) |
| D4 | FSM dual persistence | High | P0 | CONFIRMED, fix scheduled |
| D5 | Empty content_replace ordering | Medium | P1 | Mitigated (watch) |
| D6 | Report data quality | Medium | P4 | Pending live provenance |
| D7 | Poll re-merge window | Low | P5 | Potential |
| D8 | Broker-blocked node naming | Low | P5 | Potential |

## 6. Regression risks

- Tree `started_after` filter: bubbles without persisted live events fall
  back to conversation-wide view (old behavior) — no data loss, only scope.
- Orphan grouping only re-parents `sub_agent` rows inside a supervisor turn
  window — peer-collaboration rows keep their persisted `requesting_agent_id`
  semantics.
- Evidence registry items carry `validation_status=unvalidated` — the tree
  counts references, never claims verification.
- Verify-gate: `verify_failed` still fires for failure-without-grounding and
  clarify turns are untouched.
