# ZHANLU — ENTERPRISE AI OPERATING SYSTEM
# Manus / ChatGPT-Level Agent Intelligence, Autonomous Execution & Hybrid Multi-Agent Collaboration

## 0. Mission and product vision

We are evolving Zhanlu into a comprehensive Enterprise AI Operating System.

The goal is to deliver an AI experience comparable in capability to Manus and ChatGPT, while specializing in enterprise operations, organizational knowledge, departmental workflows, governed data access, and multi-agent collaboration.

Zhanlu should not behave like a conventional chatbot that simply receives a question and generates text.

It should behave like an intelligent AI working environment that can:

- Understand natural-language instructions and business objectives.
- Maintain conversational and project context.
- Research and retrieve information from enterprise resources.
- Reason about complex objectives.
- Develop and adapt execution plans.
- Discover and select appropriate tools, skills, agents and data sources.
- Execute tasks through APIs, databases, documents and other connected systems.
- Delegate work to autonomous specialist agents.
- Coordinate multiple agents and teams.
- Observe intermediate results and recover from failures.
- Verify findings and actions.
- Generate reports, spreadsheets, presentations and other artifacts.
- Execute long-running workflows.
- Remember relevant project instructions and decisions.
- Request user clarification or approval when necessary.
- Provide transparent execution progress and final results.

The user should interact primarily through one conversational interface.

The General Assistant should understand the request and manage the necessary work. Users should not need to understand the internal agent architecture to get things done.

**Core product principle: Users express goals. Zhanlu determines and executes the appropriate workflow.**

We are not building a copy of Manus or ChatGPT. We are using their publicly documented product capabilities as benchmarks and applying them to an enterprise environment.

---

# PART A — MANDATORY EXTERNAL RESEARCH

## 1. Research the official reference materials

Before modifying code, visit and read the actual content of every reference below.

Do not rely solely on page titles, search snippets or assumptions.

### IBM — Agent architecture and reasoning

1. Multi-agent systems:
https://www.ibm.com/think/topics/multiagent-system

Study:
- Autonomous agents.
- Hierarchical agent structures.
- Distributed agent structures.
- Agent responsibilities.
- Cooperation and coordination.
- Distributed problem-solving.
- Advantages and limitations of multi-agent architectures.

2. Multi-agent collaboration:
https://www.ibm.com/think/topics/multi-agent-collaboration

Study:
- Task decomposition.
- Responsibility assignment.
- Shared environment and shared memory.
- Inter-agent communication.
- Sequential and concurrent execution.
- Dynamic collaboration.
- Conflict resolution.
- Collaborative planning.
- Result aggregation.

3. ReAct agents:
https://www.ibm.com/think/topics/react-agent

Study:
- Reasoning and action interleaving.
- Tool selection.
- Observations.
- Iterative execution.
- Feedback loops.
- Stopping conditions.
- Multi-agent ReAct patterns.

4. Agentic reasoning:
https://www.ibm.com/think/topics/agentic-reasoning

Study:
- Planning.
- Reasoning engines.
- Decision-making.
- Tool calling.
- Search and evaluation.
- Adaptive reasoning.
- Self-reflection and feedback.

5. Agentic workflows:
https://www.ibm.com/think/topics/agentic-workflows

Study:
- Dynamic workflows.
- Problem understanding.
- Planning and execution.
- Adaptive tool usage.
- Iteration.
- Human feedback.
- Multi-agent integration.

6. A2A protocol:
https://www.ibm.com/think/topics/agent2agent-protocol

Study:
- A2A client and server.
- Agent Cards.
- Agent discovery.
- Task lifecycle.
- Messages and artifacts.
- Authentication and authorization.
- Streaming.
- Long-running tasks.
- Asynchronous updates.

### Manus — Product benchmark

7. Official website:
https://manus.im/

8. Official API documentation:
https://open.manus.ai/docs/v2/introduction

Study the documented product capabilities:

- Autonomous task creation and execution.
- Multi-turn task continuation.
- Project-level shared instructions.
- File attachments and generated files.
- Webhooks and completion notifications.
- Skills and custom agents.
- Long-running task management.
- Task status and result retrieval.

Explore additional official documentation linked from the documentation index where relevant.

### OpenAI — Product and engineering benchmark

9. ChatGPT agent product overview:
https://openai.com/index/introducing-chatgpt-agent/

