# Zhanlu Agent Reliability Architecture

> Ported from [Hermes Agent](../../hermes-agent-main) — 17 modules, 193 tests, 337 total passing.

## Overview

Zhanlu agents now have Hermes-grade reliability infrastructure across 7 layers:

| Layer | Modules | Phase |
|-------|---------|-------|
| Loop Safety | `tool_loop_guardrails`, `iteration_budget` | P0 |
| Context Management | `tool_result_persistence`, `compaction/pre_api_prune`, `prompt_caching` | P0/P1/P3 |
| Error Recovery | `api_error_classifier`, `provider_fallback` | P1/P4 |
| Message Hygiene | `message_sanitization` | P2 |
| Tool Intelligence | `tool_result_classification`, `verification_stop`, `coding_context` | P1/P2/P5 |
| Self-Improvement | `background_review`, `memory_manager`, `skill_curator` | P2/P5 |
| Observability | `agent_metrics`, `alerting`, `scheduled_tasks` | P4/P6 |

## The Turn Loop Pipeline

All 3 turn loops (main `add_message`, resume, stream) share the same reliability pipeline:

```
┌─────────────────────────────────────────────────────────────────────┐
│                         TURN LOOP                                    │
│                                                                      │
│  1. Iteration Budget Check                                          │
│     └─ conv_budget.consume() → break if exhausted                   │
│                                                                      │
│  2. History Loop Guard (existing)                                   │
│     └─ _detect_tool_call_loop(llm_messages) → nudge if repeat       │
│                                                                      │
│  3. Pre-API Pipeline (before LLM call)                              │
│     ├─ prune_tool_results_only(llm_messages)  [P1: deterministic]   │
│     ├─ sanitize_messages(llm_messages)         [P2: surrogates,     │
│     │                                           arg repair, sequence]│
│     └─ apply_cache_control(messages)           [P3: cache_control]  │
│                                                                      │
│  4. LLM API Call (_call_llm_with_tools)                             │
│     ├─ model_override for fallback             [P4: provider switch]│
│     └─ Error handler:                                               │
│         ├─ classify_api_error(e)               [P1: structured]     │
│         ├─ if should_compress → reactive compaction                 │
│         ├─ if should_fallback → with_fallback() [P4: model switch]  │
│         └─ metrics.record_error()              [P4: observability]  │
│                                                                      │
│  5. Tool Execution                                                  │
│     ├─ guard_ctrl.before_call(name, args)      [P0: loop guard]     │
│     │   └─ block if repeat-failure/no-progress                      │
│     ├─ execute_tool_with_retry(name, args)     [P1: error classify] │
│     │   └─ is_retryable → classify_api_error()                      │
│     ├─ _persisted_result_str(result)           [P0: Layer 2 disk]   │
│     ├─ guard_ctrl.after_call(name, args, result) [P0: track outcome]│
│     ├─ _apply_turn_budget_to_messages()        [P0: Layer 3 spill]  │
│     ├─ if guard_ctrl.halt_decision → break     [P0: halt nudge]     │
│     ├─ if execute_code succeeded → refund      [P0: budget refund]  │
│     └─ metrics.record_guardrail_halt()         [P4: observability]  │
│                                                                      │
│  6. No-Tool-Calls Exit (LLM tries to finish)                       │
│     ├─ Hallucination guardrail (existing)                           │
│     ├─ build_verify_on_stop_nudge()             [P2: verify code]   │
│     │   └─ detect_project_facts() → "run pytest" [P5: coding ctx]   │
│     └─ break if no nudge                                             │
│                                                                      │
│  7. Post-Turn                                                       │
│     ├─ spawn_background_review()               [P2: memory review]  │
│     │   └─ run_consolidation()                 [P5: semantic dedup] │
│     └─ return response                                               │
└─────────────────────────────────────────────────────────────────────┘
```

## Module Reference

### P0: Loop Safety

#### `tool_loop_guardrails.py`
Per-turn controller detecting 3 loop patterns:
- **Exact-failure**: same tool + same args failing repeatedly (block after 5)
- **Same-tool-failure**: same tool failing with different args (halt after 8)
- **No-progress**: idempotent tool returning identical results (block after 5)

Uses `tool_result_classification` (P1) as the source of truth for idempotent/mutating tool sets.

#### `iteration_budget.py`
Thread-safe per-conversation consume/refund counter. Bounds total iterations across all turns (including resumes). `execute_code` turns are refunded. Per-agent override via `AgentApp.max_call_count`.

#### `tool_result_persistence.py`
3-layer context overflow protection:
- **Layer 1**: per-tool cap (existing `truncate_output`)
- **Layer 2**: per-result disk persistence + inline preview (threshold: 20K chars, context-scaled)
- **Layer 3**: per-turn aggregate budget spill (threshold: 80K chars, context-scaled)

`read_file` is pinned as no-persist (prevents persist→read→persist loops).

### P1: Smarter Recovery

#### `api_error_classifier.py`
15-reason enum (`FailoverReason`) with `ClassifiedError` carrying `retryable`, `should_compress`, `should_fallback`. Replaces inline string matching. Status-code + string-based classification.

Wired into both LLM call error handler AND `tool_retry.is_retryable()`.

