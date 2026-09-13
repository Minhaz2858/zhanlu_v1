# Zhanlu Universal Context Safety Architecture
## SafeContextGate + Data-Pointer + Deterministic Fallback

> **For:** CodeBuddy Implementation
> **Scope:** `backend/app/` (FastAPI)
> **Goal:** Eliminate ALL context-window crashes from ANY source + enable CSV/XLSX export
> **Pattern:** Production-grade agent architecture (Claude, ChatGPT, Kimi, Letta/MemGPT)

---

## 1. Executive Summary

### The Problem
Zhanlu crashes with "Sorry, I hit an error" when ANY tool returns a large payload that exceeds the LLM's context window:
- `ask_data_agent` / `execute_query` / `fetch_data_batch`: 1000+ rows (~150K tokens)
- `web_search`: 100 pages of text (~50K tokens)
- `search_documents`: 500 text chunks (~30K tokens)
- File uploads: 10MB of text (~2M tokens)
- ANY other tool that returns large JSON, CSV, or text

Current `compaction/` is reactive (emergency trimming AFTER overflow). We need **proactive** prevention that catches ANY large payload from ANY tool.

### The Solution (Universal)

| Layer | What It Does | When It Runs |
|-------|-------------|--------------|
| **SafeContextGate** | Universal pre-flight enforcer. Estimates tokens. Compresses history, spills ANY large payload from ANY tool, prunes irrelevant tools BEFORE LLM call. | **Every LLM call** |
| **Data-Pointer Layer** | ANY tool result >2000 chars or >20 list items gets stored in Redis/DataExecution and replaced with a `cache_id` pointer. | **On any tool return** |
| **Data Exploration Tools** | Explicit tools (`data_top_n`, `data_aggregate`, `build_file`) that let the LLM fetch small slices from ANY cached payload. | **On demand** |
| **Deterministic Fallback** | If LLM still fails, build a report from cached profile without any LLM. | **On error** |

**Result:** Context-window crashes become structurally impossible regardless of which tool produced the data. Works for qwen, deepseek, kimi, or any future model.

---

## 2. Architecture Overview

```
User Message
    |
    v
[ASSEMBLE] system + skills + KB + history + user msg + tool schemas
    |
    v
+----------------------------------------------------------+
| SAFECONTEXTGATE (runs before EVERY LLM call)             |
|                                                          |
| Step 1: Estimate total tokens (prompt + max_output)      |
| Step 2: Check against budget (context_window * 0.75)     |
| Step 3: If over budget -> COMPACT                        |
|                                                          |
|   PROTECT (never touch):                                 |
|   - System prompt                                        |
|   - User's CURRENT message                               |
|   - Last 6 turns (user + assistant)                      |
|   - Active/pending tool calls                            |
|                                                          |
|   COMPRESS:                                              |
|   - Old history (>6 turns) -> hierarchical summary       |
|   - Large payloads (>2000 chars or >20 list items)       |
|     -> store in Redis/DataExecution -> replace w/ pointer|
|   - Tool schemas -> keep only intent-relevant tools      |
|                                                          |
|   GUARANTEE: output fits in window with 25% headroom     |
+----------------------------------------------------------+
    |
    v
[LLM CALL] - guaranteed safe context
    |
    v
[TOOL CALL] ANY tool -> large payload (rows, text, files)
    |
    v
+----------------------------------------------------------+
| UNIVERSAL SPILL LAYER (tool_result_persistence.py)       |
|                                                          |
| Detect: ANY result >2000 chars OR contains list >20 items|
| Action: Store in Redis (2h TTL) + DataExecution          |
| Return: Pointer {cache_id, size, profile, source_tool}   |
|                                                          |
| Works for: ask_data_agent, execute_query, web_search,    |
|            fetch_data_batch, search_documents,           |
|            file uploads, ANY future tool                 |
+----------------------------------------------------------+
    |
    v
Main Agent receives POINTER (~400 tokens) - NOT raw payload
    |
    v
[USER EXPLORATION] "Top 10 customers" -> data_top_n(cache_id, ...)
                   "Export to Excel" -> build_file(cache_id, ...)
    |
    v
[DONE] or [ERROR -> Deterministic Fallback from profile]
```