10. Current ChatGPT agent help documentation:
https://help.openai.com/en/articles/11752874-chatgpt-agent

11. OpenAI agent development documentation:
https://developers.openai.com/tracks/building-agents

Study:

- Research and action within one conversational experience.
- Tool selection.
- Web search and browser interaction.
- Computer use.
- Code execution.
- File handling.
- Connected applications.
- Multi-step task execution.
- Background and long-running work.
- User steering and interruption.
- Approval checkpoints.
- Multi-agent architecture.
- Sessions and execution state.
- Tool integration.
- Tracing and evaluation.
- Recovery and error handling.

### Research deliverable

Create:

`docs/architecture/zhanlu-manus-chatgpt-capability-benchmark.md`

Include:

1. Reference product capabilities.
2. Relevant architectural principles.
3. Zhanlu's current implementation.
4. Missing capabilities.
5. Capabilities that already exist but need improvement.
6. Recommended implementation.
7. Dependencies and risks.
8. Priority.

Use a capability matrix:

| Capability | Manus | ChatGPT | Zhanlu current | Gap | Priority |
|---|---|---|---|---|---|

Do not claim that Manus or ChatGPT use an internal architecture unless supported by public documentation.

Separate publicly documented product behavior from architectural recommendations and assumptions.

---

# PART B — UNDERSTAND THE EXISTING ZHANLU ARCHITECTURE

## 2. Audit before implementation

Inspect the actual repository and previous progress documents.

The following foundation has already been implemented and must be preserved:

- General Assistant.
- Department/project context.
- Agent discovery.
- Published department agents.
- AgentApp-aware delegation.
- Structured Agent Task Contract.
- Collaboration Broker.
- Hybrid Pattern C peer collaboration.
- DAG agent dispatch.
- Heterogeneous batch execution.
- Shared evidence.
- Agent invocation hierarchy.
- Distributed governance store.
- Cycle detection and duplicate prevention.
- Depth, child, concurrency and timeout budgets.
- Token metering and budget enforcement.
- Child cancellation.
- Invocation auditing.
- Execution-tree REST API.
- LiveActivityStream.
- AgentExecutionTree.
- Evidence visualization.
- Security sanitization.
- Ontology and governed metric architecture.
- Enterprise data retrieval and NL2SQL.
- Existing project-level resources and shared memory.

Inspect:

- Backend runtime.
- Agent discovery.
- Agent prompts and configurations.
- Tool registry.
- Skills registry.
- MCP integration.
- A2A-related components.
- Collaboration Broker.
- Task contracts.
- DAG and ReAct execution.
- Memory and session management.
- Project and department permissions.
- Database models.
- SSE events.
- Frontend chat experience.
- Execution visualization.
- Report generation and artifacts.
- Automation infrastructure.

For every feature, classify it as:

- IMPLEMENTED AND VERIFIED.
- IMPLEMENTED BUT NOT VERIFIED.
- PARTIALLY IMPLEMENTED.
- UI ONLY.
- DOCUMENTED BUT NOT IMPLEMENTED.
- MISSING.

Do not trust old progress reports without checking the current code.

Do not rebuild working mechanisms.

---

# PART C — THE GENERAL ASSISTANT AS THE INTELLIGENT CORE

## 3. One conversational entry point

The General Assistant is the user's primary interface to Zhanlu.

Users can select:

- A department/project.
- A model if the product supports model selection.
- Additional resources or files.
- An optional skill.
- An optional specific agent when they explicitly want one.

However, selecting a department should not force users to select an individual agent.

For example:

The user selects Sales & Dealer Management and asks:

"Analyze our sales performance this quarter and explain which products and dealers are underperforming."

The General Assistant should understand the objective and manage the complete workflow.

It should automatically discover the relevant capabilities and specialists.

The user should not have to manually assemble a team.

## 4. General Assistant responsibilities

The General Assistant must be capable of:

1. Intent understanding.
2. Context resolution.
3. Business objective identification.
4. Requirement extraction.
5. Data-source discovery.
6. Tool and skill discovery.
7. Agent capability discovery.
8. Task planning.
9. Task decomposition.
10. Execution strategy selection.
11. Direct tool execution.
12. Specialist delegation.
13. Peer-to-peer collaboration.
14. Intermediate result evaluation.
15. Adaptive replanning.
16. Evidence aggregation.
17. Result verification.
18. Final response synthesis.
19. Artifact generation.
20. Execution-state reporting.

