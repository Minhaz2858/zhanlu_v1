# Zhanlu — Phase 7C Architectural Verification + Phase 8 Implementation Specification

**Mission:** Independently verify the architectural completeness of Phase 7C and prepare a comprehensive, implementation-ready Phase 8 specification.

**Execution mode:** Autonomous investigation, evidence-based verification, architecture review, and specification authoring.

**Branch:** `fix/automation-chat-artifacts-ordering`

**Reported Phase 7C HEAD:** `df526032c` (verify against actual Git history; do not assume this is still HEAD).

---

# GLOBAL EXECUTION RULES

You must work autonomously.

Do not stop after producing preliminary findings. Complete both workstreams, validate your conclusions, and produce the final documentation.

Follow these rules:

1. Inspect the actual repository, Git history, working tree, backend, frontend, migrations, tests, and existing architecture documents.
2. Never assume previous implementation reports are accurate without independently verifying the code.
3. Every architectural claim must be supported by actual file paths, function names, and line references.
4. Clearly distinguish CONFIRMED, SUSPECTED, NOT A DEFECT, and DEFERRED.
5. Never fabricate implementation status, test results, database behavior, or architectural capabilities.
6. Preserve existing architecture and reuse existing services.
7. Do not create duplicate subsystems or competing abstractions.
8. Never overwrite another developer's work or unrelated uncommitted changes.
9. Do not perform destructive Git operations.
10. Do not commit unrelated files.
11. Do not implement Phase 8 during this task. Phase 8 must first receive a complete, reviewed implementation specification.

Use RED → GREEN → REVIEW if a minimal non-production test is needed to validate an architectural claim. The main Phase 7C review must remain read-only.

# WORKSTREAM 1 — PHASE 7C ARCHITECTURAL COMPLETENESS AUDIT

## 1.1 Establish the actual repository state

Inspect:

- Current branch and HEAD.
- Git commit history.
- Working tree status.
- Staged and unstaged changes.
- Untracked files.
- Existing foreign-dirty files.
- Phase 7C implementation commits.
- Phase 7C audit and progress documentation.

Verify the discrepancy in the previous completion report, which claims nine commits but lists ten commit hashes.

Establish the exact commit sequence and determine whether all reported changes are actually present at the current HEAD.

**Critical:** Never reset, stash, clean, checkout over, or overwrite foreign WIP.

## 1.2 Independently audit all six Phase 7C workstreams

Use the original audit:

`docs/plans/2026-11-02-phase-7c-architecture-audit.md`

Verify every finding against the actual implementation.

### A — SemanticGuidelines vs BusinessRule

Verify:

- BusinessRule remains authoritative.
- SemanticGuideline remains a retrieval representation.
- Derived marker ownership and lifecycle.
- Create/PATCH marker-prefix protection.
- Disable/reject/resubmit synchronization.
- Idempotency and manual-row protection.
- Concurrent synchronization behavior.
- Organization-wide rule materialization.
- Datasource targeting and consumer scoping.

Identify any remaining lifecycle, consistency, or concurrency risks.

### B — Datasource auto-binding and authorization

Verify:

- Cross-organization pinned datasource protection.
- Workspace-settings authorization.
- Organization-admin write authorization.
- Verified organization resolution.
- Auto-bind behavior across database and file resources.
- Explicit datasource binding behavior.
- V2/V3 differences.
- Absence of default/first-datasource fallback.
- Absence of automatic query-all execution.

Pay particular attention to whether authorization is enforced at every entry point, not merely the primary API.

### C — AuditEvent governance

Verify:

- Organization attribution for NL2SQL audit rows.
- Event type/source/outcome registries.
- Actual writer vocabulary against registered vocabulary.
- Contract-test completeness.
- Sensitive-data handling.
- Failure-path audit behavior.
- Deferred db_tools policy-denial auditing.

Determine whether the registry contract can detect future vocabulary drift reliably.

### D — Resource lifecycle and orphan cleanup

Verify every deletion path:

- Normal entity deletion.
- Dedicated automation deletion.
- Bulk deletion.
- AgentApp deletion.
- Resource soft deletion.
- Cleanup ordering.
- Watchdog missing-resource handling.
- Tombstone behavior.
- Retired ontology links.
- Organization filtering in resource digests.

Determine whether all production deletion paths invoke cleanup correctly.

Pay special attention to transaction boundaries, repeated deletion, partial failure, and idempotency.

### E — Canonical provenance

