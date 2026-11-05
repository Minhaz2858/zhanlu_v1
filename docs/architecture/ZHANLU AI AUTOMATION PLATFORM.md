# ZHANLU AI AUTOMATION PLATFORM
# FULL AUTONOMOUS IMPLEMENTATION MISSION — R1 THROUGH R11

**Project:** Zhanlu Enterprise AI Agent Platform  
**Mission:** Complete the implementation, integration, refactoring, migration, testing, and acceptance of the entire Zhanlu AI Automation subsystem.

**Execution mode:** Autonomous end-to-end engineering  
**Priority:** Correctness → Reliability → Governance → Integration → Enterprise acceptance  
**Architecture status:** Approved for implementation  
**Implementation scope:** R1–R11

---

# 0. YOUR MISSION

You are responsible for completing the entire Zhanlu AI Automation implementation.

This is not another architecture-design assignment.

The architecture investigation, technical specification, correction passes, and final reliability reviews have already been completed.

Your job now is to turn the approved specifications into a working, integrated, reliable enterprise automation platform.

You are authorized to:

- Inspect and modify backend source code.
- Modify frontend source code.
- Create and execute Alembic migrations.
- Implement database models and constraints.
- Refactor existing automation services.
- Implement scheduler and execution infrastructure.
- Integrate the existing Agent Harness.
- Implement execution recovery and governance.
- Add and modify tests.
- Update API contracts.
- Update frontend components.
- Remove obsolete automation implementations when migration is safe.
- Update technical documentation and implementation progress records.

**You must execute R1 through R11 autonomously, in dependency order, and carry the project through to integrated acceptance.**

Do not stop after writing a plan.

Do not stop after completing R1.

Do not stop after creating migrations.

Do not stop after backend implementation.

Do not stop after frontend integration.

Continue until all increments have passed their defined acceptance criteria, or a genuine external blocker makes further safe progress impossible.

---

# 1. AUTHORITATIVE DOCUMENTS

Before modifying anything, read the latest versions of the following documents.

## Architecture and specifications

1. `docs/superpowers/specs/2026-11-03-zhanlu-automation-target-architecture.md`
2. `docs/superpowers/specs/2026-11-03-zhanlu-automation-technical-specification.md`
3. `docs/superpowers/specs/2026-11-03-zhanlu-automation-implementation-roadmap.md`

## Architecture decisions and acceptance

4. `docs/reports/2026-11-03-zhanlu-automation-architecture-decision-record.md`
5. `docs/reports/2026-11-03-zhanlu-automation-system-architecture-audit.md`
6. `docs/reports/2026-11-03-zhanlu-automation-final-architecture-review.md`
7. `docs/reports/2026-11-03-zhanlu-automation-final-correction-pass.md`
8. `docs/reports/2026-11-03-zhanlu-automation-final-reliability-verification.md`
9. `docs/reports/2026-11-03-zhanlu-automation-final-acceptance-matrix.md`

If filenames or locations differ, locate the actual latest documents. Do not create duplicate specifications.

The latest corrected technical specification is the normative engineering contract.

The roadmap determines implementation order.

The ADR determines approved architectural decisions.

The acceptance matrix determines verification requirements.

**Do not silently change approved architecture or weaken its reliability guarantees to make implementation easier.**

If actual repository constraints reveal an unavoidable conflict, investigate it, document the evidence, and choose the safest compatible solution. Do not silently invent a new architecture.

---

# 2. NON-NEGOTIABLE ARCHITECTURAL INVARIANTS

These principles govern the entire implementation.

## 2.1 Zhanlu remains an Agent Harness

Automation must be a governed capability built on top of the existing Zhanlu Agent Harness.

It must not become a separate AI platform.

Preserve:

- Production v3 Agent Runtime.
- FSM planning.
- ReAct and existing reasoning mechanisms.
- Existing tool registry.
- Tool RBAC.
- TraceContext.
- Existing ApprovalService.
- Existing dynamic datasource resolution.
- Multi-agent delegation.
- Peer collaboration.
- Existing provenance and execution observability mechanisms.

Automation must invoke the existing production agent capabilities through the approved Agent Invocation Contract.

Do not create a second planner, tool router, agent runtime, or authorization system.

## 2.2 Agent autonomy

The active agent determines:

- User intent.
- Execution plan.
- Required information.
- Datasource selection.
- Tool selection.
- Agent delegation.
- Clarification requirements.
- Execution depth.
- Verification requirements.
- Result completeness.

Do not introduce hardcoded keyword-to-tool routing, business-specific automation execution paths, or predetermined datasource selection.

## 2.3 Dynamic heterogeneous datasources

One automation execution may use multiple authorized heterogeneous datasources.

The harness exposes authorized SourceDescriptors and capabilities.

The agent determines which resources are needed.

No:
- Default datasource.
- First datasource assumption.
- Automatic query-all behavior.
- Hardcoded source selection.

## 2.4 Governance belongs to the runtime

Enforce authorization, project isolation, organization isolation, tool permissions, approvals, budgets, provenance, and side-effect policies in actual execution boundaries.

Never rely on system prompts as the security boundary.

Unattended automation must not acquire more authority than its approved scope.

## 2.5 Correctness over superficial completion

A green frontend, successful HTTP response, or mocked unit test does not establish that automation works.

Verify actual database persistence, scheduling, execution, agent invocation, recovery, and delivery.

---

# 3. INITIAL REPOSITORY PROTECTION AND INVESTIGATION

Before implementation:

1. Establish the actual repository root.
2. Inspect the current Git branch and HEAD.
3. Record the complete working-tree status.
4. Identify existing uncommitted user changes.
5. Inspect concurrent modifications.
6. Verify current Alembic head.
7. Inspect actual database and deployment configuration.
8. Identify running backend and frontend services.
9. Identify scheduler and worker processes.
10. Verify which automation implementation is currently active.

The previous investigation recorded approximately 249 uncommitted changes. The current number may differ.

**Preserve every existing user change.**

Rules:

- Never execute `git reset --hard`.
- Never execute `git clean -fd`.
- Never discard user modifications.
- Never overwrite foreign-dirty files without inspecting their changes.
- Never revert unrelated functionality.
- Never force-push.
- Never rewrite existing commit history.
- Never use destructive database operations.
- Never run migrations against production without explicit deployment authorization.

Work on the existing branch unless a safe, non-disruptive branch strategy is available.

Do not commit changes automatically. Keep changes reviewable.

Create a progress record:

`docs/reports/2026-11-03-zhanlu-automation-implementation-progress.md`

Update it after every increment.

Record:
- Increment.
- Files changed.
- Migration IDs.
- Tests executed.
- Test results.
- Bugs discovered.
- Remaining risks.
- Git state.

---

# 4. IMPLEMENTATION STRATEGY

Execute the approved R1–R11 roadmap.

Do not invent a replacement roadmap or change migration ownership without evidence.

For every increment:

1. Read its exact roadmap scope.
2. Inspect the relevant current implementation.
3. Identify dependencies.
4. Write or update failing tests first.
5. Implement the smallest coherent change.
6. Run targeted tests.
7. Run relevant regression tests.
8. Inspect the final diff.
9. Verify database and API contracts.
10. Update the progress report.
11. Proceed to the next increment once its acceptance criteria pass.

You may delegate independent read-only investigations or independent test reviews to subagents.

Do not delegate conflicting modifications to the same files.

Do not execute dependent migrations or implementation increments concurrently.

Do not skip verification gates.

---

# 5. R1 — P0 CORRECTNESS HOTFIXES

**Objective:** Fix critical existing automation correctness problems before larger architectural changes.

Implement the four approved hotfix areas.

## R1.1 Deletion status correctness

Fix the invalid `status="deleted"` behavior.

Ensure:
- All persisted status values satisfy the actual database constraints.
- Delete semantics are explicit.
- Soft deletion and lifecycle status are not confused.
- Existing PostgreSQL CHECK constraints are respected.
- API responses remain consistent.
- Existing records are handled safely.

## R1.2 Run-Now concurrency protection

Implement a database-backed in-flight guard.

Requirements:
- Concurrent Run-Now requests cannot start duplicate executions.
- Existing scheduled execution is detected.
- `force=true` follows the approved governance and audit contract.
- Correct HTTP conflict responses.
- Database transactions protect concurrency.
- Multi-process behavior is tested.

## R1.3 Artifact failure isolation

Artifact-rendering failures must not automatically turn a successfully executed agent task into a failed execution.

Separate:
- Agent execution outcome.
- Result persistence.
- Artifact generation.
- Artifact delivery.