---

## 3. New Modules to Create

### 3.1 `services/safe_context/` - Universal Pre-Flight Gate

**Purpose:** Run before every LLM call. Guarantee context fits. Tool-agnostic.

**Classes:**

```
SafeContextGate
  - __init__(model_name: str)
      Reads context_window, max_output, tool_cap from config.py
      Sets budget = context_window * 0.75
  
  - enforce(messages: list, tools: list) -> (messages, tools)
      1. Estimate tokens (tiktoken or char//4 heuristic)
      2. If total <= budget: return as-is
      3. If total > budget: run compaction pipeline
  
  - _split_protected(messages) -> (protected, compressible)
      Protected: system prompt + last 6 turns + active tool calls
      Compressible: everything else
  
  - _hierarchical_compress(messages) -> list
      Group old messages into chunks of ~4 turns
      Small chunks (<500 tokens): keep verbatim
      Large chunks: summarize into [Earlier: intent | actions | outcome]
      Preserve constraints ("only APAC", "ignore returns") explicitly
  
  - _spill_large_payloads(messages) -> list
      Detect ANY large payload in ANY message (>2000 chars OR list >20 items)
      Store in Redis/DataExecution, generate cache_id
      Replace with pointer: [Large data at {cache_id}. Use data_describe() to explore.]
      Works for: data rows, web search results, file uploads, API responses, ANY tool output
  
  - _prune_tools(tools, user_message) -> list
      Keep only tools whose category matches user intent keywords
      Categories: data, artifact, web, memory, system
      Default: keep core tools if no match
  
  - _emergency_truncate(messages, target) -> list
      Last resort: aggressive truncation if still over budget
      Never touches protected zone
```

**Integration:** Insert ONE line in `routers/agents.py` `_stream_llm_with_tools`, right before LLM call:
```python
from app.services.safe_context.gate import SafeContextGate

# [NEW] Universal pre-flight context safety gate
gate = SafeContextGate(model_name=resolved_model)
messages, tools = gate.enforce(messages, tools)
# Now guaranteed: total_tokens <= context_window * 0.75
```

---

### 3.2 `services/data_exploration/` - Data Exploration Tools

**Purpose:** Let LLM explore ANY cached payload via small, safe tool calls. Works with ANY cache_id from ANY tool.

**Files:**

`profiler.py` - DatasetProfiler
```
profile(rows: list[dict], source_sql: str = '') -> dict
  Returns: {row_count, schema, stats, sample[5], anomalies, relationships, source_sql}
  
  _infer_schema(df): datetime | number | string
  _compute_stats(df): min, max, mean, sum, nulls per numeric column
  _detect_anomalies(df): IQR outliers per numeric column
  _detect_relationships(df): time-series span, correlations >0.8
  format_briefing(profile) -> str: LLM-safe summary (~400 tokens)
```

`engine.py` - DatasetCache + DataExplorer
```
DatasetCache
  - store(cache_id, rows, profile): Redis setex(2h) + DataExecution persist
  - load(cache_id): Redis get -> fallback DataExecution -> restore to Redis
  - get_profile(cache_id): lightweight, no full row load

DataExplorer
  - MAX_OUTPUT_ROWS: read from config.MODEL_EXPLORATION_ROW_CAP[model] (default 50)
  - MAX_OUTPUT_CHARS: 6000
  - _serialize(df, note): caps at MAX_OUTPUT_ROWS, hard char limit
  - describe(cache_id): schema + stats table
  - top_n(cache_id, by, n): nlargest(n, by), n capped at MAX_OUTPUT_ROWS
  - aggregate(cache_id, group_by, metric, agg, sort, limit): groupby + cap
  - filter_rows(cache_id, column, operator, value): eq/gt/lt/gte/lte/contains
  - sample(cache_id, n): random sample, capped
```