#### `tool_result_classification.py`
Single source of truth for tool side effects:
- `NO_EFFECT_TOOL_NAMES`: read_file, web_search, etc.
- `FILE_MUTATING_TOOL_NAMES`: write_file
- `STATE_MUTATING_TOOL_NAMES`: execute_code, create_agent, etc.
- `is_safe_to_discard_on_interrupt()`: for interrupt recovery
- `file_mutation_result_landed()`: for checkpoint decisions

#### `compaction/pre_api_prune.py`
Deterministic, no-LLM 3-pass prune before each API call:
1. **Dedup**: byte-identical tool results → back-reference
2. **Summarize**: old large results → placeholder
3. **Truncate**: oversized old tool_call args → truncated

Skipped below 16K tokens trigger threshold.

### P2: Post-Turn Intelligence

#### `message_sanitization.py`
3-pass pre-API sanitization:
1. **Surrogates**: replace U+D800-U+DFFF with U+FFFD (prevents json.dumps crash)
2. **Tool call args**: repair malformed JSON (trailing commas, unclosed braces)
3. **Interrupted sequences**: append synthetic assistant turn after orphaned tool message

#### `verification_stop.py`
Detects "wrote code → tried to finish without verifying" and nudges the agent. Filters non-code files (.md, .txt, etc.). Max 2 nudges per turn. Auto-detects project facts (P5) for specific test command suggestions.

#### `background_review.py`
Async fire-and-forget post-turn review. Every ~5 turns, spawns an asyncio task that asks the LLM "should anything be saved to memory?" with memory-tool-only whitelist. Uses digest history for long conversations. After review, triggers memory consolidation (P5).

### P3: Polish

#### `prompt_caching.py`
Applies up to 4 `cache_control` breakpoints (system prompt + last 3 messages). No-op when disabled (DeepSeek default — automatic prefix caching). Config: `PROMPT_CACHE_ENABLED`, `PROMPT_CACHE_TTL`.

### P4: Operational & Observability

#### `agent_metrics.py`
Thread-safe `Counter` + `Histogram` primitives tracking all reliability features. 10 metric categories: guardrails, iteration budget, result persistence, pre-API prune, error classification, prompt caching, verification-on-stop, provider fallback, message sanitization, background review.

Exposed via `GET /api/_agent-metrics`.

#### `provider_fallback.py`
Tries configured fallback models when `should_fallback=True` (e.g. model_not_found). Uses `LLM_FALLBACK_PROVIDERS` config. Skips primary model. `_call_llm_with_tools` accepts `model_override` parameter.

### P5: Intelligence

#### `memory_manager.py`
Extends `memory_advanced` with:
- **Semantic dedup**: embedding cosine similarity (threshold: 0.85)
- **Consolidation pipeline**: remove expired → merge duplicates → archive stale → promote frequent
- **Lifecycle**: low-importance + unused + old → archived; high-usage → promoted

#### `skill_curator.py`
- **Overlap detection**: Jaccard token similarity (threshold: 0.6)
- **Stale detection**: unused or not used in 60 days
- **Suggestions only**: does NOT auto-modify skills

Exposed via `GET /api/_skill-curation`.

#### `coding_context.py`
File-system-based project fact detection:
- Languages: Python, JS/TS, Rust, Go, Java, Ruby, C/C++
- Test commands: pytest, npm test, cargo test, go test, make test
- Build commands, lint commands, framework, package manager
- `get_test_command_for_files()`: maps file extensions to test command

Injected into system prompt at both `add_message` and `stream_add_message` sites.

### P6: Production Integration

#### `scheduled_tasks.py`
3 periodic asyncio background tasks:
- Memory consolidation: every 30 min
- Skill curation: every 6 hours
- Alert checks: every 5 min

Started at app startup via `@app.on_event("startup")`.

#### `alerting.py`
7 threshold-based alerts:
- Guardrail halt rate (>10)
- No-progress rate (>10)
- Budget exhaustion (>20)
- Context overflow rate (>10)
- Rate limit rate (>20)
- Fallback failures (>5)
- Sanitization arg repairs (>50)

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `AGENT_MAX_ITERATIONS` | 50 | Per-conversation total iteration cap |
| `TOOL_RESULT_STORAGE_DIR` | "tool_results" | Disk storage for persisted results |
| `PROMPT_CACHE_ENABLED` | False | Enable explicit cache_control markers |
| `PROMPT_CACHE_TTL` | "5m" | Cache TTL ("5m" or "1h") |
| `LLM_FALLBACK_PROVIDERS` | "" | Comma-separated fallback model names |
| `AgentApp.max_call_count` | null | Per-agent iteration budget override |
| `TOOL_MAX_OUTPUT_CHARS` | 8000 | Per-tool output truncation (Layer 1) |
| `MAX_TOOL_ITERATIONS` | 10 | Per-turn iteration cap (existing) |

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/_agent-metrics` | JSON snapshot of all reliability metrics |
| `GET /api/_skill-curation` | Skill overlap + stale detection report |

## Testing

| Category | Count |
|----------|-------|
| Unit tests (per module) | 151 |
| Integration tests (cross-module) | 14 |
| E2E tests (full pipeline) | 8 |
| Performance benchmarks | 6 |
| Broader regression tests | 158 |
| **Total** | **337+** |

All tests pass with 0 regressions.