The General Assistant is responsible for the overall user outcome, not just forwarding a prompt to another agent.

It should maintain the original user request as the authoritative objective throughout execution.

---

# PART D — INTELLIGENT TASK UNDERSTANDING AND PLANNING

## 5. Intent understanding

Do not implement a keyword-only router.

The General Assistant should interpret the request using:

- Current user message.
- Previous conversation.
- Selected project.
- Selected department.
- Shared project memory.
- Available resources.
- Connected enterprise data.
- User permissions.
- Existing task state.
- Relevant agent capabilities.

Distinguish between:

- Conversational intent.
- Information retrieval.
- Data analysis.
- Research.
- Problem investigation.
- Decision support.
- Report creation.
- Operational execution.
- Multi-step project work.
- Cross-department objectives.

A request can contain several intents.

For example:

"Find out why production costs increased, compare the impact on our product margins, and prepare a report for management."

This is a connected business objective requiring several subtasks, not three unrelated chatbot questions.

## 6. Dynamic planning engine

Introduce or improve a structured planner using the existing runtime.

The planner should produce an execution plan containing:

- Goal.
- User intent.
- Context.
- Constraints.
- Required capabilities.
- Candidate agents.
- Candidate tools and skills.
- Data requirements.
- Task list.
- Task dependencies.
- Parallel execution opportunities.
- Expected results.
- Evidence requirements.
- Acceptance criteria.
- Risk level.
- Required approvals.
- Time and token budgets.

Use structured output with validation.

The plan must be executable, observable and recoverable.

### Planning example

User:

"Investigate why vehicle sales dropped last month and recommend how we can improve next month's sales."

Possible plan:

| Task | Responsible capability | Dependency |
|---|---|---|
| Retrieve sales performance | Sales Agent / NL2SQL | None |
| Compare product performance | Sales Agent | Sales data |
| Analyze dealer performance | Sales Agent | Sales data |
| Investigate inventory availability | Inventory Agent | Sales findings |
| Analyze demand trends | Demand Forecasting Agent | Sales history |
| Identify supply constraints | Supply Chain Agent | Inventory findings |
| Synthesize business causes | General Assistant | Specialist results |
| Verify recommendations | General Assistant / Reviewer | Evidence |

The planner decides which tasks are necessary and which can run in parallel.

If the initial results reveal a new issue, it can create additional subtasks.

The plan is not a fixed workflow template.

It is a dynamically constructed execution strategy.

## 7. Adaptive planning

The General Assistant must not assume that the initial plan is always correct.

After observing intermediate results, it should evaluate:

- Is the original objective satisfied?
- Is the evidence sufficient?
- Are there contradictions?
- Is a data source missing?
- Is another specialist required?
- Should a task be retried?
- Should the plan be modified?
- Can the task be completed with fewer resources?

Support controlled replanning.

Preserve previous decisions and execution history.

Do not restart the entire workflow unnecessarily.

---

# PART E — HYBRID MULTI-AGENT ORCHESTRATION

## 8. Preserve collaboration model C

Zhanlu uses Hybrid Orchestration with Pattern C peer-to-peer collaboration.

Keep the existing architecture:

- General Assistant as primary supervisor.
- Existing ReAct execution runtime.
- Existing DAG executor.
- Existing Collaboration Broker.
- Existing Agent Task Contract.
- Existing agent discovery.
- Existing invocation and governance systems.

Do not introduce another orchestration framework.

Support three execution patterns.

### Pattern A — Direct execution

The General Assistant executes using its own tools, skills, enterprise data access and available capabilities.

### Pattern B — Supervisor-led delegation

The General Assistant assigns specific tasks to relevant specialists.

Specialists may work sequentially or concurrently.

### Pattern C — Peer collaboration

A specialist can request another specialist's assistance through the Collaboration Broker.

Example:

Sales Agent discovers that inventory shortages may explain reduced sales.

It requests Inventory Agent to investigate.

Inventory Agent retrieves inventory evidence and reports its findings.

Sales Agent incorporates the findings and completes its analysis.

The General Assistant receives the results and produces the final response.

### Strategy selection

The system dynamically chooses the appropriate pattern based on:

- User objective.
- Task dependencies.
- Required expertise.
- Data access.
- Tool availability.
- Expected result.
- Evidence requirements.
- Cost.
- Latency.
- Risk.
- Permissions.

The system should not maximize agent count.