Verify:

- Canonical prov-v1 generation.
- SQL lineage-only requests.
- Document evidence conversion.
- Process evidence conversion.
- Tri-state evidence integrity.
- Execution reference organization authorization.
- Provenance serialization.
- Evidence-status honesty.

Determine whether the canonical provenance contract is consistent across all production writers and consumers.

### F — Business Context Snapshot (BCS)

Verify:

- V2/V3 chat consumers.
- Explainability endpoint.
- NL2SQL planning.
- Datasource narrowing.
- Agent-bound rule filtering.
- Rejected process filtering.
- Metric lifecycle gates.
- Organization isolation.
- Department context handling.
- Subagent context isolation.

Determine whether BCS consumers expose consistent information under different execution paths.

## 1.3 Perform a second-order architectural audit

Do not limit the audit to the original 19 tasks.

Investigate whether the fixes introduced or exposed additional issues.

Specifically inspect:

1. Transaction integrity and rollback behavior.
2. Concurrent requests and race conditions.
3. Multi-tenant isolation across service boundaries.
4. Authorization consistency between API, agent tools, MCP, and background workers.
5. Soft-delete and historical-record consistency.
6. Audit completeness.
7. Provenance completeness and serialization compatibility.
8. Backward compatibility with existing conversations and agents.
9. Migration compatibility and existing database deployments.
10. Frontend/backend contract consistency.
11. Test coverage versus actual production execution paths.
12. Failure handling and recovery behavior.

Do not manufacture defects merely to make the audit look comprehensive.

Every additional finding must have reproducible evidence or a clearly labeled suspected risk.

## 1.4 Verify test and build evidence

Inspect existing test suites and identify:

- Tests specifically added for Phase 7C.
- Existing tests modified by Phase 7C.
- Tests that verify actual endpoint behavior.
- Tests that only exercise service functions directly.
- Tests missing real authorization boundaries.
- Tests missing transaction or concurrency behavior.

Run appropriate targeted regression suites if the repository environment permits.

Do not claim a suite passed unless you actually executed it.

Do not launch an expensive full regression unnecessarily if targeted tests provide sufficient evidence.

## 1.5 Produce Phase 7C audit report

Create:

`docs/audits/2026-11-02-phase-7c-architectural-completeness-review.md`

Required structure:

1. Executive assessment.
2. Verified Git state and commit reconciliation.
3. Phase 7C completion verification.
4. Six-workstream audit results.
5. Second-order architectural findings.
6. Security and multi-tenant review.
7. Transaction and lifecycle review.
8. Provenance and audit governance review.
9. Test coverage and regression evidence.
10. Confirmed remaining defects.
11. Suspected risks requiring further investigation.
12. Deferred technical debt.
13. Architecture invariants that must remain unchanged.
14. Final acceptance verdict.

Classify each finding:

- CRITICAL
- HIGH
- MEDIUM
- LOW
- DEFERRED
- NO DEFECT

Provide:

- Finding ID.
- Evidence.
- Impact.
- Reproduction or verification method.
- Recommended correction.
- Files affected.
- Whether it blocks Phase 8.

**Acceptance criterion:** Phase 7C must receive an explicit architectural acceptance verdict. If critical defects remain, identify them as blockers rather than declaring the phase complete.

---

# WORKSTREAM 2 — PHASE 8 IMPLEMENTATION SPECIFICATION

After completing Workstream 1, prepare the next milestone.

The working title is:

**Phase 8 — Agent Harness Source Selection, Explicit Resource Bindings & Canonical AgentRun Provenance**

Primary objectives:

1. Replace implicit datasource availability with explicit, governed agent-resource bindings.
2. Migrate users away from `auto_bind_all_datasources`.
3. Make source selection understandable and controllable through the existing Agent Harness.
4. Introduce canonical AgentRun-side provenance envelopes.
5. Ensure every agent execution can be traced to authorized resources, tools, evidence, and execution observations.

The following is the initial scope hypothesis. Independently inspect the repository and refine it based on actual architecture.

## 2.1 Workstream A — Explicit multi-source Agent Binding UX

Investigate the existing:

- AgentApp configuration.
- KnowledgeBase relationships.
- AgentDataBinding.
- DataSourceRuntime.
- Resource Center.
- Agent Studio / Agent Configuration UI.
- Workspace settings.
- Auto-bind configuration.
- Datasource authorization.
- Runtime source descriptors.
- Project and department selection.
- Existing resource discovery and binding APIs.