Persist artifact failure information and expose it honestly.

## R1.4 Deletion and in-flight cancellation

When an automation is deleted:

- Prevent future scheduling.
- Cancel eligible pending occurrences.
- Request cancellation of in-flight executions.
- Preserve execution history.
- Prevent inappropriate post-deletion delivery.
- Reconcile irreversible external side effects.

Do not claim cancellation reverses an external operation already accepted.

## R1 acceptance

All four fixes must have:
- Regression tests.
- Database integration tests where applicable.
- API tests.
- Correct lifecycle behavior.

Run the existing automation regression suite.

Do not proceed with unresolved R1 failures.

---

# 6. R2 — DATABASE FOUNDATION

**Objective:** Establish the canonical persistence foundation for automation.

Follow the approved Alembic migration chain.

Previously verified baseline was revision 125, with migrations 126–131 allocated in the architecture specification. Recheck the actual current chain before creating anything.

Implement the migration assigned to R2.

Expected foundation includes:
- Automation drafts.
- Canonical automation definitions/specifications.
- Definition versioning.
- Ownership and project relationships.
- Unique constraints.
- Foreign keys.
- Indexes.
- Correct status constraints.
- Organization isolation.
- Compatibility with existing automation records.

Reuse existing models where appropriate.

Do not duplicate tables or create redundant state ownership.

## Important requirements

- Alembic is the only schema migration mechanism.
- No runtime `ALTER TABLE`.
- No automatic DDL at application startup.
- No conflicting migration revision IDs.
- No destructive legacy cleanup before reconciliation.
- All migration scripts must have a defensible downgrade strategy where possible.

Verify:
- Fresh database migration.
- Existing database upgrade.
- Model/schema consistency.
- Constraint behavior.
- Migration idempotency where applicable.

Do not execute migrations against production.

---

# 7. R3 — CANONICAL AUTOMATION CREATION LIFECYCLE

**Objective:** Eliminate the fragmented creation architecture.

Implement one authoritative server-side creation path.

## 7.1 Chat-based creation

User example:

"Every weekday at 08:00, synchronize ERP sales data, detect anomalies, and generate an HTML report."

The agent should:

1. Understand the request.
2. Determine the objective.
3. Identify missing information.
4. Ask clarification when necessary.
5. Produce a structured automation proposal.
6. Validate the proposal.
7. Present the review card.

The LLM may propose a specification but must not bypass confirmation.

## 7.2 Structured draft

Implement the canonical draft lifecycle.

A draft should include the approved specification fields:

- Name.
- Description.
- Objective.
- Project.
- Organization.
- Scheduling configuration.
- Trigger configuration.
- Output requirements.
- Notification preferences.
- Execution constraints.
- Approval policy.
- Version.
- Ownership.

Use the approved AutomationSpec schema.

Do not introduce arbitrary fields without updating the specification.

## 7.3 User confirmation

Implement the normative replay-first confirmation transaction.

Requirements:
- PostgreSQL transaction.
- Draft row locking.
- Unique confirmation-key constraint.
- Same-key replay returns the original definition.
- Different-key replay is rejected.
- Stale versions return HTTP 409.
- Concurrent requests create one definition.
- Crash-before-commit recovery.
- Crash-after-commit replay.

Test with actual PostgreSQL concurrency.

## 7.4 Retire competing creation paths

Inspect and safely retire:
- Legacy `create_resource` creation.
- Direct definition creation from LLM tools.
- Competing frontend persistence paths.

The new agent tool should propose a draft.

The confirmation endpoint should be the only authoritative definition-creation path.

Do not break unrelated resource creation.

## 7.5 Frontend creation experience

Implement:
- Pending automation preview.
- Editable specification.
- Schedule editor.
- Project selection.
- Output selection.
- Confirmation action.
- Error and conflict handling.
- Success response.

After successful confirmation, the same canonical definition must immediately appear in the Automation Center.

## R3 acceptance

Verify:
- Chat → draft.
- Draft → review.
- Review → confirmation.
- Confirmation → database definition.
- Definition → management page.
- Duplicate confirmation → same definition.
- Stale confirmation → rejected.
- Unauthorized confirmation → rejected.

No ghost tasks or duplicate definitions.

---

# 8. R4–R5 — SCHEDULING FOUNDATION

**Objective:** Build durable, correct, multi-instance scheduling.