It should maximize successful task completion.

## 9. Autonomous specialist agents

Each department may contain multiple specialist agents.

Examples:

**Sales**
- Sales Performance Agent.
- Dealer Analysis Agent.
- Customer Intelligence Agent.
- Revenue Analysis Agent.

**Manufacturing**
- Production Scheduling Agent.
- Quality Control Agent.
- Predictive Maintenance Agent.
- Capacity Planning Agent.

**Procurement and Supply Chain**
- Inventory Management Agent.
- Supplier Risk Agent.
- Supply Chain Optimization Agent.

**Finance**
- Financial Analysis Agent.
- Cost Analysis Agent.
- Margin and Profitability Agent.
- Financial Report Generator.

**HR**
- Workforce Analysis Agent.
- Employee Data Assistant.
- Recruitment Analysis Agent.

Agents should have:

- Defined responsibilities.
- Clear operational boundaries.
- Their own system prompts.
- Configured tools.
- Configured skills.
- Authorized knowledge bases.
- Department context.
- Execution budgets.
- Structured input and output.
- Access policies.
- Evaluation criteria.

Specialists should not duplicate general-purpose capabilities unnecessarily.

Share common domain intelligence through reusable tools and skills.

---

# PART F — A2A AND MCP INTEGRATION

## 10. Agent communication architecture

Distinguish the protocols:

**MCP**
- Agent-to-tool communication.
- Database and enterprise integrations.
- External services.
- Tool discovery and invocation.

**A2A**
- Agent-to-agent communication.
- Agent discovery.
- Capability exchange.
- Task assignment.
- Status reporting.
- Results and artifacts.

Use the existing internal Collaboration Broker as the initial execution boundary.

Design it to support standard A2A interoperability.

### A2A-compatible concepts

Implement or map:

- Agent Card.
- Agent identity.
- Agent capabilities.
- Supported skills.
- Task.
- Message.
- Artifact.
- Task status.
- Authentication.
- Authorization.
- Streaming.
- Long-running execution.

The task lifecycle should distinguish states such as:

- submitted
- working
- input-required
- completed
- failed
- cancelled

Ensure internal states map correctly to protocol-level states.

Do not claim full A2A protocol compliance merely because internal agent delegation exists.

If external A2A federation is required, implement it as a separate, feature-gated capability.

---

# PART G — REACT EXECUTION AND RELIABILITY

## 11. ReAct execution loop

Build on the existing ReAct runtime.

The runtime should support:

1. Understand the active objective.
2. Evaluate current context.
3. Select an appropriate action.
4. Execute a tool, skill or agent task.
5. Observe the actual result.
6. Evaluate the result against acceptance criteria.
7. Decide whether to continue, delegate, retry, replan or finish.
8. Verify the outcome.
9. Synthesize the response.

Persist structured execution events and decisions.

Do not expose private chain-of-thought.

Store:

- Intent summary.
- Plan.
- Task assignments.
- Action summaries.
- Tool inputs and safe outputs.
- Observations.
- Evidence references.
- Evaluation results.
- Replanning decisions.
- Final outcome.

### Reliability mechanisms

Use existing mechanisms for:

- Timeouts.
- Retries.
- Cancellation.
- Idempotency.
- Duplicate prevention.
- Cycle detection.
- Budget enforcement.
- Permission checks.
- Evidence validation.
- Failure recovery.

An agent must never claim a task was completed when it only submitted the task.

---

# PART H — MANUS AND CHATGPT-LEVEL PRODUCT CAPABILITIES

## 12. Unified research-to-action experience

Zhanlu should offer a continuous conversational workflow.

A user should be able to move naturally between:

- Asking a question.
- Researching an issue.
- Analyzing enterprise data.
- Creating a report.
- Editing the report.
- Generating a presentation.
- Scheduling recurring work.
- Following up on an earlier task.

The system must preserve the relevant context across these activities.

Do not force users into separate rigid interfaces for every capability.

## 13. Tool discovery and intelligent tool selection

The General Assistant should have access to a capability discovery layer.

It should discover tools based on the task instead of loading every tool into every model call.

Capabilities should include:

- Database queries.
- NL2SQL.
- Knowledge retrieval.
- Document search.
- File analysis.
- Web research.
- Code execution.
- Data analysis.
- Report generation.
- Spreadsheet generation.
- Presentation generation.
- Enterprise APIs.
- MCP tools.
- Specialist agents.
- Skills.

