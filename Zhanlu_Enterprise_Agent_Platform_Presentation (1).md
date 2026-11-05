# ZHANLU

## A Governed Enterprise Agent Platform

**System architecture · Agent orchestration · Data intelligence ·
Governance**\
**Technical interview presentation**\
**Minhazul Islam**

> **One-line explanation:** Zhanlu is an enterprise agent harness that
> connects authorized business data and tools, coordinates specialist
> agents, and turns user goals into traceable, governed work.

------------------------------------------------------------------------

# 1. What is Zhanlu?

Zhanlu is not a single-purpose manufacturing application and not simply
a chatbot with tools.

It is a **general-purpose enterprise AI agent platform**. An
organization can configure departments, projects, specialist agents,
data sources, knowledge resources, and governed tools. A user states a
goal in natural language; Zhanlu's orchestration layer determines how to
accomplish it within the user's authorized scope.

``` mermaid
flowchart TB
    U["USER<br/>Ask a question · Request analysis · Start a task"]
    Z["ZHANLU<br/>Governed Enterprise Agent Platform"]
    OUT["OUTCOME<br/>Answer · Analysis · Report · Workflow result"]

    U --> Z --> OUT

    Z --- A["Agent orchestration"]
    Z --- D["Enterprise data access"]
    Z --- K["Knowledge and ontology"]
    Z --- W["Workflow and automation"]
    Z --- G["Governance and audit"]

    classDef main fill:#17365d,color:#fff,stroke:#17365d
    classDef layer fill:#e8f1fb,color:#17365d,stroke:#5484b8
    class Z main
    class A,D,K,W,G layer
```

**The central design idea:** the agent decides the task strategy; the
harness controls what the agent is allowed to access and execute.

------------------------------------------------------------------------

# 2. The platform architecture

``` mermaid
flowchart TB
    subgraph EXPERIENCE["1 · EXPERIENCE LAYER"]
        CHAT["Conversational workspace"]
        DEPT["Department / project workspace"]
        REPORT["Report center"]
        ADMIN["Administration and resource center"]
    end

    subgraph ORCHESTRATION["2 · AGENT ORCHESTRATION LAYER"]
        GA["General Assistant"]
        PLAN["Intent understanding and planning"]
        BROKER["Agent broker<br/>Discover · Delegate · Coordinate"]
        REVIEW["Review / replan / synthesize"]
    end

    subgraph AGENTS["3 · SPECIALIST AGENT LAYER"]
        A1["Department Agent A"]
        A2["Department Agent B"]
        A3["Department Agent C"]
        A4["Project-specific Agent"]
    end

    subgraph CAPABILITIES["4 · SHARED CAPABILITY LAYER"]
        SQL["Structured data tools<br/>NL2SQL · Query validation"]
        DOC["Document intelligence<br/>Parsing · Hybrid RAG"]
        KG["Ontology and business context"]
        WF["Workflow / automation tools"]
        EXT["MCP and external tools"]
    end

    subgraph DATA["5 · CUSTOMER RESOURCE LAYER"]
        DB["Databases and warehouses"]
        FILE["Files and knowledge bases"]
        API["APIs and services"]
        MCP["Registered MCP servers"]
        AUTO["Automation resources"]
    end

    subgraph CONTROL["CROSS-CUTTING GOVERNANCE"]
        AUTH["Identity · RBAC · Resource scope"]
        SAFE["Policy · Approval · Execution budgets"]
        PROV["Evidence · Provenance · Audit"]
    end

    CHAT --> GA
    DEPT --> GA
    REPORT --> GA
    ADMIN --> GA
    GA --> PLAN --> BROKER
    BROKER --> A1
    BROKER --> A2
    BROKER --> A3
    BROKER --> A4
    A1 --> SQL
    A1 --> DOC
    A2 --> KG
    A2 --> WF
    A3 --> EXT
    A4 --> SQL
    SQL --> DB
    DOC --> FILE
    KG --> DB
    WF --> API
    EXT --> MCP
    EXT --> AUTO

    AUTH -. governs .-> GA
    AUTH -. governs .-> BROKER
    AUTH -. governs .-> CAPABILITIES
    SAFE -. governs .-> CAPABILITIES
    PROV -. records .-> ORCHESTRATION
```

### How to read this architecture