The goal is to let users explicitly configure which enterprise resources an agent can access.

Requirements:

### Binding management

Users must be able to:

- Discover resources available within their authorized organization/project.
- Select multiple heterogeneous resources.
- Bind multiple databases and file-based knowledge resources.
- View currently bound resources.
- Inspect resource type and connection state.
- Enable or disable individual bindings.
- Configure appropriate read/write permissions.
- Remove bindings safely.
- Understand when a resource is unavailable or requires reconnection.

### Runtime behavior

Preserve the fundamental Agent Harness architecture:

**The active agent decides:**

- What the user intends.
- Whether clarification is necessary.
- Which resources are relevant.
- Which tools should be used.
- Whether multiple sources are needed.
- How to plan and execute the task.

**The harness enforces:**

- Organization authorization.
- Resource ownership.
- Agent binding scope.
- Tool RBAC.
- Execution permissions.
- Query safety.
- Provenance.
- Budgets and limits.
- Auditability.

Mandatory invariants:

- No default datasource.
- No first-datasource fallback.
- No implicit datasource selection.
- No automatic query-all behavior.
- No client-controlled cross-organization resource binding.
- No bypass through MCP, delegated agents, or alternate execution paths.
- Multiple heterogeneous resources must remain supported.
- Agents must dynamically choose sources based on task requirements.

Do not introduce a second datasource-routing architecture.

### Auto-bind migration

Investigate `auto_bind_all_datasources`.

Design a backward-compatible migration:

1. Determine existing users and agents depending on auto-bind.
2. Define how current effective resource access is represented by explicit bindings.
3. Preserve existing authorized access during migration.
4. Introduce an observable migration state.
5. Provide a controlled transition period.
6. Make explicit bindings the authoritative configuration.
7. Deprecate the auto-bind flag.
8. Define the safe removal criteria.

Never silently remove access or silently widen access.

Do not remove the flag until migration and compatibility acceptance criteria are met.

## 2.2 Workstream B — AgentRun canonical provenance

Investigate:

- AgentRun and AgentRunStep models.
- Tool invocation records.
- AgentInvocation.
- Delegation and peer collaboration.
- Observation records.
- FinalResponseWriter.
- SQL lineage.
- Document provenance.
- Ontology provenance.
- Canonical prov-v1 implementation.
- Existing audit and revision mechanisms.

The objective is to establish a canonical provenance envelope for agent execution.

The proposed lifecycle:

```text
User Request
    |
    v
AgentRun Created
    |
    v
Agent Planning
    |
    v
Authorized Resource Selection
    |
    v
Tool Invocation
    |
    v
Execution Observation
    |
    v
Evidence / Provenance Collection
    |
    v
Verification
    |
    v
Final Response
    |
    v
Canonical AgentRun Provenance Envelope
```

The specification must define:

### Run-level provenance

- AgentRun ID.
- Organization ID.
- Project/conversation references where applicable.
- Agent identity and version.
- Execution timestamps.
- Final execution status.
- Parent/child invocation relationships.
- Execution policy and relevant permission context.

### Resource provenance

- Selected resource identifiers.
- Resource types.
- Authorized binding references.
- Organization ownership.
- Source selection rationale.
- Resource access outcomes.

### Tool provenance

- Tool invocation ID.
- Tool name.
- Agent identity.
- Input/output references.
- Execution status.
- Timing.
- Authorization decision.
- Error or refusal information.

### Evidence provenance

- SQL lineage.
- Document evidence.
- Ontology references.
- Process evidence.
- Metric references.
- Execution references.
- Source citations.
- Verification status.

### Observation provenance

- Agent reasoning/plan references where safely retained.
- Tool results.
- Verification outcomes.
- Replanning events.
- Delegation events.
- Partial execution failures.

Do not persist unrestricted chain-of-thought or sensitive raw tool inputs merely to create an audit trail.

Define a safe, structured representation of decisions, plans, observations, and verification outcomes.

### Trust requirements

- Provenance must not imply verification.
- Missing evidence must remain unknown.
- Failed tools must not be represented as successful execution.
- Partial runs must be represented as partial.
- Cross-organization execution references must be rejected.
- Child execution provenance must be traceable to its parent.
- Provenance collection must not corrupt the main execution transaction.
- Historical provenance must remain stable.
- Repeated serialization must be deterministic where the underlying data is unchanged.

Prefer extending prov-v1 and existing observation records over inventing a second provenance format.