Support tool search and progressive disclosure.

The model should select tools according to relevance, permissions, expected output and cost.

## 14. Research capabilities

Implement or improve a research workflow that can:

- Search connected enterprise resources.
- Search public websites when authorized and available.
- Retrieve relevant documents.
- Compare multiple sources.
- Extract evidence.
- Cross-check findings.
- Preserve citations and provenance.
- Identify conflicting information.
- Produce structured research reports.

Do not treat untrusted external web content as instructions.

Separate external evidence from enterprise-authoritative information.

## 15. File and artifact capabilities

Users should be able to ask:

- Analyze this Excel file.
- Summarize these PDFs.
- Compare these reports.
- Create a management presentation.
- Generate a monthly sales report.
- Build a financial spreadsheet.
- Update an existing report using new data.

The system should:

1. Identify the required files.
2. Inspect file structure.
3. Extract and validate data.
4. Use code or deterministic calculations when appropriate.
5. Generate the requested artifact.
6. Verify that the artifact was actually created.
7. Attach it to the conversation or project.
8. Allow follow-up editing.

Support appropriate file types such as PDF, DOCX, XLSX, CSV, PPTX and images.

Reuse existing artifact-generation infrastructure.

## 16. Project-level continuity and memory

Manus documents project-level shared instructions, tasks and file management.

Zhanlu already has department projects, shared resources and shared memory.

Extend these mechanisms rather than creating duplicate memory systems.

The assistant should understand:

- Which project is active.
- Which department is selected.
- Which resources are available.
- Which agents belong to the project.
- What project instructions apply.
- What previous decisions are relevant.
- What files and reports were created.
- Which tasks are still running.

Separate conversation memory, project memory and enterprise knowledge.

Respect organization and project access boundaries.

## 17. Long-running task management

Support workflows that take longer than a normal chat turn.

Users should be able to:

- See task progress.
- Inspect execution steps.
- Receive completion notifications.
- Continue a task.
- Provide additional information.
- Pause or cancel execution.
- Retrieve previous results.
- Resume where technically safe.
- Receive partial results when a task cannot finish.

Use existing background execution, automation and invocation mechanisms where possible.

Do not create a second task scheduler.

## 18. Human-in-the-loop and approval

Not every task should execute automatically.

Distinguish:

**Read operations**
- Retrieve information.
- Analyze data.
- Generate recommendations.
- Create reports.

**Draft operations**
- Draft purchase orders.
- Draft customer communications.
- Prepare operational changes.

**Consequential operations**
- Submit orders.
- Modify business records.
- Send external communications.
- Execute financial or operational actions.

Require appropriate confirmation and authorization for consequential operations.

A user confirmation must not bypass RBAC or ontology governance.

---

# PART I — ENTERPRISE-SPECIFIC INTELLIGENCE

## 19. Department and resource architecture

Zhanlu is an enterprise platform, not a general-purpose consumer assistant.

Preserve the current organization:

Organization
  └── Department Projects
       ├── Department Resources
       ├── Department Shared Memory
       ├── Department Agents
       ├── Department Skills
       ├── Department Conversations
       ├── Department Automations
       └── Department Access Policies

Users select a department/project in the chat.

The General Assistant uses that context to understand available enterprise information and authorized capabilities.

A department agent must not automatically access another department's private resources.

Cross-department access must follow explicit organizational permissions and ontology policies.

## 20. Shared enterprise intelligence

Develop reusable capabilities rather than implementing every department as an isolated system.

Shared components should include:

- Governed ontology.
- Metric definitions.
- Entity resolution.
- Enterprise knowledge retrieval.
- NL2SQL.
- Forecasting.
- Business calculations.
- Evidence compilation.
- Report generation.
- Approval workflows.
- Audit and provenance.

Department agents consume these shared capabilities using their authorized bindings.

Maintain the rule:

**LLMs coordinate and explain. Deterministic services compute authoritative business figures.**

---

# PART J — EXECUTION VISUALIZATION AND CHAT EXPERIENCE

## 21. Conversational execution experience

Improve the current chat interface without making it look like a technical orchestration dashboard.

The user should see a natural, understandable execution experience.

During a complex task, display meaningful progress such as:

- Understanding your request.
- Planning the analysis.
- Reviewing available enterprise data.
- Analyzing sales performance.
- Checking inventory conditions.
- Comparing specialist findings.
- Verifying the results.
- Preparing your report.