-   **Experience:** where users work with Zhanlu.
-   **Orchestration:** understands requests and coordinates work.
-   **Agents:** domain- or project-specific reasoning and execution
    units.
-   **Capabilities:** reusable tools for data, documents, ontology,
    workflows, and external systems.
-   **Resources:** the customer's connected systems and knowledge.
-   **Governance:** applies across the whole platform rather than being
    left to the final response.

------------------------------------------------------------------------

# 3. How a user request becomes agent work

``` mermaid
sequenceDiagram
    autonumber
    actor User
    participant UI as Zhanlu Workspace
    participant GA as General Assistant
    participant Broker as Agent Broker
    participant Agent as Specialist Agent
    participant Harness as Governed Tool Runtime
    participant Source as Authorized Data Source

    User->>UI: Submit a business goal
    UI->>GA: Request + project context
    GA->>GA: Understand intent and constraints
    GA->>GA: Build task plan and acceptance criteria
    GA->>Broker: Request suitable specialist capability
    Broker->>Broker: Discover and authorize candidate agent
    Broker-->>GA: Delegation decision
    GA->>Agent: Delegate bounded task
    Agent->>Harness: Request tool / source operation
    Harness->>Harness: Check identity, scope, policy and budgets
    Harness->>Source: Execute permitted operation
    Source-->>Harness: Data / documents / tool result
    Harness-->>Agent: Result with execution metadata
    Agent-->>GA: Findings and evidence
    GA->>GA: Review, reconcile and synthesize
    GA-->>UI: Final response and execution trace
    UI-->>User: Answer / report / next action
```

### Important distinction

The General Assistant does not simply forward every message to a fixed
agent. It interprets the request, creates a plan, discovers suitable
agents, delegates bounded work, evaluates returned results, and produces
a consolidated response.

------------------------------------------------------------------------

# 4. Agent-to-agent collaboration

Zhanlu uses a **hybrid orchestration pattern**: a central supervisor
governs task decomposition and delegation, while specialist agents can
collaborate through controlled peer invocations.

``` mermaid
flowchart TB
    USER["User goal"]
    GA["General Assistant<br/>Supervisor / coordinator"]
    PLAN["Task plan<br/>Goals · Constraints · Acceptance criteria"]

    USER --> GA --> PLAN

    PLAN --> D1["Delegate task 1"]
    PLAN --> D2["Delegate task 2"]
    PLAN --> D3["Delegate task 3"]

    D1 --> A1["Specialist Agent A"]
    D2 --> A2["Specialist Agent B"]
    D3 --> A3["Specialist Agent C"]

    A1 <-->|Governed peer collaboration| A2
    A2 <-->|Governed peer collaboration| A3

    A1 --> R1["Task result + evidence"]
    A2 --> R2["Task result + evidence"]
    A3 --> R3["Task result + evidence"]

    R1 --> GA
    R2 --> GA
    R3 --> GA

    GA --> VERIFY["Review and verify"]
    VERIFY --> FINAL["Consolidated result"]
```

### Delegation is governed

The broker enforces controls such as:

-   Cycle detection and duplicate-invocation prevention
-   Maximum delegation depth and child count
-   Concurrency and timeout limits
-   Token / execution budgets
-   Invocation records and auditability
-   Explicit terminal states for child runs

**Why it matters:** collaboration can be useful without allowing an
uncontrolled swarm of agents.

### Tool policy: how a child agent's reach is bounded

The controls above are enforced through concrete policy layers:

| Layer | Meaning |
|---|---|
| `enabled_tools` | Preference, not prison — moved to the priority head of the tool list |
| `disabled_tools` | Hard operator deny — the child cannot call them, regardless of prompt |
| `strict_tools: true` | Explicit opt-in that turns `enabled_tools` into an exact allowlist (no discovery) |
| Ontology bindings | The child reads only through its own authorized object bindings — scoping is structural, not prompt-hope |
| Invocation records | Every delegation links parent → child with live status — a replayable execution tree |

### Four orchestration patterns (all implemented)

These patterns run against a live workspace: eight department projects
(R&D, Manufacturing & Production, Procurement, Sales, Finance,
Aftersales, Logistics), one published specialist agent per business
function, and per-project model sets — for example, R&D runs
`qwen3.5-omni-plus` while other departments use the platform default.