Follow the approved scheduler design and ADRs.

The selected direction is PostgreSQL-backed durable scheduling with atomic claims, leases, fencing, and an advisory-lock-elected in-process leader.

Do not replace it with a new infrastructure platform without explicit justification.

## R4 — Trigger correctness

Implement:
- One-time triggers.
- Recurring schedules.
- Manual triggers.
- Timezone handling.
- DST behavior.
- Schedule modification.
- Schedule degradation.
- Misfire policies.
- Pause/resume.
- One-shot completion.

Eliminate silent schedule corruption.

Schedule parse errors must be visible and auditable.

Implement the approved deterministic misfire-collapse algorithm.

Enforce bounded recovery after prolonged downtime.

Never create an unbounded missed-execution backlog.

## R5 — Durable occurrences and scheduler

Implement:
- Canonical automation occurrence persistence.
- Separate workflow occurrence persistence.
- Shared scheduler mechanics.
- Atomic occurrence claims.
- Unique occurrence identity.
- Database leases.
- Lease owner.
- Lease expiry.
- Heartbeat.
- Fencing token.
- Stale worker rejection.
- Safe recovery.
- Multi-instance scheduler leadership.
- Missed-run recovery.
- Duplicate dispatch prevention.

Automation and workflow resources must retain separate identities and semantics.

Do not force workflow resources into automation definitions.

## R5.1 Event persistence

Implement the approved attempt-scoped event identity:

`UNIQUE(execution_id, attempt, seq)`

Critical events:
- Synchronous parent acknowledgement.
- ACK only after database commit.
- Duplicate replay protection.
- Per-attempt sequence tracking.
- Gap detection.

Telemetry:
- Bounded buffering.
- Counted drops.
- No false zero-loss guarantee.

Parent owns automation execution persistence.

## R5.2 Side-effect ledger

Implement `automation_side_effect_ops` according to the normative specification.

Every side-effecting operation must have:
- Execution identity.
- Attempt identity.
- Operation identity.
- Tool identity.
- Idempotency metadata.
- Reconciliation status.
- External reference where available.
- Audit information.

## Mandatory retry barrier

Before creating a retry after an expired execution:

Reconcile every outstanding side-effect operation.

| Outcome | Action |
|---|---|
| confirmed_complete | Preserve result; never repeat |
| confirmed_not_started | Safe to execute |
| unknown + idempotent | Retry under valid idempotency contract |
| unknown + non-idempotent | Block automatic retry; require reconciliation |

Missing execution output is not proof that no side effect occurred.

This requirement is mandatory for ERP, financial, database, and other enterprise mutations.

## R5 acceptance

Test:
- Multiple scheduler processes.
- Duplicate claims.
- Process restarts.
- Lease expiration.
- Stale workers.
- Concurrent modifications.
- Misfire recovery.
- DST.
- Workflow scheduling.
- Side-effect reconciliation.
- Critical event crash recovery.

Use real PostgreSQL integration tests.

---

# 9. R6–R7 — EXECUTION AND GOVERNANCE

**Objective:** Make automation execution a reliable consumer of the existing Agent Harness.

## R6 — Agent Invocation Contract and execution isolation

Implement the approved reusable Agent Invocation Contract.

The automation subsystem must not directly depend on internal HTTP chat streaming implementation details.

Preserve the production v3 runtime.

The invocation context must include:
- Automation definition ID.
- Execution ID.
- Attempt ID.
- User identity.
- Organization.
- Project.
- Authorized datasource descriptors.
- Tool permissions.
- Execution budget.
- Deadline.
- TraceContext.
- Cancellation signal.
- Approval context.

The active agent retains its ability to plan dynamically.

Do not create an automation-specific agent planner.

### Subprocess execution

Implement the approved execution isolation strategy.

Requirements:
- Process boundaries.
- Parent-owned automation persistence.
- Typed event communication.
- Durable critical-event acknowledgement.
- Cancellation propagation.
- Deadline enforcement.
- Hard termination where supported.
- Worker recovery.
- Fencing.
- Output persistence.
- Provenance preservation.

Do not assume Python threads can be forcibly terminated safely.

Do not claim a process kill reverses external side effects.

### Finalization

Implement:

`claimed → running → finalizing → terminal`

Persist output and provenance before finalization.

Implement fenced terminal writes.