These labels must correspond to actual backend events.

Do not invent progress messages.

## 22. Agent execution tree

When specialist agents are actually used, show:

- Real agent name.
- Agent responsibility.
- Assigned objective.
- Parent agent.
- Child agents.
- Current state.
- Progress.
- Duration.
- Result summary.
- Evidence.
- Errors and limitations.

Display peer collaboration as a real relationship.

Avoid generic labels such as "sub-agent" when the actual agent identity is available.

When direct execution occurs without child agents, do not render a misleading multi-agent panel.

## 23. Fix execution lifecycle consistency

Investigate the existing issue where the General Assistant produces its final answer while several child agents remain RUNNING.

Trace:

- Parent invocation.
- Child invocation.
- Parent-child IDs.
- Task contract.
- Database status.
- Background execution.
- SSE event lifecycle.
- REST execution tree.
- Frontend state merging.

Enforce:

1. Every started child reaches a truthful terminal state.
2. Required child tasks resolve before parent completion.
3. Partial results are represented as partial.
4. Failed and cancelled tasks remain visible.
5. Duplicate events cannot create duplicate branches.
6. Out-of-order events cannot regress terminal status.
7. Reconnection restores actual persisted state.
8. The UI never fabricates completion.

---

# PART K — ENTERPRISE SCENARIO EVALUATION

## 24. Required scenarios

Test across all major departments.

### Sales
- Sales performance.
- Dealer comparisons.
- Revenue trends.
- Customer behavior.
- Product performance.

### Manufacturing
- Production planning.
- Capacity.
- Equipment maintenance.
- Quality.
- Bottlenecks.

### Procurement
- Supplier risk.
- Procurement performance.
- Material shortages.
- Delivery performance.

### Inventory
- Stockout risk.
- Overstock.
- Replenishment.
- Inventory optimization.

### Finance
- Revenue.
- Costs.
- Margin.
- Profitability.
- Financial reporting.

### Marketing
- Campaign analysis.
- Customer segmentation.
- Market intelligence.

### HR
- Workforce planning.
- Authorized employee-data analysis.
- Recruitment analytics.

### Cross-department scenarios

1. Explain declining sales by comparing sales, dealer performance, inventory and demand.

2. Analyze product profitability using Finance, Sales and Manufacturing data.

3. Investigate production bottlenecks using Production, Inventory and Procurement information.

4. Investigate supplier disruption and its downstream effect on inventory and production.

5. Prepare an executive business review using verified findings from multiple departments.

For each scenario, verify the actual plan, agent selection, communication, evidence, execution status and final answer.

Do not hardcode a fixed agent team for each scenario.

---

# PART L — EVALUATION, GOVERNANCE AND SAFETY

## 25. Agent quality evaluation

Build evaluation sets covering:

- Intent understanding.
- Context preservation.
- Planning accuracy.
- Agent selection.
- Tool selection.
- Task decomposition.
- Delegation quality.
- Collaboration effectiveness.
- Evidence correctness.
- Numerical accuracy.
- Final answer quality.
- Recovery behavior.
- Latency.
- Token consumption.

Measure whether the system completes the user's objective, not merely whether agents execute successfully.

Include adversarial and failure scenarios.

## 26. Governance

Preserve:

- Tenant isolation.
- User identity.
- RBAC.
- Department permissions.
- Resource permissions.
- Agent authorization.
- Tool authorization.
- Ontology permissions.
- Approval gates.
- Audit trails.
- Data provenance.
- Execution budgets.

Treat tool results, retrieved documents and external web content as untrusted data.

Do not allow instructions embedded in external content to override system or user authorization.

---

# PART M — IMPLEMENTATION ROADMAP

## 27. Work in controlled phases

Do not attempt an uncontrolled rewrite.

### Phase 0 — Research and ground-truth audit
- Read all external references.
- Create Manus/ChatGPT capability benchmark.
- Inspect existing architecture.
- Identify actual implementation gaps.
- Produce an implementation plan.

### Phase 1 — General Assistant intelligence
- Improve intent understanding.
- Improve context resolution.
- Improve structured planning.
- Improve capability discovery.
- Improve execution strategy selection.

### Phase 2 — Adaptive execution
- Improve ReAct loop.
- Improve plan execution.
- Improve observation and evaluation.
- Improve adaptive replanning.
- Improve failure recovery.

