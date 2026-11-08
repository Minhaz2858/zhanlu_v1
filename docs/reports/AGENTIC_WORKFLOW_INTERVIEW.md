# Zhanlu Agentic AI Platform — End-to-End Workflow

> Interview quick-reference. All diagrams are Mermaid — paste into any renderer (GitHub, VS Code preview, mermaid.live).

---

## 1. System Architecture (Big Picture)

```mermaid
graph TB
    User[👤 User / Browser] --> FE[🖥️ React Frontend<br/>Chat / Workflows / Dashboards]
    FE -->|SSE stream<br/>/v3/messages/stream| API[🌐 FastAPI Backend<br/>routers/agents.py]

    API --> ORCH[⚙️ ReAct Orchestrator Loop<br/>tool_choice control · healing · guardrails]
    ORCH --> LLM[🧠 LLM Layer<br/>DeepSeek / Qwen / DashScope]

    ORCH --> REG[🗂️ Tool Registry<br/>150+ tools, MCP-exposed]
    REG --> DATA[🗄️ Data Agent<br/>ask_data_agent → NL2SQL]
    REG --> RAG[📚 Agentic RAG<br/>hybrid retrieval + grading]
    REG --> DELE[🤝 Multi-Agent<br/>delegate_task · parallel DAG]
    REG --> ONTO[🕸️ Ontology Layer<br/>objects · bindings · escalations]
    REG --> WORK[🔧 Workflow / Automation<br/>scheduled jobs · artifact builders]

    DATA --> PSQL[(🐘 PostgreSQL<br/>platform + RLS)]
    DATA --> WARE[(🏢 Enterprise DBs<br/>MySQL / warehouses)]
    RAG --> VDB[(🔎 Embeddings<br/>bge-m3 / MiniLM)]

    ORCH --> SSE[🚪 Stable SSE Gate<br/>normalize → one terminal done]
    SSE -->|final_answer_delta + done| FE

    classDef primary fill:#90EE90,stroke:#333,stroke-width:2px,color:darkgreen
    classDef secondary fill:#87CEEB,stroke:#333,stroke-width:2px,color:darkblue
    classDef database fill:#E6E6FA,stroke:#333,stroke-width:2px,color:darkblue
    class ORCH,REG primary
    class API,FE,SSE,LLM secondary
    class PSQL,WARE,VDB database
```

---

## 2. End-to-End Request Lifecycle (The Agentic Loop)

```mermaid
flowchart TD
    Start([🚀 User sends message]) --> Prep[⚙️ Prepare runtime<br/>bindings · data sources · org context]
    Prep --> EmitStart[📨 message_start event<br/>server_message_id · run_id]
    EmitStart --> Loop{🔄 ReAct loop<br/>iteration < budget?}

    Loop -->|yes| Think[🧠 LLM decides:<br/>call tool OR answer directly]
    Think --> ToolQ{🔧 Tool call?}
    ToolQ -->|no| Final
    ToolQ -->|yes| Heal[🩹 Execute with healing<br/>arg reformulation on failure]
    Heal --> Guard[🛡️ Guardrails<br/>hallucination check · read-only walls · risk tiers]
    Guard --> Ground{📊 Grounded<br/>enough?}
    Ground -->|no, retryable| Think
    Ground -->|yes / no progress| Think
    Think --> BudgetQ{💰 Budget /<br/>progress left?}
    BudgetQ -->|yes| Loop
    BudgetQ -->|no| Final

    Final[✍️ FinalResponseWriter<br/>one call, tool_choice=none] --> Stream[📡 Stream final_answer_delta<br/>token-level SSE]
    Stream --> Freeze[🔒 Freeze answer<br/>content_hash + frozen_at<br/>atomic transaction]
    Freeze --> Outbox[📤 Quality-eval outbox<br/>Celery evaluates async]
    Outbox --> Done[🏁 done event<br/>metadata only: status · hash · frozen_at]
    Done --> FEFreeze[🖥️ Frontend freezes content<br/>never re-renders after done]
    FEFreeze --> End([✅ Answer delivered])

    classDef primary fill:#90EE90,stroke:#333,stroke-width:2px,color:darkgreen
    classDef secondary fill:#87CEEB,stroke:#333,stroke-width:2px,color:darkblue
    classDef error fill:#FFB6C1,stroke:#DC143C,stroke-width:2px,color:black
    class Loop,ToolQ,Ground,BudgetQ primary
    class Prep,Think,Heal,Final,Stream,Done secondary
    class Guard error
```

**Interview narrative:** the loop is *silent orchestration* — during tool calls the stream only emits progress events (`tool_progress`, `activity_step`, `phase`); text deltas only come from the single FinalResponseWriter pass at the end. This guarantees the final answer is built from verified tool results, not hallucinated mid-stream.

---

## 3. Sequence: One Turn Through the Data Agent (NL2SQL)