Recovery must distinguish:
- Existing output requiring finalization replay.
- Running execution with no output requiring side-effect reconciliation.
- Terminal state already committed.
- Unknown external operation outcomes.

Do not create a new attempt before the mandatory reconciliation barrier.

## R7 — Governance and audit

Implement:
- Central authorization service.
- Project and organization isolation.
- Tool RBAC.
- Datasource authorization.
- Unattended execution scope.
- Approval integration.
- Approval expiration.
- Side-effect safety checks.
- Audit events.
- Provenance.
- Execution identity.

Keep authorization and retry safety as independent gates.

Human approval does not make a tool operation idempotent.

No prompt-based security enforcement.

## R6–R7 acceptance

Test:
- Existing production runtime parity.
- Dynamic datasource selection.
- Multiple datasource use.
- Tool authorization.
- Multi-agent delegation.
- Approval-required execution.
- Unattended execution.
- Cancellation.
- Hard worker termination.
- Critical event durability.
- Fencing.
- Side-effect reconciliation.
- Audit trail.
- Provenance on failure paths.

---

# 10. R8–R11 — INTEGRATION AND ENTERPRISE ACCEPTANCE

Complete the exact responsibilities in the approved roadmap.

## R8 — Schema reconciliation and legacy retirement

Implement:
- Existing task reconciliation.
- Legacy type normalization.
- Alembic-only schema enforcement.
- Runtime DDL removal.
- Definition compatibility.
- Safe retirement of obsolete fields and pathways.
- Data consistency verification.

Never delete historical execution records.

Never remove a legacy path until replacement behavior is verified.

## R9 — Delivery and artifact reliability

Implement:
- Durable delivery claims.
- Delivery event persistence.
- Delivery retries.
- Delivery status.
- Artifact storage.
- MinIO integration.
- Provenance.
- Notification handling.
- Failure isolation.

### Honest delivery guarantees

SMTP:
- At-least-once delivery-attempt semantics.
- Duplicate emails remain possible.
- Deterministic Message-ID is for identification and tracing, not guaranteed receiver deduplication.
- Timeout maps to unknown.
- Ambiguous outcomes require reconciliation.
- Manual retry is audited.

Chat/panel:
- Idempotent visible-state upserts where supported.

Do not rerun successful agent execution merely because optional delivery failed.

## R10 — Automation Center and frontend integration

Implement the complete management experience.

### Main Automation Center

Support:
- Month view.
- Card view.
- Table view.
- All Projects.
- Project filtering.
- Status filtering.
- Search.
- Pagination.
- Next-run time.
- Last execution.
- Success/failure statistics.
- Task creation.
- Task editing.
- Pause/resume.
- Run Now.
- Delete.
- Execution history.

### Task details

Show:
- Definition.
- Schedule.
- Execution status.
- Next run.
- Last run.
- Ownership.
- Project.
- Output configuration.

### Execution details

Show:
- Execution timeline.
- Agent planning.
- Agent activity.
- Tool calls.
- Datasource usage.
- Peer collaboration.
- Verification.
- Errors.
- Retry history.
- Side-effect reconciliation.
- Artifacts.
- Provenance.
- Delivery state.

### Frontend correctness

Use persisted backend state as the source of truth.

Do not rely on client-only task state.

Implement:
- Correct loading states.
- Empty states.
- Error states.
- Real-time updates.
- Pagination.
- Bounded polling.
- Terminal-state handling.
- Cancellation feedback.
- Permission-aware actions.

The Automation Center and chat must use the same canonical automation resource and API contracts.

## R11 — Complete enterprise acceptance

Follow the roadmap's final integration scope.

Execute full regression and end-to-end testing.

Test the system across:
- Fresh installation.
- Existing database upgrade.
- Single-process deployment.
- Multi-process deployment.
- Application restart.
- Worker crash.
- Scheduler crash.
- Database interruption.
- Datasource interruption.
- Agent timeout.
- Cancellation.
- Retry.
- Approval.
- Artifact generation.
- Delivery.
- Project isolation.
- Organization isolation.

Verify the entire lifecycle:

User request  
→ Agent understanding  
→ Structured draft  
→ Validation  
→ User confirmation  
→ Canonical definition  
→ Schedule registration  
→ Occurrence creation  
→ Atomic claim  
→ Execution context  
→ Agent planning  
→ Dynamic datasource/tool selection  
→ Agent execution  
→ Peer delegation where needed  
→ Verification  
→ Side-effect reconciliation  
→ Output persistence  
→ Finalization  
→ Artifact generation  
→ Delivery  
→ Audit and execution history.