`tools.py` - ToolRegistry wrappers
```
data_describe(cache_id)
data_top_n(cache_id, by, n=10)
data_aggregate(cache_id, group_by, metric, agg='sum', sort='desc', limit=20)
data_filter(cache_id, column, operator, value)
data_sample(cache_id, n=10)
```

**Note:** These tools accept ANY `cache_id` — regardless of whether it came from `ask_data_agent`, `web_search`, `execute_query`, `fetch_data_batch`, or a file upload.

---

### 3.3 `services/artifact_renderers/tabular.py` - File Generation

**Purpose:** Build CSV/XLSX/DOCX from ANY cached payload. Zero LLM involvement in data.

```
TabularArtifactBuilder
  - build_csv(cache_id) -> MinIO URL
      pandas.DataFrame.to_csv -> BytesIO -> MinIO put_object
  
  - build_xlsx(cache_id, sheet_name='Data') -> MinIO URL
      pandas.DataFrame.to_excel(openpyxl) -> auto-width columns -> MinIO
  
  - build_data_docx(cache_id, title, sections) -> MinIO URL
      python-docx: title + paragraphs + tables + charts
      sections: [{type: 'paragraph'|'table'|'chart'|'page_break', ...}]
      _execute_section(df, spec): deterministic pandas ops (top_n, aggregate, filter)
      _render_chart(df, spec): matplotlib -> temp PNG -> embed in docx
  
  - _upload(buf, filename, content_type) -> MinIO URL
      Uses app.core.minio client, bucket='zhanlu-artifacts'

build_file(cache_id, format, title, sections) -> dict
  Tool wrapper. Returns {success, file_url, format, cache_id} or {error}
```

---

## 4. Patches to Existing Files

### 4.1 `routers/agents.py` - Insert SafeContextGate

**Location:** Inside `_stream_llm_with_tools`, immediately before the LLM API call.

**What to add:**
```python
from app.services.safe_context.gate import SafeContextGate

# [NEW] Universal pre-flight context safety gate
# Catches overflow from ANY source: data, history, skills, KB, tool schemas, file uploads
gate = SafeContextGate(model_name=resolved_model)
messages, tools = gate.enforce(messages, tools)
# Now guaranteed: total_tokens <= context_window * 0.75
```

**Also fix sub-agent model pass-through (known bug):**
When calling sub-agents (ask_data_agent, delegate_task), pass the parent model so sub-agents use the conversation's model, not `get_model()` (global default).

---

### 4.2 `services/tool_result_persistence.py` - Universal Spill Layer

**Location:** In the existing 3-layer spill logic (Layer 2: per-result persistence).

**What to add:** After writing large results to disk/DataExecution, ALSO emit a structured pointer that the LLM can use.

```python
# In persist_tool_result() or _persisted_result_str():

# [NEW] Universal data-pointer spill
# Detect ANY large payload from ANY tool
if _is_large_payload(result_str):
    # Store in cache
    cache_id = generate_cache_id(result_str, tool_name, conversation_id)
    rows = extract_rows_from_result(result_str)  # Try to parse as table
    profile = profile_dataset(rows, source_sql=tool_name) if rows else {}
    
    DatasetCache().store(cache_id, rows or [], profile)
    DataExecution.create_or_update(
        execution_id=cache_id,
        conversation_id=conversation_id,
        result_data={'rows': rows or [], 'profile': profile, 'source_tool': tool_name},
        row_count=len(rows) if rows else 0,
        source_tool=tool_name,
    )
    
    # Return POINTER instead of raw payload
    return json.dumps({
        'tool': tool_name,
        'cache_id': cache_id,
        'row_count': len(rows) if rows else estimate_size(result_str),
        'profile': profile,
        'available_tools': [
            'data_describe(cache_id)',
            'data_top_n(cache_id, by, n)',
            'data_aggregate(cache_id, group_by, metric, ...)',
            'data_filter(cache_id, column, operator, value)',
            'data_sample(cache_id, n)',
            'build_file(cache_id, format, title, sections)'
        ],
        'note': f'Large result from {tool_name} stored. Use tools to explore or build files.'
    })

# Existing logic continues for small results...
```