1.  **Supervisor → single specialist.** The supervisor discovers agents
    from a directory (`list_available_agents`). Only **published**
    agents appear — a half-built or retired agent is invisible and
    undelegable. The child runs with its own layered system prompt and
    its own tool policy, not a generic fallback.
2.  **Heterogeneous parallel fan-out.** One delegation call can carry a
    batch of `{task, agent, budget}` items — different specialists
    execute concurrently under a semaphore (max 4), each with its own
    wall-clock budget and its own invocation record linked to the
    parent.
3.  **Plan-DAG execution.** For multi-step goals, the planner emits a
    dependency graph of typed nodes. Independent nodes run in parallel
    ready-batches, each node in its own database session (SQLAlchemy
    sessions are not concurrency-safe), bounded and cache-synced between
    rounds. Failed nodes can trigger corrective re-plans.
4.  **Governed run kernel.** A stricter contract for auditable runs:
    agent profile versioning, admission checks, token budgets, an
    eight-check policy gate before any tool call, idempotent execution
    envelopes, and step-level persistence for replay.

### The patterns delivering: one fault, three specialists in parallel

``` mermaid
sequenceDiagram
    autonumber
    actor User
    participant GA as General Assistant
    participant PM as Predictive Maintenance Agent
    participant INV as Inventory Agent
    participant SCO as Supply Chain Agent

    User->>GA: "Line 3 vibration alerts rising — assess impact"
    Note over GA: decomposes into a plan with parallel branches
    par Parallel fan-out (semaphore-bounded, 4 max)
        GA->>PM: diagnose telemetry + maintenance history
        GA->>INV: check spare-part stock levels
        GA->>SCO: check supplier lead times for replacement
    end
    PM-->>GA: bearing degradation, ~2 weeks runway (grounded rows)
    INV-->>GA: bearing in stock: 0 · alternate part: 4 units
    SCO-->>GA: primary supplier lead 3 weeks · alternate 5 days
    Note over GA: synthesizes ONE answer — figures only from children's findings
    GA-->>User: recommendation + per-agent trace in the execution tree
```

**What this shows:** answer latency equals the slowest specialist, not
the sum; each child runs under its own AgentApp prompt and authorized
bindings; and the supervisor — not the children — speaks to the user.

### Cross-organization delegation: A2A protocol

A2A (Agent2Agent v1.0, JSON-RPC 2.0 over HTTPS) is implemented
**bidirectionally**:

| Direction | Mechanism | Principle |
|---|---|---|
| **Inbound** — external agent delegates *to* Zhanlu | Platform serves a machine-readable **AgentCard** (`GET /a2a/agent-card`) exposing every published agent as a skill; external `tasks/send` is routed through the governed runtime | No side door — external callers get the same policy, budgets, and audit |
| **Outbound** — Zhanlu delegates *to* external agents | An internal agent calls the `a2a` tool: `tasks/send` / `tasks/get` / `tasks/cancel`, with SSE streaming of task snapshots and a JSON fallback; requests carry unique agent-identity headers | Traceable identity — the platform signs who it is on the way out |
| **Task lifecycle** | Tasks are first-class records: create, poll, cancel, artifacts; `input-required` surfaces as a clarification, not a failure | Same status vocabulary inside and outside |

**Protocol landscape in one breath:** MCP covers tool-level integration,
A2A covers agent-level delegation, ACP is a sibling communication
profile.

``` mermaid
sequenceDiagram
    autonumber
    participant SUP as Zhanlu Supervisor (internal)
    participant GW as A2A layer
    participant EXT as External partner agent

    Note over GW: Inbound — the partner discovers us
    EXT->>GW: GET /a2a/agent-card (discovery)
    GW-->>EXT: card: skills = published agents
    EXT->>GW: tasks/send (JSON-RPC)
    GW->>GW: create task row → governed runtime executor<br/>(policy · budget · audit)
    GW-->>EXT: task snapshots (SSE) → final artifacts

    Note over GW: Outbound — we delegate to them
    SUP->>GW: a2a tool call (task + identity headers)
    GW->>EXT: tasks/send (streaming requested)
    EXT-->>GW: SSE snapshots → final task
    GW-->>SUP: grounded result → synthesized with everything else
```

------------------------------------------------------------------------

# 5. The Agent Harness: reasoning is separate from enforcement