This is the final acceptance target.

---

# 11. TESTING AND QUALITY REQUIREMENTS

Testing is mandatory throughout implementation.

Do not defer all testing to R11.

## Required testing layers

1. Unit tests.
2. Service tests.
3. API contract tests.
4. PostgreSQL integration tests.
5. Concurrency tests.
6. Multi-process tests.
7. Worker crash tests.
8. Scheduler recovery tests.
9. Agent integration tests.
10. Frontend component tests.
11. End-to-end tests.
12. Security tests.
13. Fault-injection tests.
14. Migration tests.

## Critical reliability scenarios

Explicitly test:

- Duplicate confirmation.
- Concurrent confirmation.
- Duplicate occurrence claim.
- Multiple scheduler instances.
- Worker death during execution.
- Worker death after external side-effect acceptance.
- Unknown side-effect reconciliation.
- Stale worker writes.
- Duplicate critical events.
- Cross-attempt sequence reuse.
- Event parent crash.
- Email timeout after provider acceptance.
- Duplicate delivery claim.
- Finalization crash.
- Cancellation after irreversible operation.
- Approval expiration.
- Organization access revocation.
- Application restart during execution.
- Artifact-render failure.
- Notification failure.

## Test execution discipline

For each increment:

- Write tests first.
- Run the failing test to establish the defect.
- Implement the fix.
- Run the targeted tests.
- Run related regression tests.
- Run broader backend tests where feasible.
- Run frontend tests where relevant.
- Record exact test counts and results.

Do not use mocks as substitutes for PostgreSQL concurrency tests.

Do not mark a test successful if it was skipped.

Do not conceal failing tests.

Do not weaken assertions just to make tests pass.

---

# 12. DATABASE AND MIGRATION SAFETY

Follow the approved migration chain.

Before every migration:
1. Inspect current Alembic head.
2. Inspect current schema.
3. Inspect foreign keys and constraints.
4. Verify the migration parent.
5. Check concurrent migration risks.
6. Verify existing data compatibility.

After every migration:
1. Inspect the resulting schema.
2. Verify indexes and constraints.
3. Test model compatibility.
4. Run relevant API and service tests.
5. Verify upgrade/downgrade behavior where supported.

Do not:
- Reset production databases.
- Drop existing data.
- Rewrite historical migrations already deployed.
- Introduce runtime DDL.
- Run destructive backfills without a verified plan.

Keep historical executions and provenance accessible.

---

# 13. FRONTEND AND BACKEND CONTRACT INTEGRITY

For every frontend feature:

- Identify its authoritative backend endpoint.
- Define request and response schemas.
- Validate authorization.
- Validate project scope.
- Handle empty and error responses.
- Handle concurrent updates.
- Handle stale versions.
- Ensure the frontend does not create hidden persistence paths.

For every API:
- Validate authentication.
- Validate input.
- Validate authorization.
- Validate transaction boundaries.
- Return stable error codes.
- Record appropriate audit events.
- Maintain backwards compatibility where required.

Do not build a frontend around mock data or placeholder responses.

---

# 14. OBSERVABILITY AND DEBUGGING

Every automation must be diagnosable.

Maintain linked identifiers:

- Automation definition ID.
- Draft ID.
- Occurrence ID.
- Execution ID.
- Attempt ID.
- AgentRun ID.
- Conversation ID.
- Tool invocation ID.
- WorkflowRun ID where relevant.
- Artifact ID.
- Delivery event ID.
- Audit event ID.

Provide structured logs and persisted execution events.

Make it possible to answer:

- Why did this automation run?
- Who created it?
- Who confirmed it?
- Which schedule triggered it?
- Which agent was invoked?
- Which datasources were accessed?
- Which tools were executed?
- Which operations changed external systems?
- Which approvals were obtained?
- Why did execution fail?
- Was a retry safe?
- What outputs were generated?
- Were they delivered?

Do not expose credentials, secrets, or sensitive payloads in logs.

---

# 15. AUTONOMOUS EXECUTION RULES

I want you to work autonomously.

You do not need to ask me routine questions about implementation details already established in the specifications.

