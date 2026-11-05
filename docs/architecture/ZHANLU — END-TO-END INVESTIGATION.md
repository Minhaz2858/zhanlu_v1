# ZHANLU — END-TO-END INVESTIGATION: RESPONSE STREAMING, AGENT ORCHESTRATION, EXECUTION TREE & ANSWER CONSISTENCY

## Mission

Conduct a systematic, end-to-end architectural investigation of Zhanlu's General Assistant and multi-agent execution system.

We have identified several potentially serious logical inconsistencies during real UI usage.

Do not assume these are isolated frontend problems.

Trace the complete lifecycle:

User message → General Assistant → intent understanding → planning → tool/agent discovery → execution strategy → tool execution/delegation → observations → evidence aggregation → reviewer/verification → final-answer generation → SSE streaming → message persistence → frontend rendering → execution-tree reconciliation.

Your responsibility is to identify the actual root causes, identify other related logical defects, and implement only verified fixes after documenting the findings.

**Investigation first. No speculative fixes. No unnecessary rebuilding.**

---

# PART A — OBSERVED DEFECTS

## A1. Final answer collapses and rewrites itself

Observed behavior:

1. General Assistant begins generating a response.
2. Content becomes visible in the chat.
3. The existing response suddenly collapses, disappears, or gets replaced.
4. The assistant appears to generate or rewrite the same answer again.
5. Sometimes the execution stream continues showing finalization while the answer is already visible.

Investigate the entire message lifecycle.

Backend:

- ReAct loop termination.
- Final response generation.
- Streaming response emission.
- `done`, `final`, `message_complete`, or equivalent event handling.
- Message persistence.
- Conversation-history updates.
- Retry and recovery paths.
- Multiple finalization branches.
- Artifact and report generation paths.

Frontend:

- Chat.jsx.
- MessageBubble.
- Streaming message state.
- SSE event handlers.
- Message reconciliation.
- Temporary message IDs versus persisted message IDs.
- React state updates and component keys.
- Markdown rendering.
- Execution activity stream.
- Scroll and rendering lifecycle.

Determine whether the same assistant turn is being:
- Generated twice.
- Persisted twice.
- Appended twice.
- Replaced by a second message.
- Reconstructed from incomplete history.
- Re-rendered using a stale snapshot.

Check whether a streaming message and its persisted equivalent are incorrectly treated as two independent messages.

### Required invariant

One user turn must produce one authoritative assistant answer.

Streaming chunks must update the same logical message.

Final persistence must reconcile with the streamed message rather than replace it with an older or incomplete version.

A completed message must never spontaneously revert to an earlier state.

If a retry or correction is genuinely necessary, it must be explicit and must not silently erase valid content.

---

# PART B — AGENT DISCOVERY AND DELEGATION

## B1. Investigate why specialists are not appearing

The Supply Chain & Procurement project currently contains these agents:

1. Supplier Risk Agent — published
2. Inventory Management Agent — published
3. Supply Chain Optimization Agent — published
4. Data Analyst — draft

The user asked:

"Give me last week's sales data."

The execution tree showed:

- general_assistant — Completed
- general_assistant — Completed

No specialist appeared.

Important: Do not automatically conclude that this is a defect.

A simple database query may be correctly handled directly by General Assistant using its own authorized NL2SQL tools.

However, investigate whether specialist discovery and delegation are functioning correctly.

Trace:

1. How the active project is resolved from the conversation.
2. How project metadata enters General Assistant's context.
3. How AgentApp records are loaded.
4. How published status is filtered.
5. How department and project scope are applied.
6. How available agents are exposed to the planner.
7. How the LLM selects direct execution versus delegation.
8. How the Collaboration Broker authorizes delegation.
9. How specialist invocations are persisted.
10. How the frontend receives and renders those invocations.

Inspect the complete discovery path.

Particularly verify:

- Is the active project actually included in the execution context?
- Are published specialists available to General Assistant?
- Is the draft Data Analyst correctly excluded?
- Is agent discovery only happening in some execution paths?
- Are agent descriptions and capabilities sufficiently informative?
- Is the planner choosing direct NL2SQL because it is genuinely sufficient?
- Is delegation being prevented by a permission, publication, or context-resolution bug?
- Are specialists being executed but missing from the execution-tree API?

## B2. Distinguish supervisor execution from specialist execution

The execution tree must distinguish:

- Supervisor: General Assistant.
- Specialist: Actual AgentApp identity.
- Tool: A tool execution, not an agent.
- Peer collaboration: Actual peer relationship.
- Direct execution: General Assistant working independently.

Do not count multiple General Assistant turns as multiple specialist agents.