``` mermaid
flowchart TD
    START["Agent task begins"]
    CTX["Build authorized context<br/>Role · Instructions · Project · Source descriptors"]
    THINK["Agent reasons and chooses next step"]
    DEC{"What should happen next?"}

    TOOL["Propose tool call"]
    VALIDATE["Validate tool name and arguments"]
    POLICY["Harness policy checks<br/>RBAC · Source scope · Risk · Budgets"]
    APPROVAL{"Approval required?"}
    EXEC["Execute permitted tool"]
    OBS["Observe result and capture provenance"]
    VERIFY["Check result and task progress"]
    CONTINUE{"Continue, replan, or finish?"}

    FINAL["Final response generation"]
    STOP["Refuse / stop / escalate"]
    USER["User receives result"]

    START --> CTX --> THINK --> DEC
    DEC -->|Tool operation| TOOL --> VALIDATE --> POLICY --> APPROVAL
    APPROVAL -->|No| EXEC
    APPROVAL -->|Yes| HUMAN["Human approval"]
    HUMAN -->|Approved| EXEC
    HUMAN -->|Rejected| STOP
    POLICY -->|Denied| STOP
    EXEC --> OBS --> VERIFY --> CONTINUE
    CONTINUE -->|Continue| THINK
    CONTINUE -->|Replan| CTX
    CONTINUE -->|Complete| FINAL
    DEC -->|Respond| FINAL
    FINAL --> USER
    STOP --> USER
```

**Architectural invariant:** the LLM proposes actions; deterministic
runtime controls enforce permissions, source scope, validation, budgets,
approvals, and execution boundaries.

------------------------------------------------------------------------

# 6. Dynamic multi-source data access

Zhanlu is designed as an **agent harness for heterogeneous enterprise
data**, not a single-database chatbot.

The active agent determines which authorized source or sources are
relevant to the current task. The platform does not silently select the
first datasource, assume one default database, or query every connected
source automatically.

``` mermaid
flowchart TB
    TASK["User task"]
    AGENT["Active agent<br/>Understands intent and plans"]
    CATALOG["Authorized source descriptors<br/>Available sources · Capabilities · Metadata"]
    SELECT["Agent selects relevant source(s)"]
    CHECK["Harness checks authorization and scope"]
    EXEC["Appropriate capability / connector"]

    TASK --> AGENT --> CATALOG --> SELECT --> CHECK --> EXEC

    EXEC --> SQL["Structured databases"]
    EXEC --> DOC["Files and knowledge bases"]
    EXEC --> API["REST / enterprise APIs"]
    EXEC --> MCP["Registered MCP tools"]
    EXEC --> OTHER["Other supported connectors"]

    SQL --> RESULT["Observed results + provenance"]
    DOC --> RESULT
    API --> RESULT
    MCP --> RESULT
    OTHER --> RESULT
    RESULT --> AGENT
```

### Source-selection rules

1.  The active agent interprets the task and chooses sources
    dynamically.
2.  The harness exposes only sources the caller is authorized to use.
3.  Each source has explicit capabilities and metadata.
4.  The agent can use multiple relevant sources in one task.
5.  No implicit default / first datasource.
6.  No automatic query-all behavior.
7.  Unsupported or unauthorized source access fails explicitly.

### Data-source examples

  -----------------------------------------------------------------------
  Source category                     Typical information
  ----------------------------------- -----------------------------------
  Relational databases                Orders, customers, transactions,
                                      operational records

  Data warehouses                     Historical and aggregated business
                                      data

  Knowledge bases                     Manuals, policies, specifications,
                                      internal documents

  APIs                                External business services and
                                      operational applications

  MCP tools                           Registered tools and services
                                      exposed through MCP
  -----------------------------------------------------------------------

### Enterprise system compatibility (QMS · WMS · MES · ERP · PLM · EAM)

The honest answer has two tiers:

-   **Works today:** any system reachable as a database or REST API.
    ERP records become ontology objects; MES / QMS / WMS / EAM data is
    bound, discovered into the semantic store, and queryable
    cross-system in one grounded question. Write-back (PO drafts,
    dispositions, quotes) is approval-gated and audited. Live ERP and
    warehouse bindings already run in production.