You should make reasonable engineering decisions within the approved architecture.

However, autonomy does not mean ignoring safety.

### You may proceed without asking for approval when:
- Implementing an approved increment.
- Adding tests.
- Fixing defects within the increment.
- Refactoring affected modules.
- Updating API contracts.
- Creating approved migrations.
- Improving logging and error handling.
- Resolving ordinary integration problems.

### Stop and report when:
- An external credential or permission is missing.
- A destructive production operation would be required.
- A migration could cause irreversible data loss.
- An architecture change is genuinely necessary.
- A security boundary would need to be weakened.
- A business policy decision cannot be safely inferred.
- Concurrent user changes create a risk of overwriting their work.

When blocked:
- Complete independent safe work.
- Document the blocker.
- Explain the evidence.
- Identify the minimum decision required.
- Do not fabricate success.

Do not stop merely because one test needs debugging. Investigate and fix it.

---

# 16. IMPLEMENTATION COMPLETION CRITERIA

The project is not complete merely because R1–R11 have been started.

Every increment must have:
- Implemented functionality.
- Relevant tests.
- Passing acceptance criteria.
- Updated documentation.
- No unresolved critical regression.

The final system must demonstrate:

### Creation
- One canonical creation path.
- Server-side drafts.
- Explicit confirmation.
- Database-enforced idempotency.

### Scheduling
- Durable occurrences.
- Correct timezone behavior.
- Bounded misfire recovery.
- Multi-instance concurrency safety.

### Execution
- Existing Agent Harness integration.
- Dynamic authorized datasource selection.
- Governed tool execution.
- Multi-agent support.
- Execution isolation.
- Reliable cancellation.
- Fenced recovery.

### Side effects
- Durable side-effect ledger.
- Reconciliation before retry.
- No unsafe automatic re-execution of unknown non-idempotent operations.

### Governance
- Correct RBAC.
- Project and organization isolation.
- Approval enforcement.
- Audit events.
- Provenance.

### Delivery
- Durable delivery tracking.
- Honest delivery guarantees.
- Artifact storage.
- Independent notification failure handling.

### Management
- Fully integrated Automation Center.
- Accurate execution history.
- Correct live status.
- Real persisted data.

### Operations
- Safe application restarts.
- Multi-process execution.
- Recovery after worker failure.
- No unbounded retries.
- No silent state corruption.

---

# 17. FINAL DELIVERABLES

Create or update:

1. `docs/reports/2026-11-03-zhanlu-automation-implementation-progress.md`
2. `docs/reports/2026-11-03-zhanlu-automation-final-implementation-report.md`
3. Updated technical specification reflecting actual implementation.
4. Updated acceptance matrix.
5. Migration and deployment instructions.
6. Operational troubleshooting documentation.
7. Relevant API documentation.
8. Test and regression results.

The final implementation report must include:

- R1–R11 completion table.
- Files changed.
- Database migrations.
- API changes.
- Frontend changes.
- Agent Harness integration.
- Scheduler architecture.
- Execution architecture.
- Governance implementation.
- Test counts and actual results.
- Known limitations.
- Outstanding issues.
- Deployment instructions.
- Rollback strategy.
- Git branch and working-tree status.

Distinguish:
- Implemented.
- Tested.
- Integration-tested.
- Production-validated.

Never claim production validation unless it was actually performed.

---

# 18. FINAL EXECUTION DIRECTIVE

Start immediately.

Work through R1, R2, R3, R4, R5, R6, R7, R8, R9, R10, and R11 in the approved dependency order.

You are responsible for carrying the implementation through completion.

Do not return with another architecture proposal.

Do not merely describe what needs to be done.

Do not stop after writing documentation.

Do not leave unfinished placeholder implementations.

Do not claim completion when only mocked tests pass.

Do not skip acceptance criteria.

Do not replace the existing Agent Harness.

Do not discard user changes.

**Build the actual working system.**

At the end, provide a clear report identifying:
- What is fully implemented.
- What is verified by tests.
- What remains blocked.
- What needs deployment.
- Whether the entire R1–R11 acceptance target has passed.

The desired final result is:

`ZHANLU AI AUTOMATION — R1–R11 IMPLEMENTATION COMPLETE`

If an external blocker prevents full completion, report the exact blocker and complete every other safe, independent increment.

**Proceed autonomously and finish the entire project.**