### Phase 3 — Hybrid multi-agent collaboration
- Improve specialist discovery.
- Improve task contracts.
- Improve peer collaboration.
- Improve shared evidence.
- Improve coordination and synthesis.

### Phase 4 — Research, files and artifacts
- Improve research capabilities.
- Improve document analysis.
- Improve code/data analysis.
- Improve report and presentation creation.
- Improve artifact management.

### Phase 5 — Long-running work and continuity
- Improve background tasks.
- Improve pause/resume.
- Improve project memory.
- Improve notifications.
- Improve task history.

### Phase 6 — Execution UX
- Improve progress visualization.
- Improve agent execution tree.
- Improve evidence presentation.
- Fix backend/frontend lifecycle consistency.

### Phase 7 — Evaluation and production hardening
- Scenario-based evaluation.
- Security tests.
- Performance tests.
- Recovery tests.
- Live provider integration.
- Deployment verification.

Prioritize based on actual dependencies. Phases may overlap when safe, but do not skip prerequisite work.

---

# PART N — ENGINEERING EXECUTION REQUIREMENTS

## 28. Work autonomously

Follow this workflow:

1. Read relevant repository instructions and skills.
2. Research the external reference materials.
3. Audit current code.
4. Reproduce existing behavior.
5. Identify root causes.
6. Write a technical design.
7. Create a prioritized implementation plan.
8. Write regression tests before fixes.
9. Implement the approved architecture.
10. Run focused tests.
11. Run relevant broader regressions.
12. Build the frontend.
13. Restart and verify the runtime where possible.
14. Run live tests when provider and infrastructure permit.
15. Update documentation.
16. Report the actual result.

Do not stop after writing the research report or implementation plan.

Continue implementing the highest-priority approved work.

Do not silently skip difficult parts.

If a genuine external dependency blocks a task, document the exact blocker and continue with independent work.

## 29. Engineering constraints

- Preserve existing working architecture.
- Reuse the existing ReAct runtime.
- Reuse the existing Collaboration Broker.
- Reuse the existing task contract.
- Reuse the existing governance store.
- Reuse existing agent discovery.
- Reuse existing project and department models.
- Reuse existing ontology and NL2SQL.
- Reuse existing report and artifact infrastructure.
- Reuse existing execution visualization.
- Use feature flags for risky behavior changes.
- Preserve backward compatibility.
- Avoid unnecessary migrations.
- Avoid duplicated abstractions.
- Avoid another orchestrator or registry.
- Do not bypass authorization.
- Do not claim tests passed unless actually executed.
- Do not claim live verification when only mocks were used.

If existing implementations conflict with the target architecture, document the conflict and choose the smallest safe correction.

---

# PART O — FINAL DELIVERABLES

## 30. Required documentation

Produce:

1. `docs/architecture/zhanlu-manus-chatgpt-capability-benchmark.md`
2. `docs/architecture/zhanlu-general-assistant-intelligence-architecture.md`
3. `docs/architecture/zhanlu-hybrid-multi-agent-execution-design.md`
4. `docs/architecture/zhanlu-agentic-platform-implementation-roadmap.md`
5. Updated orchestration progress log.

## 31. Final acceptance criteria

Zhanlu should deliver a coherent enterprise AI experience in which:

- Users communicate naturally through one General Assistant.
- The assistant understands the complete user objective.
- It remembers relevant conversation and project context.
- It dynamically plans tasks.
- It discovers tools, skills and specialist agents.
- It can execute work directly.
- It can delegate work to specialists.
- Specialists can collaborate with each other.
- It can research, analyze and create artifacts.
- It can observe and evaluate results.
- It can adapt plans when necessary.
- It can recover from failures.
- It verifies findings.
- It reports progress accurately.
- It produces grounded, useful final answers.
- It respects enterprise authorization and governance.

### Final product principle

**Manus-like autonomous task execution.**

**ChatGPT-like conversational intelligence and research-to-action experience.**

**IBM-inspired agent reasoning, ReAct execution and multi-agent collaboration.**

**Zhanlu's enterprise-specific data, ontology, departmental knowledge, tools, governance and operational intelligence.**

The objective is to make Zhanlu an intelligent enterprise platform that can understand what users want to accomplish and reliably carry out the necessary work.

Do not optimize for the number of agents, the number of tool calls or the appearance of activity.

Optimize for the quality, correctness, reliability, transparency and successful completion of the user's objective.