-   **Needs a connector:** proprietary interfaces — SAP RFC/BAPI, OPC UA
    (SCADA/PLC), file-based QMS records, PLM APIs (Windchill /
    Teamcenter). Each is a new data-source type plus a tool registration
    on extension points that already exist; bounded configuration work,
    not architecture change.

**Design rule:** the platform never replaces the enterprise system as
the execution authority. It reads through their interfaces, correlates
across them, and writes back only through governed, approval-gated
actions — the system of record stays the system of record.

------------------------------------------------------------------------

# 7. Structured data intelligence: from natural language to SQL

``` mermaid
flowchart TD
    Q["Natural-language data question"]
    INTENT["Understand business intent and time range"]
    AUTH["Resolve authorized source scope"]
    DISC["Discover relevant schemas and tables"]
    PLAN["Create grounded query plan"]
    COMPILE["Compile SQL deterministically"]
    VALIDATE["Validate tables, columns, joins and query safety"]
    RUN["Execute against authorized source"]
    RESULT["Canonical result set"]
    LINEAGE["Capture SQL lineage and provenance"]
    ANSWER["Explain results using computed data"]

    Q --> INTENT --> AUTH --> DISC --> PLAN --> COMPILE --> VALIDATE
    VALIDATE -->|Invalid| PLAN
    VALIDATE -->|Valid| RUN --> RESULT --> LINEAGE --> ANSWER
```

### Zhanlu's deterministic NL2SQL path

-   Understand intent and temporal constraints.
-   Discover authorized source metadata.
-   Select candidate tables based on schema evidence.
-   Produce a grounded query plan.
-   Compile and validate SQL before execution.
-   Execute against the selected authorized source.
-   Return a canonical result with lineage and provenance.

**Engineering result recorded in the project:** the optimized path
reduced typical SQL planning/execution round trips from 77 to 2--5,
reduced retrieved rows from approximately 180,000 to 50--200, and
reduced latency from 90--478 seconds to 30--45 seconds in the measured
workload.

*These are project-specific measurements, not a guarantee for every
customer database or query.*

### Verification and self-healing: why the answer can be trusted

Before SQL runs, a **source-verification gate** checks grain and
authority metadata and metric bindings; ambiguous questions are
**clarified with the user** rather than guessed. At runtime, three error
classes self-heal without human intervention: unknown columns (schema
re-read and repair), syntax errors (reserved-word and alias rewrites),
and collation mismatches (CAST rewrites on joins). Empty results trigger
a probe of the latest posted data before the system reports honestly
that nothing is available.

Finally, a **grounding gate** fails any substantive report whose figures
did not come from tool results in the current run — stale memory or
fabricated numbers become a retryable failure, never a delivered answer.
Production evidence recorded in the project: 474 NL2SQL runs, ~86.5%
success rate, with failures classified rather than hidden.

------------------------------------------------------------------------

# 8. Knowledge, documents, and enterprise ontology

Zhanlu combines structured data with enterprise knowledge so agents can
interpret business concepts rather than relying only on raw table names.

``` mermaid
flowchart LR
    subgraph INPUT["KNOWLEDGE INPUTS"]
        DB["Database schemas"]
        DOC["Documents"]
        RULE["Business rules"]
        EVID["Evidence"]
        EXEC["Observed executions"]
    end

    subgraph ONTO["ENTERPRISE KNOWLEDGE LAYER"]
        OBJ["Business objects"]
        REL["Relationships"]
        METRIC["Metric definitions"]
        PROC["Processes and activities"]
        RES["Resource bindings"]
    end

    subgraph GOVERN["LIFECYCLE AND PROVENANCE"]
        REVIEW["Proposed → Reviewed"]
        STATUS["Verified · Enabled · Rejected"]
        TRACE["Evidence · Lineage · Execution references"]
    end

    DB --> OBJ
    DOC --> EVID
    RULE --> REVIEW
    EVID --> OBJ
    EXEC --> TRACE
    OBJ --> REL
    OBJ --> METRIC
    OBJ --> PROC
    RES --> OBJ
    OBJ --> REVIEW --> STATUS
    STATUS --> TRACE
```

### Knowledge lifecycle is not one generic status

-   Proposed knowledge is a hypothesis, not an established fact.
-   Verified knowledge can inform the assistant as evidence-backed
    context.
-   Enabled rules can provide governed executable guidance where
    applicable.