```mermaid
sequenceDiagram
    participant U as 👤 User
    participant FE as 🖥️ Frontend
    participant API as 🌐 FastAPI (v3 stream)
    participant R as ⚙️ ReAct Loop
    participant DA as 🗄️ ask_data_agent
    participant SV as 🛡️ P-SV Verify Gate
    participant DB as 🏢 Warehouse

    U->>FE: "Show me October sales"
    FE->>API: POST /v3/messages/stream (SSE)
    API->>FE: message_start (run_id, turn_id)
    API->>R: orchestrate turn

    Note over R: tool_choice forces ask_data_agent<br/>when data question + bound KBs
    R->>DA: delegate question + bound source IDs
    DA->>DA: auto-resolve KB (single bound → no LLM guess)

    DA->>SV: pre-SQL gate: metric binding,<br/>grain + authority metadata, clarify?
    SV-->>DA: verified SQL contract (or clarification)
    DA->>DB: execute SQL (read-only, row cap)
    DB-->>DA: rows

    Note over DA: Self-heal on errors:<br/>unknown column → schema repair<br/>syntax 1064 → reserved-word fix<br/>collation 1267 → CAST rewrite

    DA->>DA: synthesis (figures only from this run's results —<br/>ungrounded report → retryable failure)
    DA-->>R: answer + rows + citations
    R->>R: grounding guardrail (nudge-retry)
    R->>API: FinalResponseWriter stream
    API->>FE: final_answer_delta * N
    API->>FE: done (completion_status, content_hash)
    FE->>FE: freeze content, stop busy state
```

---

## 4. Multi-Agent Collaboration

```mermaid
flowchart TB
    GA[🎯 General Assistant<br/>orchestrator] --> Plan{📋 Plan nodes?}
    Plan -->|independent| Pre[⚡ Parallel ready-batch prepass<br/>max 4 concurrent, own DB session each]
    Plan -->|dependent| Serial[➡️ Serial loop]

    Pre --> D1[🤝 delegate_task<br/>sub-agent A]
    Pre --> D2[🤝 delegate_task<br/>sub-agent B]
    Serial --> D3[🤝 delegate_task<br/>next ready node]

    D1 --> CI[🧬 Context inheritance:<br/>parent context digest into sub-agent prompt]
    D2 --> CI
    D3 --> CI

    D1 --> Bound[🕸️ Bound-KB scoping:<br/>sub-agents see only their bindings]
    D2 --> Bound
    D3 --> Bound

    D1 & D2 & D3 --> Merge[📥 Results merged into plan state<br/>adaptive replan if node failed]
    Merge --> GA

    classDef primary fill:#90EE90,stroke:#333,stroke-width:2px,color:darkgreen
    classDef secondary fill:#87CEEB,stroke:#333,stroke-width:2px,color:darkblue
    class GA,Plan primary
    class Pre,D1,D2,D3,CI,Bound secondary
```

---

## 5. Safety, Governance & Reliability (What Makes It Production-Grade)

```mermaid
flowchart LR
    subgraph IN[🔐 Inbound]
        AUTH[Auth + multi-tenant<br/>RLS on 127+ tables]
        RISK[Risk-tier enforcement:<br/>write actions need approval]
    end
    subgraph LOOP[⚙️ During loop]
        HEAL[Tool-call healing<br/>+ retry budgets]
        SV[Source-verification gate<br/>before any SQL]
        CANCEL[Mid-turn cancel]
        HONEST[Honest failure:<br/>no fake success / empty reports]
    end
    subgraph OUT[📡 Outbound]
        GATE[Stable SSE gate:<br/>exactly ONE terminal done]
        FREEZE[DB immutability after freeze<br/>content_hash locked]
        EVAL[Async quality evaluator<br/>outbox + Celery, lease-based]
    end

    AUTH --> LOOP --> OUT

    classDef inC fill:#E6E6FA,stroke:#333,stroke-width:2px,color:darkblue
    classDef loopC fill:#90EE90,stroke:#333,stroke-width:2px,color:darkgreen
    classDef outC fill:#87CEEB,stroke:#333,stroke-width:2px,color:darkblue
    class AUTH,RISK inC
    class HEAL,SV,CANCEL,HONEST loopC
    class GATE,FREEZE,EVAL outC
```

---

## 6. Interview Talking Points (60-second summary)

1. **One agentic loop, many specialists.** A ReAct-style orchestrator with 150+ registered tools (also exposed via MCP), delegating to a data agent (NL2SQL), agentic RAG, and sub-agents — with a plan-DAG that runs independent nodes in parallel.
2. **Grounding is enforced, not hoped for.** A source-verification gate runs *before* SQL executes (metric binding, grain, clarify-if-ambiguous), and a report-grounding gate *fails* any answer whose figures didn't come from tool results in that run. Self-healing fixes unknown columns, syntax, and collation errors at runtime — schema-agnostic, no hardcoded tables.
3. **The stream is a protocol, not a pipe.** Silent orchestration → one FinalResponseWriter pass → stable SSE gate that normalizes legacy events, guarantees exactly one terminal `done`, preserves partial content on failure. Answers are frozen atomically (hash + timestamp, DB-immutable) and quality-evaluated asynchronously via an outbox with lease-based claiming.
4. **Honesty by design.** Failures surface as failures (no fabricated success), empty results trigger probes and retries before reporting, low-confidence RAG context gets refused rather than answered, and human escalations carry full context.
5. **Multi-tenant and governed.** Row-level security, risk-tiered write actions gated on human approval, mid-turn cancel, audit events on every governed action.

**Known trade-offs to mention proactively (shows maturity):** wall-clock budgets for long agent runs, per-tool timeouts, and FSM fallback mode — flagged and tracked, not hidden.