## 2.3 Workstream C — Integration across the Agent Harness

Inspect all relevant execution paths:

- V2 agent runtime.
- V3 agent runtime.
- ReAct/Plan-Act-Observe-Verify.
- NL2SQL.
- Document RAG.
- Ontology tools.
- MCP.
- A2A.
- Delegate tools.
- Workflow DAG execution.
- Background automation.
- General Assistant.
- Department agents.

The specification must explain how explicit resource bindings and canonical provenance work consistently across these paths.

Identify the true shared integration chokepoints.

Do not assume all paths share the same execution context without verifying it.

## 2.4 Workstream D — Security and governance

Specify:

- Organization isolation.
- Resource ownership verification.
- Binding-level permissions.
- Read/write separation.
- Agent identity validation.
- Delegation authorization.
- MCP authorization.
- Resource removal and revocation.
- Provenance access control.
- AuditEvent integration.
- Historical record retention.

Include negative authorization tests.

A resource becoming unbound or unauthorized must not remain usable through cached runtime context.

## 2.5 Workstream E — Frontend experience

Design the integration within existing Zhanlu interfaces.

Do not create a standalone application.

Investigate the actual frontend architecture and determine the appropriate location for:

- Resource Center.
- Agent configuration.
- Binding management.
- Runtime selected-source visibility.
- Resource access status.
- Provenance inspection.
- Execution trace and evidence visualization.

The UI should make it clear:

1. Which resources an agent is allowed to access.
2. Which resources the agent selected for the current task.
3. Why those resources were selected.
4. Which tools executed.
5. Which evidence supports the answer.
6. Which parts of the execution were incomplete or failed.

Keep the UI aligned with existing design patterns.

---

# 3. PHASE 8 ARCHITECTURE DECISIONS

Before writing the implementation plan, explicitly resolve:

1. Is AgentDataBinding authoritative, or should it be extended?
2. How should AgentApp.knowledge_bases interact with AgentDataBinding?
3. What is the canonical resource-binding identity?
4. How should database and file resources share one binding interface without losing their distinct capabilities?
5. How should binding revocation invalidate cached execution contexts?
6. How should explicit bindings propagate to child agents?
7. How should child agents retain their own authorization boundaries?
8. Where should canonical provenance envelopes be persisted?
9. Which execution choke point should create and finalize the envelope?
10. How should failed and partially completed runs be represented?
11. How should existing prov-v1 entries be linked to AgentRun?
12. How should provenance be exposed through APIs and the UI?
13. How should old auto-bind configurations migrate?
14. What compatibility period is required?
15. What tests prove the absence of implicit datasource fallback?

For every decision provide:

- Existing implementation.
- Proposed decision.
- Alternatives considered.
- Reason for selection.
- Compatibility implications.
- Security implications.
- Migration requirements.

Do not invent new tables, services, or abstractions before proving existing ones cannot support the requirement.

---

# 4. PHASE 8 IMPLEMENTATION ROADMAP

Produce an ordered implementation roadmap with small, independently testable increments.

For each task specify:

- Task ID.
- Objective.
- Existing integration point.
- Files to modify.
- New files, if genuinely necessary.
- Migration requirements.
- API contract changes.
- Frontend changes.
- Authorization requirements.
- RED tests.
- GREEN implementation.
- Regression suites.
- Acceptance criteria.
- Dependencies.
- Rollback strategy.

Separate work into logical phases such as:

- Phase 8A: Binding model and contract consolidation.
- Phase 8B: Backend binding APIs and authorization.
- Phase 8C: Agent configuration and Resource Center UX.
- Phase 8D: Runtime source-selection integration.
- Phase 8E: Delegation and multi-agent authorization propagation.
- Phase 8F: Canonical AgentRun provenance.
- Phase 8G: Provenance inspection and observability UI.
- Phase 8H: Auto-bind migration and deprecation.
- Phase 8I: Integrated acceptance and regression.

These are proposed work packages, not predetermined architecture. Reorder or merge them if repository evidence justifies it.

Define dependencies explicitly.

Avoid large, risky all-at-once migrations.

---

# 5. TESTING AND ACCEPTANCE STRATEGY

The Phase 8 specification must include a concrete verification matrix.

At minimum cover:

### Binding and authorization

- One agent with one database.
- One agent with multiple databases.
- One agent with database + file KB.
- Two agents with different bindings.
- Same organization, different projects.
- Cross-organization binding attempts.
- Disabled binding.
- Revoked binding.
- Resource deleted after binding.
- Cached context after revocation.
- Agent with zero bindings.
- MCP execution.
- Delegated child execution.