-   Rejected knowledge is excluded from assistant context.
-   Observed execution is not the same as documented process design.
-   Provenance records where evidence came from and how it was used.

### How the semantic layer is collected and kept current

The semantic store is what lets agents work on any customer database
without hardcoding anything. It is built automatically at bind time and
kept fresh continuously:

1.  **Schema ingestion** — scrape `information_schema` at bind time into
    table / column metadata stores.
2.  **Relationship discovery** — three phases: foreign-key constraints,
    naming heuristics, and data-overlap sampling.
3.  **Business rule extraction** — from views, constraints, procedures,
    and column comments into query guidelines.
4.  **Terminology layer** — metric bindings (aliases and confusable
    terms), synonyms, grain and authority metadata.
5.  **Query-log mining** — past successful SQL becomes workload hints
    and few-shot examples for future queries.
6.  **Drift sentinel** — hourly schema signature hashing; a customer
    schema change is detected and re-discovered automatically.

This is the difference between a demo and a product: a demo hardcodes
"sales_order means X"; the product discovers it, records the evidence,
stays current through drift detection, and learns the customer's
vocabulary from every query they run.

### Document intelligence: retrieval that refuses to guess

Retrieval is hybrid (dense embeddings + keyword), chunk-graded by an LLM
relevance judge, and retried with reformulation when context is thin.
Generation is citation-bound down to the source file. When confidence
falls below threshold, the system **refuses and suggests sources**
instead of answering from thin context — the same honesty contract as
the data path, applied to documents. Knowledge bases are isolated per
tenant.

### One question, both paths (subsystems composing)

The subsystems are not alternatives — a single user question can use
several of them in one governed turn:

``` mermaid
sequenceDiagram
    autonumber
    actor User
    participant GA as General Assistant
    participant SQL as NL2SQL Pipeline
    participant RAG as Document Retrieval
    participant W as Warehouse

    User->>GA: "Compare Q3 order volume vs forecast, and summarize the supplier risk memo"
    Note over GA: one turn — two capabilities, planned together
    par Data path
        GA->>SQL: verified schema + metric binding for "order volume"
        SQL->>W: verified SQL (read-only, row-capped)
        W-->>SQL: rows (self-heal if the schema surprised us)
        SQL-->>GA: figures + SQL + citations
    and Document path
        GA->>RAG: retrieve "supplier risk memo"
        RAG-->>GA: graded chunks with citations — or an honest refusal
    end
    Note over GA: cross-path grounding check:<br/>every number traceable to a tool call
    GA-->>User: one streamed answer, figures + citations,<br/>frozen atomically
```

**The shared contract:** nothing reaches the user that is not grounded
in a verifiable source — the semantic store grounds SQL, the
verification gate grounds queries, self-healing grounds execution, and
retrieval grades and refuses. Grounding is the platform's spine, not a
feature.

------------------------------------------------------------------------

# 9. Workflows, reports, and automation

``` mermaid
flowchart TB
    USER["User describes desired outcome"]
    ASSIST["Agent interprets and plans"]
    CONFIG["Clarify scope, period, sources and output"]
    CONFIRM["User confirms execution"]
    RUN["Run governed workflow"]
    DATA["Retrieve and compute source data"]
    ANALYZE["Analyze and identify findings"]
    VERIFY["Validate figures and evidence"]
    GENERATE["Generate report / artifact"]
    DELIVER["Present result in workspace"]
    AUDIT["Persist run, provenance and audit"]

    USER --> ASSIST --> CONFIG --> CONFIRM --> RUN
    RUN --> DATA --> ANALYZE --> VERIFY --> GENERATE --> DELIVER --> AUDIT
```

Examples of platform outputs include:

-   Natural-language answers grounded in enterprise data
-   Structured analysis and investigation results
-   Business reports with computed figures
-   PPTX / DOCX / HTML report artifacts
-   Governed workflow execution and approval requests
-   Scheduled automation results

**Important:** the language model writes explanations; deterministic
services compute authoritative figures and validate report payloads.

------------------------------------------------------------------------

# 10. Trust, permissions, and audit

