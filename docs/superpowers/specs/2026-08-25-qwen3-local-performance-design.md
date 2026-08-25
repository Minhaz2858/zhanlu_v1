# qwen3.6-27b Local Performance Design

**Date:** 2026-08-25
**Status:** Approved (all 6 sections)
**Goal:** Make qwen3.6-27b-awq4 work reliably, fast, and with high-quality answers — **100% offline** (no DeepSeek API calls).

## Context

The customer wants to use qwen3.6-27b as the primary model in the Zhanlu system. Current state (per memory + today's investigation):

- **Already fixed (commits c1806ce, 7e372a8, 1090349):** context-window-aware data source runtime, stream-path smart_truncate, Hermes-style FSM proactive pruning.
- **Still broken:**
  1. vLLM rejects mid-list system messages with HTTP 400 (`"System message must be at the beginning."`)
  2. qwen3.6-27b emits tool_call args as single-quoted Python dict literals (`'query': '...'`), which `sanitize_messages` replaces with `{}` → "Invalid arguments"
  3. 8-15 LLM calls per turn (self-eval re-plan + goal-contract forces + verify nudges + Tier 2 retry) → 113-141s LLM time alone
  4. No deterministic fallback when synthesis fails → "Sorry, I hit an error while responding" from the frontend

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Component 1: Bug fixes (foundation — must do first)           │
│  • Mid-list system message → find + eliminate source            │
│  • Malformed args (single-quoted JSON) → extend sanitizer      │
└─────────────────────────────────────────────────────────────────┘
                              │ (without these, qwen can't call tools)
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  Component 2: LLM call count reduction (8-15 → 3-5 per turn)   │
│  • Per-model fast-mode override (qwen3-specific knobs)         │
│  • Disable goal-contract forces + verify nudges for qwen3       │
│  • Tighten self-eval to 1 re-plan + 5s timeout (keep safety)    │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  Component 3: vLLM prefix caching (~30-50% speedup)            │
│  • Add --enable-prefix-caching to vLLM startup                  │
│  • Pin system prompt at position 0 (never append mid-list)      │
│  • Stable tool schema ordering (no random shuffle)              │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  Component 4: Synthesis quality on qwen3.6-27b                 │
│  • CEO-grade synthesis prompt with markdown scaffolding         │
│  • Inline data rows in the synthesis prompt (≤100 rows)         │
│  • Structured output: Executive Summary → Key Metrics →        │
│    Breakdown → Risks → Recommendations (markdown headers)      │
│  • Cap max_tokens=3072 for synthesis (was 1536)                 │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  Component 5: Deterministic fallback (never "Sorry")           │
│  • Enhanced _data_rows_fallback → markdown table + numeric    │
│    summary (totals, top-3, date range)                          │
│  • Deterministic executive summary template (fills in numbers  │
│    from the rows, no LLM needed)                                │
│  • Fires when qwen synthesis returns <100 chars OR apology      │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  Component 6: vLLM server config (3 new flags)                 │
│  • --enable-prefix-caching                                      │
│  • --enable-chunked-prefill                                     │
│  • --max-num-seqs 16                                            │
└─────────────────────────────────────────────────────────────────┘
```

## Component 1: Bug fixes

### 1.1 Mid-list system message fix

**Root cause (2 sources in `agents.py`):**

1. **Line 1731-1766** — `_inject_reflexion_critique()` appends `{"role": "system", "content": "..."}` after tool results, mid-conversation. Fires whenever any tool call returns `success: False` with an error — on qwen3.6-27b this is *every* turn because of bug 1.2 (malformed args → "Invalid arguments" → reflexion fires → mid-list system → vLLM 400).

2. **Line 5363-5371** — the web search handler appends `{"role": "system", "content": "You DO have access to web_search..."}` mid-list when building followup messages.

**Fix strategy (3 layers, defense-in-depth):**

| Layer | What | Why |
|---|---|---|
| 1. Source fix | Convert both `_inject_reflexion_critique` and the web-search handler to use `{"role": "user", ...}` instead of `{"role": "system", ...}` | Eliminates the bug at the source |
| 2. Safety guard | Add a pass in `sanitize_messages()` that demotes any `{"role": "system"}` message not at index 0 to `{"role": "user"}` | Defense-in-depth: catches any future code path that re-introduces the bug |
| 3. Integration test | Test that constructs a message list with a mid-list system message and verifies it gets demoted | Catches regressions |

### 1.2 Malformed tool_call arguments fix

**Current behavior** in `repair_tool_call_arguments()` (`message_sanitization.py:132-194`):
- Tries `json.loads(strict=False)` — handles control chars ✅
- Strips trailing commas ✅
- Last resort: returns `"{}"` — tool gets empty args, returns "Invalid arguments" ❌

**Missing case:** qwen3.6-27b emits args as Python-style dict literals: `'query': 'show me sales'` (single quotes). Standard JSON parsers reject this.

**Fix:** Add a new repair pass between the existing `json.loads(strict=False)` and the trailing-comma pass:

```python
import ast

def _try_python_literal_eval(raw: str) -> str | None:
    """Repair single-quoted Python dict literals (e.g. 'query': 'value')
    by using ast.literal_eval, then re-serialize as valid JSON."""
    try:
        parsed = ast.literal_eval(raw)
        if isinstance(parsed, dict):
            return json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)
    except (ValueError, SyntaxError):
        return None
```

This handles:
- `'query': 'show me sales'` → `{"query":"show me sales"}`
- `{'query': 'show me sales'}` → `{"query":"show me sales"}`
- Mixed quotes: `{"query": 'show me sales'}` → `{"query":"show me sales"}`

### 1.3 Test plan

1. **Reflexion test**: build a tool failure, call `_inject_reflexion_critique`, assert the appended message has `role: "user"` not `role: "system"`.
2. **Web-search handler test**: assert the followup message uses `role: "user"`.
3. **Mid-list system guard test**: construct `[system, user, system, user]`, run `sanitize_messages`, assert the second system becomes `user`.
4. **Single-quoted JSON repair test**: feed `'query': 'show sales'` to `repair_tool_call_arguments`, assert valid JSON output.
5. **End-to-end**: feed a qwen-shaped tool_call (single-quoted args) into `sanitize_messages`, assert the args are repaired (not replaced with `{}`).

## Component 2: LLM call count reduction

### 2.1 Per-model fast-mode override

**Strategy:** Keep global flags for deepseek-chat; add qwen3-specific overrides. Same code path, just different knobs. Smallest code change.

**Config additions (`config.py`):**

```python
QWEN3_FAST_MODE_ENABLED: bool = True               # master switch
QWEN3_MAX_TOOL_ITERATIONS: int = 10                 # was 40
QWEN3_GOAL_CONTRACT_ENABLED: bool = False           # was True
QWEN3_VERIFY_NUDGE_MAX: int = 0                     # was 2
QWEN3_DATA_AGENT_BUDGET_SECONDS: float = 30.0       # was 60
QWEN3_FORCE_SYNTHESIS_MAX_RETRIES: int = 0          # was 1
# Self-eval KEPT per user request (safety net), but tightened:
QWEN3_SELF_EVAL_MAX_REPLANS: int = 1                # was 3
QWEN3_SELF_EVAL_LLM_GATE_TIMEOUT_S: float = 5.0     # was 15.0
```

**Helper (`agents.py`):**

```python
def _is_qwen_local_model(endpoint) -> bool:
    """True iff the user's selected model is a qwen3.* served on local vLLM."""
    if not endpoint or not endpoint.model_id:
        return False
    mid = endpoint.model_id.lower()
    if not mid.startswith("qwen3"):
        return False
    return (endpoint.is_private or
            str(endpoint.api_key or "").strip() in {"", "EMPTY"})
```

### 2.2 Call sites where overrides apply

| Call site | Current | Qwen3 override |
|---|---|---|
| `MAX_TOOL_ITERATIONS` in v3 loop range | 40 | 10 |
| `_check_answer_verification_gate` call | runs if `SELF_EVAL_REPLAN_ENABLED` | runs, but capped at 1 re-plan + 5s timeout |
| `_contract` construction | runs if `GOAL_CONTRACT_ENABLED` | skip if qwen3 |
| `_verify_nudge` cap | `VERIFY_NUDGE_MAX=2` | 0 |
| `DATA_AGENT_BUDGET_SECONDS` | 60s | 30s |
| `_force_llm_synthesis(max_retries=...)` | 1 | 0 |

### 2.3 Expected LLM call budget per turn (qwen3.6-27b)

```
Tool-calling iterations:    2-3 calls × 5s = 10-15s   (was 8-12 × 5s = 40-60s)
Self-eval re-plan:          0-1 call × 5s = 0-5s      (was 3 × 15s = 45s) ← KEPT but tightened
Goal-contract forces:        0 calls                  (was 3 × 5s = 15s)
Verify nudges:              0 calls                  (was 2 × 5s = 10s)
Final synthesis (Tier 1):   1 call × 8s = 8s         (was 1-2 × 8s = 8-16s)
─────
Total LLM time:             18-28s                   (was 113-141s)
+ ask_data_agent execution: 30s max                  (was 60s)
─────
Total turn time:            ~50-58s max              (was ~200s) — 3.5x speedup
```

### 2.4 Trade-offs the customer accepts

- **No goal-contract forces:** deferred deliverables (e.g. "give me a PPT") won't auto-trigger the 3-force cascade. Customer must explicitly ask. Acceptable for a 27B model — the cascade was tuned for deepseek-chat's stronger reasoning.
- **No verify nudges:** agent doesn't re-verify its answer before delivering. Acceptable: deterministic fallback (Component 5) catches empty/apology answers.
- **30s ask_data_agent cap:** very large queries truncate at 30s. Acceptable: partial rows + `truncated=True` are still useful.
- **Self-eval kept but tightened:** max 1 re-plan (not 3), 5s timeout (not 15s). On qwen's local vLLM, 5s is plenty — the model is already loaded in memory.

### 2.5 Test plan

1. Unit test `_is_qwen_local_model` (True for qwen3.* + is_private; False for deepseek-chat; False for qwen3 with real API key)
2. Integration test: simulate a qwen3 endpoint, verify `MAX_TOOL_ITERATIONS` is 10 not 40 in the v3 loop
3. Integration test: simulate a deepseek endpoint, verify settings are unchanged (regression guard)
4. Latency test: mock 5s per LLM call, run a turn, assert total < 60s

## Component 3: vLLM prefix caching

### 3.1 Recommended vLLM startup command

```bash
python -m vllm.entrypoints.openai.api_server \
  --model /data/models/qwen3.6-27b-awq4 \
  --served-model-name qwen3.6-27b \
  --quantization awq \
  --max-model-len 65536 \
  --gpu-memory-utilization 0.90 \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_xml \
  --reasoning-parser qwen3 \
  --enable-prefix-caching \           # ← NEW
  --enable-chunked-prefill \          # ← NEW
  --max-num-seqs 16 \                 # ← NEW
  --max-num-batched-tokens 8192 \
  --disable-log-requests
```

### 3.2 Code-side requirements (verified)

| Requirement | Status | Notes |
|---|---|---|
| 1. System prompt at position 0 | Will be fixed by Component 1 | Mid-list system message bug fix |
| 2. Stable tool schema ordering within a turn | Already OK | `tools = get_tools(...)` called once per turn (line 9055); list object is reused |
| 3. No timestamp/random in system prompt | Already OK | `_runtime_context_block()` computed once per turn |
| 4. Compact data source runtime | Already done (commit c1806ce) | Smaller system prompt = better cache hit ratio |

**No code changes needed for prefix caching** — just the vLLM server flag. Component 1's mid-list system fix is the only code-side requirement.

### 3.3 Expected speedup

For a 4-call turn on qwen3.6-27b:
- Call 1 (cold): prefill ~5-10k tokens = ~3s
- Calls 2-4 (cache hit): prefill just new tokens = ~0.5s each
- **Saves ~6s per turn** (3 calls × 2s saved each)

Combined with Component 2: total LLM time drops from 18-28s → 12-22s.

### 3.4 Test plan

1. Verify the vLLM server responds with `cached_tokens > 0` in usage stats when prefix caching is enabled (integration test against the live server)
2. Verify the system prompt is byte-identical across LLM calls within a turn (capture prompts, assert equality)
3. Verify tool schema order is stable within a turn (capture tool lists, assert equality)

## Component 4: Synthesis quality on qwen3.6-27b

### 4.1 CEO-grade synthesis prompt (qwen3-specific)

Replace the generic synthesis prompt with a structured scaffolding prompt:

```
You are a CEO-grade business analyst. Using ONLY the data below, write a
comprehensive answer in this exact structure:

## Executive Summary
[2-3 sentences: what was asked, what was found, the headline number]

## Key Metrics
- Total {measure}: ¥{value}
- Date range covered: {range}
- Number of records: {N}
- Top {dimension}: {value}

## Detailed Breakdown
[markdown table of top 10-20 rows, or bullets if many dimensions]

## Risks & Opportunities
[1-2 sentences: notable patterns, anomalies, trends]

## Recommended Actions
[1-2 actionable items based on the data]

DATA (use only this, do not invent numbers):
{data_rows_inline}
```

### 4.2 Inline data rows in synthesis prompt

Currently the synthesis LLM sees data as a separate tool result message. For qwen3.6-27b (weaker attention), inline the rows **directly** in the synthesis prompt (≤100 rows, ≤4000 chars). This eliminates the "look up the data" step.

### 4.3 Cap max_tokens=3072 for synthesis (was 1536)

1536 tokens is too low for comprehensive CEO-grade prose with markdown structure. 3072 gives ~2000 words — enough for the 5-section template above. Still leaves headroom in the 65,536 context window.

### 4.4 Lower temperature for synthesis

qwen3 is a reasoning model so temperature is already dropped via `LLM_FIXED_TEMPERATURE_MODELS`. But for synthesis we want even more determinism — set `temperature=0.3` explicitly for synthesis calls.

### 4.5 Skip synthesis entirely for trivial questions

If the user's question is trivial ("what's 2+2?", "hello", "thanks"), skip the LLM synthesis call entirely and use a deterministic response. Saves 8s per trivial turn.

### 4.6 Test plan

1. Verify the new synthesis prompt produces markdown-structured output (not free-form prose)
2. Verify inline data rows appear in the prompt sent to the LLM (capture the prompt, assert data is there)
3. Verify `max_tokens=3072` is set for synthesis calls when model is qwen3
4. Verify trivial questions skip the LLM call entirely

## Component 5: Deterministic fallback (never "Sorry")

### 5.1 Enhanced `_data_rows_fallback`

Currently renders a basic markdown table. Enhance to:

```markdown
## Executive Summary (auto-generated)

Based on {N} rows from {table_name}, covering {date_range}:

- Total {measure}: ¥{total:,.2f}
- Average {measure}: ¥{avg:,.2f}
- Top {dimension}: {top_value} (¥{top_value_amount:,.2f})
- Date range: {min_date} to {max_date}
- Number of records: {N}

## Data Preview (top 20 rows)

| col1 | col2 | col3 |
|---|---|---|
| ... | ... | ... |

*Note: LLM synthesis unavailable. Showing raw data summary.*
```

### 5.2 Deterministic numeric summary (no LLM)

Pure Python computation from the rows:
- Detect numeric columns → compute sum/avg/min/max
- Detect date columns → compute range
- Detect categorical columns → compute top-3 values
- Render as markdown bullets + table

### 5.3 Trigger conditions

The deterministic fallback fires when ANY of:
- Synthesis LLM returns empty string after all retries
- Synthesis LLM returns apology/bounce-back ("I couldn't...", "Let me re-query...")
- Synthesis LLM returns <100 chars
- Synthesis LLM call times out (5s for qwen)
- Synthesis LLM call raises an exception

### 5.4 User-facing UX

The fallback message is **honest**: it tells the user "LLM synthesis unavailable, showing raw data summary". This is much better than "Sorry, I hit an error" — the user gets the data + a basic summary, and knows to ask a follow-up if they want prose.

### 5.5 Test plan

1. Feed empty synthesis output → assert deterministic fallback renders with numeric summary
2. Feed apology text ("I couldn't find...") → assert fallback fires
3. Feed <100 char output → assert fallback fires
4. Feed timeout exception → assert fallback fires
5. Feed valid 2000-char synthesis → assert fallback does NOT fire (LLM output is used)

## Component 6: vLLM server config

### 6.1 Three new flags for the customer's IT team

| Flag | Purpose | Impact |
|---|---|---|
| `--enable-prefix-caching` | Cache system prompt prefix across calls | -6s/turn |
| `--enable-chunked-prefill` | Long prompts don't block short ones | Better concurrency |
| `--max-num-seqs 16` | Support 16 concurrent users | Multi-user support |

**No GPU memory impact** — prefix caching uses existing KV cache, just reuses it intelligently.

### 6.2 No code changes

This is purely an ops task — restart vLLM with the 3 new flags. No zhanlu backend changes needed.

## Implementation order

1. **Component 1 (bug fixes)** — must be first; without it qwen can't call tools at all
2. **Component 2 (call count reduction)** — depends on Component 1's system-prompt-at-position-0 invariant
3. **Component 4 (synthesis quality)** — can be done in parallel with Component 2
4. **Component 5 (deterministic fallback)** — can be done in parallel with Component 2
5. **Component 3 + 6 (vLLM config)** — ops task; restart vLLM after Component 1 is deployed

## Full test summary

| Component | Tests | Type |
|---|---|---|
| 1. Bug fixes | 5 | Unit + integration |
| 2. Call count reduction | 4 | Unit + integration + latency |
| 3. Prefix caching | 3 | Integration (live server) |
| 4. Synthesis quality | 4 | Unit + integration |
| 5. Deterministic fallback | 5 | Unit |
| **Total** | **21** | |

## Files to modify

| File | Components | Changes |
|---|---|---|
| `backend/app/services/message_sanitization.py` | 1 | Add `_try_python_literal_eval` + mid-list system guard |
| `backend/app/routers/agents.py` | 1, 2, 4, 5 | Reflexion critique → user role; web-search handler → user role; `_is_qwen_local_model` helper; per-model overrides at call sites; synthesis prompt + inline data; enhanced `_data_rows_fallback` |
| `backend/app/config.py` | 2 | Add `QWEN3_*` config flags |
| `backend/app/services/tool_handlers/delegation_tools.py` | 2 | Per-model `DATA_AGENT_BUDGET_SECONDS` override |
| `backend/tests/test_message_sanitization.py` (or new) | 1 | 5 bug-fix tests |
| `backend/tests/test_qwen3_fast_mode.py` (new) | 2 | 4 call-reduction tests |
| `backend/tests/test_synthesis_quality.py` (new) | 4 | 4 synthesis-quality tests |
| `backend/tests/test_deterministic_fallback.py` (new) | 5 | 5 fallback tests |

## Trade-offs summary

| Trade-off | Acceptable because |
|---|---|
| No goal-contract forces for qwen3 | The cascade was tuned for deepseek-chat's stronger reasoning; qwen3 27B benefits more from going straight to synthesis |
| No verify nudges for qwen3 | Deterministic fallback (Component 5) catches empty/apology answers |
| 30s ask_data_agent cap (was 60s) | Partial rows + `truncated=True` are still useful |
| Self-eval kept but tightened (1 re-plan, 5s timeout) | Local vLLM is faster than cloud API; 5s is plenty for a loaded model |
| Synthesis quality lower than deepseek-chat | Acceptable for a 27B local model; markdown scaffolding + inline data helps; deterministic fallback covers failures |
| 100% offline (no DeepSeek API) | Customer requirement; all synthesis goes through qwen3.6-27b |

## Expected end-state

- Turn time: ~200s → ~42-52s (4x speedup)
- "Sorry, I hit an error" → never (deterministic fallback)
- Synthesis quality: acceptable for a 27B local model (markdown-structured, CEO-grade prompt, inline data)
- 100% offline (no DeepSeek API calls)
- LLM calls per turn: 8-15 → 3-5