### Dynamic source selection

- Single-source question.
- Multi-source question.
- Question requiring two heterogeneous sources.
- Question requiring clarification.
- Irrelevant source should not be queried.
- No automatic query-all.
- No default-source fallback.
- Missing required source produces an honest refusal or clarification.

### Provenance

- Successful run.
- Failed run.
- Partial run.
- Replanned run.
- Multi-tool run.
- Multi-source run.
- Delegated run.
- Child failure.
- SQL lineage.
- Document evidence.
- Ontology evidence.
- Missing evidence.
- Cross-org execution reference.
- Stable serialization.
- Historical provenance retention.

### Regression

- V2 and V3.
- NL2SQL.
- MCP/A2A.
- Existing ontology and governance.
- Document ingestion and retrieval.
- Existing enterprise agents.
- Existing auto-bind users.
- Existing frontend workflows.

Define measurable exit criteria rather than merely stating that tests should pass.

---

# 6. REQUIRED DELIVERABLES

Create the following documents:

### Deliverable 1 — Phase 7C architecture review

`docs/audits/2026-11-02-phase-7c-architectural-completeness-review.md`

Must contain the independently verified audit and acceptance verdict.

### Deliverable 2 — Phase 8 architecture specification

`docs/superpowers/specs/2026-11-02-phase-8-agent-harness-source-selection-provenance.md`

Must contain:

- Goals and non-goals.
- Existing architecture inventory.
- Gap analysis.
- Architecture decisions.
- Binding model and lifecycle.
- Runtime source-selection contract.
- Delegation behavior.
- Provenance data contract.
- Authorization model.
- Frontend design.
- API contracts.
- Migration strategy.
- Failure handling.
- Security considerations.
- Testing strategy.
- Acceptance criteria.

### Deliverable 3 — Phase 8 implementation roadmap

`docs/plans/2026-11-02-phase-8-implementation-roadmap.md`

Must contain:

- Ordered tasks.
- Dependencies.
- File-level implementation guidance.
- TDD requirements.
- Acceptance criteria.
- Risk levels.
- Rollback plans.
- Milestone exit criteria.

### Deliverable 4 — Architecture decision record

`docs/plans/2026-11-02-phase-8-architecture-decisions.md`

Record the major decisions and alternatives, particularly:

- Authoritative binding model.
- SourceDescriptor contract.
- Child-agent authorization.
- Provenance persistence.
- Auto-bind migration.

### Deliverable 5 — Progress log

`docs/plans/2026-11-02-phase-8-progress-log.md`

Record:

- Investigation completed.
- Verified existing capabilities.
- Decisions made.
- Outstanding questions.
- Implementation dependencies.
- Acceptance status.

---

# 7. FINAL REVIEW

Before finishing:

1. Verify all referenced files and functions actually exist.
2. Verify all proposed integration points against actual code.
3. Check that Phase 8 does not duplicate existing functionality.
4. Check that Phase 8 preserves dynamic heterogeneous source selection.
5. Check that no default datasource or query-all behavior is introduced.
6. Check that delegated agents cannot inherit unauthorized resources.
7. Check that provenance does not falsely imply evidence verification.
8. Check that auto-bind migration does not silently widen or reduce access.
9. Check that the proposed database changes are actually necessary.
10. Check that all implementation tasks have testable acceptance criteria.
11. Verify documentation consistency across all deliverables.
12. Confirm no production code or unrelated files were modified.

# 8. COMPLETION REPORT

Return a concise but technically detailed report containing:

- Actual branch and HEAD.
- Phase 7C architectural verdict.
- Number of findings by severity.
- Confirmed defects and deferred risks.
- Test commands and actual results.
- Phase 8 architecture decisions.
- Phase 8 implementation roadmap.
- Documents created.
- Files changed.
- Confirmation that no unrelated WIP was overwritten.
- Explicit list of unresolved questions or blockers.

**Final instruction:**

Do not confuse specification completion with implementation completion.

The objective of this assignment is to independently establish whether Phase 7C is architecturally sound and deliver a technically grounded, implementation-ready Phase 8 specification.

Do not begin Phase 8 implementation until the specification is complete and the architectural decisions are documented.

Work autonomously. Do not stop at an outline. Deliver all five documents with evidence-backed content and a clear acceptance verdict.