``` mermaid
flowchart TB
    REQUEST["Request / task"]
    ID["Verify user and organization"]
    PROJECT["Resolve project / department context"]
    AGENT["Resolve agent permissions"]
    SOURCE["Resolve authorized source bindings"]
    TOOL["Validate requested capability"]
    RISK{"Operation risk"}
    READ["Permitted read / analysis"]
    APPROVAL["Approval workflow"]
    EXEC["Execute approved action"]
    DENY["Deny / refuse / explain"]
    AUDIT["Record decision, evidence and execution"]
    RESULT["Return result"]

    REQUEST --> ID --> PROJECT --> AGENT --> SOURCE --> TOOL --> RISK
    RISK -->|Read-only / permitted| READ --> AUDIT
    RISK -->|Governed write| APPROVAL
    APPROVAL -->|Approved| EXEC --> AUDIT
    APPROVAL -->|Rejected| DENY --> AUDIT
    TOOL -->|Invalid / unauthorized| DENY
    AUDIT --> RESULT
```

Governance is enforced at multiple boundaries:

-   User and organization identity
-   Project and department membership
-   Agent-specific tool permissions
-   Datasource authorization and source scope
-   Read/write risk separation
-   Human approval for governed actions
-   Execution budgets, timeouts, and concurrency
-   Audit events, lineage, and provenance

------------------------------------------------------------------------

# 11. How I debug in production

Mechanisms are easier to trust when you can see the failures that
motivated them. Three incidents from building and operating this
platform — each one changed the design.

**1. The stale-report bug — figures from memory, not the warehouse.**
The agent produced a "fresh" report assembled entirely from a historical
context ledger while the live warehouse held current data. Root cause:
injected context carried no caveats, and nothing verified that figures
came from the current run. **Fix:** a grounding gate that hard-fails any
substantive report with zero successful data-source calls in the run,
plus explicit historical-data labeling. **Lesson:** trust is a systems
property, not a prompt.

**2. The hanging data agent.** Users saw the UI freeze on a template
label ("Querying the {data_source_id} data source"). Full-path tracing
found the chain: the model emitted empty tool arguments → the label
rendered literally → a sub-loop burned every iteration on unusable input
→ synthesis was skipped because no query ever ran → the stream simply
ended. **Fix:** auto-resolve a single bound source instead of forcing
the model to guess, watchdogs on both sides of the wire, honest terminal
statuses. **Lesson:** an agent bug is rarely one bug — trace the whole
path.

**3. The deployment mismatch.** Reported fixes "didn't work" — because
the serving stack ran stale code against an unmigrated database; the
fixed code was never live. **Lesson:** in real deployments,
works-on-my-machine is the default state of the world. Verification
means checking the running system, not the repository.

------------------------------------------------------------------------

# 12. Business value: every mechanism buys an outcome

No technical capability is presented without the business outcome it
purchases, and no business promise without the mechanism that makes it
real.

| Technical capability | Business outcome it buys |
|---|---|
| Semantic store + drift sentinel | New systems onboarded in days, not months of hardcoding; survives ERP schema changes silently |
| NL2SQL verification gate | Reports finance and operations can act on — ambiguity becomes a clarifying question, not a wrong number |
| Self-healing SQL | Data-engineering tickets for broken reports drop toward zero; analytics uptime without an analyst on call |
| Grounding gates + RAG low-match refusal | Kills the #1 enterprise AI fear: confidently wrong answers. Unverifiable output fails and retries |
| Approval-gated execution + audit replay | Automation without loss of control; compliance evidence is a by-product, not a project |
| Bounded multi-agent orchestration | One question fans out to specialists concurrently — latency of the slowest specialist, not the sum |
| Model-agnostic serving (per-project model sets) | Cost and routing optimized per department; no vendor lock-in |
| RLS multi-tenancy + RBAC | One deployment serves many business units safely — the SaaS economics multiplier |

**The value conversation in three buckets.** First, *latency of
decisions*: cross-system investigations collapse from days to one
conversation. Second, *cost of errors*: a grounding gate is cheap
insurance against the most expensive failure mode — a wrong number or
defect that escapes and escalates roughly 10× at each stage it survives.
Third, *compliance labor*: industry benchmarks put ~40% of audit-pack
assembly time as automatable, and because every verdict is logged with
inputs, rules, model version and outcome, audit-ready evidence is
produced as a by-product of normal operation.

> Every mechanism in the stack exists to convert one of three business
> liabilities — slow decisions, confident errors, compliance labor —
> into a system property instead of a person's vigilance.