A user turn should have one identifiable supervisor execution, with child executions only when actual delegation occurs.

Historical conversation messages must not be counted as current-turn agents.

The agent summary must derive from the correct current-turn execution records.

Expected behavior:

Direct execution:

General Assistant
  └── Database query
  └── Evidence verification
  └── Final answer

Delegated execution:

General Assistant
  ├── Supplier Risk Agent
  ├── Inventory Management Agent
  └── Evidence aggregation
      └── Final answer

These are two different execution strategies and must be displayed honestly.

Do not fabricate specialist executions simply to populate the panel.

---

# PART C — EXECUTION AND EVIDENCE CONSISTENCY

Investigate this observed sequence:

- `read_file` failed with resource not found.
- Database query succeeded.
- Another database query succeeded.
- Verification reported that some steps did not complete.
- General Assistant still generated a business report.
- Execution tree showed zero evidence.

Determine whether the final report was actually supported by the query results.

Inspect:

1. ObservationRecord persistence.
2. Evidence registry.
3. Evidence IDs.
4. Evidence references in AgentInvocation.
5. Tool-result-to-evidence conversion.
6. Query provenance.
7. Reviewer input.
8. Verification decision.
9. Final-answer grounding.
10. Execution-tree evidence aggregation.

### Critical invariants

- A failed irrelevant tool must not invalidate otherwise successful work.
- A failed required operation must not be silently ignored.
- Successful SQL execution must not automatically be counted as verified evidence.
- Evidence count must reflect actual evidence records associated with the current turn.
- The answer must not claim verification passed if verification failed.
- A partial result must be labelled as partial.
- Unsupported numerical claims must never be invented.

Investigate why the execution panel reports zero evidence when the final answer contains detailed numerical analysis.

Do not fix this by artificially incrementing evidence counters.

Find the actual persistence or association gap.

---

# PART D — REPORT DATA QUALITY AND BUSINESS LOGIC

Use the supplied screenshots as reproduction evidence.

The example question was:

"Give me last week's sales data."

The resulting report included:

- Reporting period October 24–30, 2026.
- Revenue by product.
- Quantity.
- Order count.
- Top customers.
- Customer concentration.
- Recommendations.
- Data coverage warnings.

Observed inconsistencies include:

1. Data coverage ended October 29 although the reporting period ended October 30.
2. Some products had revenue but missing quantities and order counts.
3. Recommendations used quantity figures that require reconciliation with the main table.
4. Some records showed zero sales quantity but positive revenue.
5. The report identified customer concentration risk.

Investigate whether these are legitimate business conditions, incomplete source data, SQL aggregation defects, or incorrect interpretation by the LLM.

Check:

- Time-zone boundaries.
- Week-start and week-end calculations.
- Inclusive versus exclusive date filters.
- Missing data.
- NULL handling.
- Zero versus missing values.
- Product/customer joins.
- Duplicate order records.
- Revenue aggregation.
- Quantity aggregation.
- MetricDefinition bindings.
- SQL execution provenance.
- Report-generation payloads.

Never silently replace missing values with zero.

Never calculate authoritative business metrics inside the LLM.

Use existing governed NL2SQL and MetricDefinition mechanisms.

The final report must clearly distinguish:
- Verified values.
- Missing values.
- Partial coverage.
- Unavailable fields.
- Derived analysis.

---

# PART E — GENERAL ASSISTANT EXECUTION LOGIC

Audit the entire General Assistant lifecycle.

Determine whether these stages are consistent:

1. Intent understanding.
2. Objective extraction.
3. Context resolution.
4. Requirement clarification.
5. Planning.
6. Capability discovery.
7. Strategy selection.
8. Direct execution or delegation.
9. Observation.
10. Adaptive replanning.
11. Evidence aggregation.
12. Reviewer evaluation.
13. Final answer.
14. Persistence.
15. UI reconciliation.

Investigate whether:

- The planner creates a plan but the executor follows a different strategy.
- Tool failures trigger unnecessary repeated execution.
- The same database query runs multiple times.
- The agent executes unnecessary specialists.
- The agent skips a required specialist.
- A completed child is executed again.
- The parent responds before required children finish.
- A final answer is generated before reviewer verification.
- The answer is regenerated after successful completion.
- Historical invocation rows contaminate current execution state.

Preserve the existing ReAct architecture, Collaboration Broker, plan-DAG, recovery policies, and evidence mechanisms.

Do not introduce another orchestrator.

---

# PART F — ADDITIONAL LOGICAL BUG AUDIT

Inspect related areas for defects even if they are not directly visible in the screenshots.

Prioritize:

1. Duplicate assistant responses.
2. Missing final responses.
3. Stale streaming state.
4. Duplicate SSE event processing.
5. Incorrect execution counts.
6. Incorrect supervisor/specialist identity.
7. Incorrect parent-child relationships.
8. Missing terminal events.
9. Incorrect execution durations.
10. Evidence count inconsistencies.
11. Failed verification with successful-looking output.
12. Incorrect handling of partial completion.
13. Context leakage between projects.
14. Incorrect department-agent discovery.
15. Unauthorized cross-department access.
16. Duplicate SQL execution.
17. Inconsistent conversation persistence.
18. Reconnection and historical-state inconsistencies.

For each finding, classify it as:

- Confirmed defect.
- Potential defect requiring reproduction.
- Existing correct behavior.
- Pre-existing limitation.
- Not reproducible.

Do not change unrelated foreign working-tree changes.

---

# PART G — REQUIRED INVESTIGATION METHOD

Follow systematic-debugging discipline.

### Phase 1: Read-only investigation

Before modifying code:

- Inspect Git status and existing diffs.
- Identify foreign uncommitted WIP.
- Map all relevant backend and frontend modules.
- Trace the complete request lifecycle.
- Inspect existing tests.
- Identify exact event names and payload structures.
- Inspect database models and relationships.
- Identify current project and conversation identifiers.
- Reproduce the reported behavior using deterministic fixtures where possible.

Do not assume earlier documentation perfectly describes current code.

Ground every finding in actual implementation.

### Phase 2: Root-cause report

Before implementation, produce:

`docs/architecture/zhanlu-chat-execution-consistency-root-cause-audit.md`

Include:

- System architecture diagram.
- End-to-end sequence diagram.
- Root-cause analysis for each observed defect.
- Relevant code locations.
- Backend/frontend responsibility mapping.
- Current behavior versus expected behavior.
- Confirmed and potential logical bugs.
- Severity and priority.
- Minimal fix recommendations.
- Regression risks.

Do not implement speculative changes.

### Phase 3: TDD implementation

Only after the audit:

1. Write a deterministic regression test reproducing each confirmed defect.
2. Verify RED.
3. Implement the smallest root-cause fix.
4. Verify GREEN.
5. Run directly related regression suites.
6. Check unrelated behavior for regressions.

Preserve existing APIs unless a compatibility issue is proven.

### Phase 4: Final verification

Verify:

- One user turn → one final answer.
- Streaming and persisted messages reconcile.
- No answer disappears or rewrites unexpectedly.
- Direct execution displays General Assistant correctly.
- Real delegation displays actual specialist identities.
- Draft agents remain excluded.
- Evidence counts reflect persisted evidence.
- Verification status matches the actual result.
- Partial completion is honest.
- Historical conversations remain intact.
- Project and department permissions remain intact.

---

# PART H — CREDIT CONSERVATION

We have approximately 90 LLM credits remaining.

This investigation must be primarily code-based and deterministic.

STRICT RULES:

- No unnecessary live LLM requests.
- No external embedding API calls.
- No repeated production-like scenarios using paid models.
- No unnecessary full regression sweeps.
- Reuse existing fixtures and mock providers.
- Run focused tests before broader tests.
- Use local deterministic SQL fixtures where possible.
- Do not consume credits just to observe an already-known failure.

A live test is not required to prove a deterministic state-management or event-ordering defect.

If a behavior cannot be proven without a live provider, document it as pending live verification.

---

# PART I — DELIVERABLE AND PRIORITIES

Priority order:

P0: Prevent assistant answers from disappearing, duplicating, or rewriting.

P1: Fix execution-tree identity and current-turn accounting.

P2: Fix evidence and verification consistency.

P3: Verify project-scoped specialist discovery and delegation.

P4: Fix confirmed report data-quality and business-logic issues.

P5: Additional logical bug fixes identified during the audit.

Produce:

1. Root-cause audit document.
2. Confirmed defect register.
3. Before/after behavior.
4. Files modified.
5. Tests added and executed.
6. Regression results.
7. Remaining limitations.
8. Live-verification requirements.
9. Updated architecture/progress documentation.

## Final engineering principles

- One user request, one authoritative response.
- One supervisor per execution turn.
- Specialist agents only when actual delegation occurs.
- No fabricated agent activity.
- No fabricated evidence.
- No unsupported business figures.
- No silent loss of completed work.
- No unnecessary retries.
- No unauthorized data access.

**The objective is not to make the UI look correct by hiding failures. The objective is to make the underlying execution lifecycle correct and ensure the UI faithfully represents what the system actually did.**

Work autonomously through investigation, root-cause verification, implementation, and focused testing. Do not stop after producing a list of possible causes.