**This single patch catches overflow from ALL tools:**
- `ask_data_agent` -> 1000 rows -> pointer
- `execute_query` -> 1000 rows -> pointer
- `fetch_data_batch` -> 4000 rows -> pointer
- `web_search` -> 100 pages -> pointer
- `search_documents` -> 500 chunks -> pointer
- File upload -> 10MB text -> pointer
- ANY future tool -> large payload -> pointer

---

### 4.3 `tool_handlers/__init__.py` - Register New Tools

Import and register in ToolRegistry:
- `data_describe`, `data_top_n`, `data_aggregate`, `data_filter`, `data_sample` (category='data')
- `build_file` (category='artifact')

These tools are generic — they accept ANY `cache_id` from ANY source tool.

---

### 4.4 `tool_handlers/artifact_tool.py` - Add CSV/XLSX Renderers

In `create_artifact` dispatch, add:
- `elif artifact_type == 'csv':` -> call `build_file(cache_id, 'csv')`
- `elif artifact_type == 'xlsx':` -> call `build_file(cache_id, 'xlsx')`

Helper: `_extract_latest_cache_id(conversation_id)` -> query DataExecution for most recent cache_id.

---

### 4.5 `routers/agents.py` - Deterministic Error Fallback

**Location:** Inside `_persist_stream_error`, before emitting generic error.

**What to add:**
1. Scan conversation messages for most recent `cache_id`
2. If found, load profile from DatasetCache
3. Build markdown report from profile:
   - Overview: row count, columns, date range
   - Statistics: totals, averages per numeric column
   - Anomalies: outliers detected
   - Sample: first 5 rows
   - Note: "Full data available via exploration tools"
4. Return THIS as assistant content instead of "Sorry, I encountered an error"
5. Log original error for debugging

**Result:** User gets useful data even when LLM completely fails (timeout, rate limit, provider error).

---

### 4.6 `config.py` - Add Model-Aware Caps

Add to existing config:
```python
MODEL_EXPLORATION_ROW_CAP = {
    "qwen3.6-27b": 50,
    "deepseek-chat": 100,
    "deepseek-v4-flash": 100,
    "kimi-k2": 200,
    "default": 50,
}

SAFECONTEXT_CONFIG = {
    "safety_margin": 0.75,        # Use 75% of context window
    "protected_turns": 6,          # Keep last 6 turns verbatim
    "never_compress_user_input": True,
    "constraint_extraction": True, # Preserve "only", "ignore", "exclude" from old messages
    "intent_anchor": True,         # Persistent user goal in system prompt
}
```

---

## 5. Data Model Changes (If Needed)

### `models/data_execution.py`

If these methods don't exist, add:
```python
@classmethod
def get_by_execution_id(cls, execution_id: str) -> Optional['DataExecution']:
    return cls.query.filter(cls.execution_id == execution_id).first()

@classmethod
def get_latest_by_conversation(cls, conversation_id: str) -> Optional['DataExecution']:
    return (cls.query
            .filter(cls.conversation_id == conversation_id)
            .order_by(cls.created_at.desc())
            .first())

@classmethod
def get_latest_by_conversation_and_tool(cls, conversation_id: str, tool_name: str) -> Optional['DataExecution']:
    return (cls.query
            .filter(cls.conversation_id == conversation_id)
            .filter(cls.source_tool == tool_name)
            .order_by(cls.created_at.desc())
            .first())
```