------------------------------------------------------------------------

# 13. What makes Zhanlu different?

  -----------------------------------------------------------------------
  Conventional chatbot                Zhanlu
  ----------------------------------- -----------------------------------
  One conversational model            Governed multi-agent runtime

  Fixed tools and routes              Agent-driven task planning and
                                      capability selection

  Often assumes one data source       Dynamic authorized heterogeneous
                                      sources

  Tool calls may be opaque            Observable agent and tool execution
                                      records

  Answer-first behavior               Plan → act → observe → verify

  Weak separation of evidence and     Provenance, lineage, and grounding
  claims                              controls

  Unbounded agent delegation risk     Bounded broker with cycle / depth /
                                      budget controls

  Generated report text               Deterministic data computation plus
                                      LLM explanation

  Permissions mainly at UI level      Runtime authorization and policy
                                      enforcement
  -----------------------------------------------------------------------

------------------------------------------------------------------------

# 14. Technology and engineering foundation

``` mermaid
flowchart LR
    subgraph FRONT["FRONTEND"]
        REACT["React workspace"]
        SSE["Streaming activity and results"]
    end

    subgraph BACK["BACKEND"]
        FAST["FastAPI services"]
        LANG["LangGraph / agent runtime"]
        BROKER["Agent broker and A2A"]
    end

    subgraph DATA["STATE AND STORAGE"]
        PG["PostgreSQL"]
        REDIS["Redis"]
        MINIO["MinIO"]
    end

    subgraph MODEL["MODEL AND RETRIEVAL"]
        VLLM["vLLM · Qwen"]
        EMB["Embedding / reranking"]
        RAG["Hybrid retrieval"]
    end

    REACT --> FAST
    SSE <--> FAST
    FAST --> LANG --> BROKER
    FAST --> PG
    FAST --> REDIS
    FAST --> MINIO
    LANG --> VLLM
    LANG --> EMB --> RAG
```

**Project stack:** FastAPI, React, PostgreSQL, Redis, MinIO, LangGraph,
MCP, A2A JSON-RPC/SSE, vLLM, and Qwen model serving.

------------------------------------------------------------------------

# 15. How I would deploy Zhanlu in a customer organization

``` mermaid
flowchart LR
    S1["1. Understand<br/>Business goals and users"]
    S2["2. Map<br/>Agents, data and permissions"]
    S3["3. Connect<br/>Authorized sources and tools"]
    S4["4. Configure<br/>Agent roles and workflows"]
    S5["5. Validate<br/>Security, evidence and outputs"]
    S6["6. Pilot<br/>One measurable business process"]
    S7["7. Expand<br/>More teams and capabilities"]

    S1 --> S2 --> S3 --> S4 --> S5 --> S6 --> S7
```

A practical deployment starts with one high-value workflow, not a
promise to automate the entire enterprise immediately.

1.  Identify a business process and its decision owners.
2.  Map the systems and data needed to support it.
3.  Connect sources with explicit authorization.
4.  Configure agents and their capabilities.
5.  Validate answers, permissions, and failure behavior.
6.  Pilot with real users and agreed success metrics.
7.  Expand to more departments after the workflow is trusted.

------------------------------------------------------------------------

# 16. Closing message

``` mermaid
flowchart TB
    DATA["ENTERPRISE DATA"]
    AGENTS["SPECIALIST AGENTS"]
    ORCH["GOVERNED ORCHESTRATION"]
    TRUST["TRUST + PROVENANCE"]
    VALUE["TRACEABLE BUSINESS OUTCOMES"]

    DATA --> ORCH
    AGENTS --> ORCH
    ORCH --> TRUST --> VALUE
```

> **Zhanlu turns enterprise AI from a single assistant into a governed
> system of collaborating agents --- able to discover authorized
> resources, plan and execute work, verify evidence, and deliver
> traceable outcomes.**

**My engineering focus:** building the harness that makes agentic
systems useful in real organizations: dynamic source selection, reliable
orchestration, deterministic execution controls, provenance, and human
governance.

**A working discipline I hold throughout:** for every deliverable I
track three separate states — *implemented*, *tested*, and
*production-ready* — and I never blur them. In an interview, in a
customer steering meeting, or in a status report, that distinction is
what makes everything else in this document believable.