---

## 6. Implementation Order

| Step | Task | File(s) | Effort |
|------|------|---------|--------|
| 1 | Create `services/safe_context/gate.py` | New | 6-8 hrs |
| 2 | Create `services/data_exploration/` (profiler, engine, tools) | New | 4-5 hrs |
| 3 | Create `services/artifact_renderers/tabular.py` | New | 3-4 hrs |
| 4 | Patch `agents.py`: insert SafeContextGate + deterministic fallback | Patch | 2-3 hrs |
| 5 | Patch `tool_result_persistence.py`: universal spill layer | Patch | 2-3 hrs |
| 6 | Patch `tool_handlers/__init__.py`: register tools | Patch | 30 min |
| 7 | Patch `artifact_tool.py`: add csv/xlsx | Patch | 1-2 hrs |
| 8 | Add config constants | Patch | 30 min |
| 9 | Write tests | New | 3-4 hrs |
| **Total** | | | **~22-28 hrs** |

**Recommended:** Implement Steps 1-5 first (the core safety net). Deploy. Then add Steps 6-9 (features + polish).

---

## 7. Testing Strategy

### Unit Tests
```
test_safe_context_gate:
  - Input: 50K tokens of messages. Assert output <= 24K (75% of 32K).
  - Verify protected zone (system + last 6 turns) is never modified.
  - Verify large payloads from ANY tool are replaced with cache_id pointers.

test_data_explorer_caps:
  - Input: 1000 rows. Assert data_top_n returns <=50 rows.
  - Assert _serialize never returns >6000 chars.

test_dataset_cache:
  - Store -> load from Redis.
  - Redis miss -> load from DataExecution -> verify restored to Redis.

test_artifact_builder:
  - build_csv: verify MinIO upload, correct MIME type.
  - build_xlsx: verify openpyxl auto-width, correct sheet name.
```

### Integration Tests
```
1. Large data query: "Show me all sales" (1000 rows from ask_data_agent)
   -> Should return pointer. Should NOT crash.
   -> SafeContextGate log should show: "Compacted: spilled 1000-row payload to data_xxx"

2. Direct SQL: "SELECT * FROM orders" (via execute_query)
   -> Should return pointer. Should NOT crash.

3. Web search: "Search for market trends 2026" (100 pages)
   -> Should return pointer. Should NOT crash.

4. Tool exploration: "Top 10 customers by revenue" -> data_top_n
   -> Should return 10 rows. Should be accurate.

5. File export: "Export to Excel" -> build_file
   -> Should return downloadable XLSX with full dataset.

6. Long conversation: 30 turns of back-and-forth
   -> Should NOT crash on turn 25+.
   -> SafeContextGate should compress old history automatically.
   -> Agent should still answer follow-ups correctly.

7. Error recovery: Trigger LLM timeout
   -> Should return deterministic report from profile.
   -> Should NOT show "Sorry, I hit an error".

8. Model switch: Test with qwen3.6-27b, then deepseek-chat
   -> Both should work. deepseek should get 100-row results (vs 50 for qwen).
```

---

## 8. Success Criteria

After implementation, verify ALL of these:

- [ ] Query "Show me all sales" (1000 rows via ask_data_agent) returns pointer — NO crash
- [ ] Direct SQL "SELECT * FROM orders" (via execute_query) returns pointer — NO crash
- [ ] Web search with 100 pages returns pointer — NO crash
- [ ] `data_top_n` returns <=50 rows for qwen, <=100 for deepseek
- [ ] `data_aggregate` returns grouped data, capped at limit
- [ ] `build_file` generates CSV/XLSX with full dataset (all rows)
- [ ] 30-turn conversation does NOT crash (SafeContextGate compresses old history)
- [ ] Agent with 20 skills does NOT crash (irrelevant skills pruned)
- [ ] LLM timeout returns deterministic report, NOT "Sorry, I hit an error"
- [ ] Works with qwen3.6-27b, deepseek-chat, and any model in config
- [ ] Existing features (docx/pptx artifacts, dashboards) still work
- [ ] Token estimation in SafeContextGate is accurate (within 10% of actual)

---

## 9. How to Use This Spec (For CodeBuddy)

### Step 1: Read the Architecture
Understand the three layers: SafeContextGate (universal safety) + Universal Spill Layer (catches ANY tool) + Data Exploration Tools (work with ANY cache_id).

### Step 2: Create New Modules First
Build `safe_context/gate.py`, `data_exploration/`, and `artifact_renderers/tabular.py` as standalone packages. They should be testable independently.

### Step 3: Apply Patches in Order
1. `agents.py` - Insert SafeContextGate (single most important change)
2. `tool_result_persistence.py` - Add universal spill layer (catches ALL tools)
3. `tool_handlers/__init__.py` - Register generic data exploration tools
4. `artifact_tool.py` - Add CSV/XLSX renderers
5. `agents.py` - Deterministic fallback
6. `config.py` - Add constants

### Step 4: Test Incrementally
After each patch, run the relevant integration test. Don't wait until the end.

### Step 5: Verify Success Criteria
Go through the checklist in Section 8. Every box must be ticked.

---

## 10. Key Design Decisions (Do Not Change)

1. **Never compress the user's current message.** It is sacred. Always protected.
2. **SafeContextGate runs BEFORE every LLM call, not after.** Proactive, not reactive.
3. **Universal spill layer catches ANY tool.** Not just ask_data_agent. ALL tools.
4. **Protected zone: system prompt + last 6 turns + active tool calls.** Never touched.
5. **Cache fallback chain: Redis (hot) -> DataExecution (persistent).** Survives restarts.
6. **Model-agnostic by default.** Use MODEL_EXPLORATION_ROW_CAP for per-model tuning.
7. **Data exploration tools are generic.** They accept ANY cache_id from ANY source tool.

---

## 11. Why This Is the Right Architecture

### It Is Universal
| Source of Overflow | How SafeContextGate Catches It |
|-------------------|-------------------------------|
| `ask_data_agent` (1000 rows) | `_spill_large_payloads` detects list >20 items -> cache pointer |
| `execute_query` (1000 rows) | Same universal spill layer |
| `fetch_data_batch` (4000 rows) | Same universal spill layer |
| `web_search` (100 pages) | `_spill_large_payloads` detects >2000 chars -> cache pointer |
| `search_documents` (500 chunks) | Same universal spill layer |
| File upload (10MB text) | Same universal spill layer |
| 50 turns of history | `_hierarchical_compress` summarizes old turns |
| 20 skills | `_prune_tools` keeps only relevant skills |
| Large KB context | `_spill_large_payloads` or `_prune_tools` |

### It Matches Production Systems
| Feature | Zhanlu (This Spec) | Claude | ChatGPT | Kimi | Letta |
|---------|-------------------|--------|---------|------|-------|
| Proactive compaction | SafeContextGate | Native | Native | Native | Explicit |
| Universal spill | tool_result_persistence.py | Native | Native | Native | Yes |
| Protected recent context | Last 6 turns | Yes | Yes | Yes | Yes |
| External memory | Redis + DataExecution | Yes | Yes | Yes | Yes |
| Deterministic fallback | Profile-based report | Partial | Partial | Partial | Yes |

### It Fixes the Root Cause
- **Reactive trimming** (current `compaction/`) waits for disaster, then panics.
- **Proactive gating** (SafeContextGate) prevents disaster before it happens.
- **Universal spill** (tool_result_persistence.py) catches ANY tool automatically.
- **Explicit tools** (Data Exploration) give the LLM control over what it sees.
- **Deterministic fallback** guarantees the user never gets an empty apology.

---

*End of Specification*