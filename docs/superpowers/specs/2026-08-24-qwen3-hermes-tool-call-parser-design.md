# Qwen3 Hermes-Qwen Tool-Call Parser — Design Specification

**Date**: 2026-08-24
**Status**: Approved (Approach B)
**Scope**: `backend/app/routers/agents.py`, `backend/tests/test_extract_tool_calls_qwen3_hermes.py`

---

## 1. Overview

Zhanlu's tool-call parser at `backend/app/routers/agents.py:6854-6972` recognizes three legacy formats for tool calls embedded in assistant content text. When the Qwen3 family (e.g. `qwen3.6-27b`) is served via vLLM with `--tool-call-parser=qwen3_xml`, the model emits a **fourth canonical format** — `<tool_call>{...}</tool_call>` — that the parser does not recognize. The result: tool calls are silently dropped, the assistant content is reduced to markup-only (and then stripped to empty), and the chat surfaces "Sorry, I hit an error while responding."

This spec adds Format 4 parsing with whitespace tolerance, defensive JSON handling, and matching strip logic, so the Qwen3 family works out of the box while preserving all existing behaviour for Formats 1–3 (which other models — DeepSeek, Moonshot, OpenAI — continue to depend on).

### Why now

- **Active customer impact**: `qwen3.6-27b` is bound to `Data Analyst` in the `Global` project; user cannot use the agent.
- **Server-side already aligned**: vLLM was launched with `--enable-auto-tool-choice --tool-call-parser=qwen3_xml --reasoning-parser=qwen3 --max-model-len=32768`.
- **Multi-model promise**: hierarchical LLM routing already supports per-project and per-agent bindings. The parser gap is the only thing breaking the multi-model path for the Qwen family.

### Out of scope (explicit)

This spec is **parser-only**. The following are deliberately excluded and should be raised as follow-ups if desired:

- Surfacing the actual backend error reason in the chat UI bubble (currently masked as "Sorry, I hit an error").
- Pre-save validation in `POST/PUT /api/llm/models`.
- A `/diagnose` endpoint for full-shape chat+tool round-trip.
- Reasoning-content passthrough for `--reasoning-parser=qwen3` (DeepSeek has this today at `sub_agent_reliability.py:62`; Qwen3 may need the same).

---

## 2. Background: Existing parser

The function `_extract_tool_calls_from_content` parses three legacy formats:

| Format | Example | Use case |
|---|---|---|
| 1 — qwen3 XML-style | `<function=NAME><parameter=key>val</parameter></function>` | Older qwen3 vLLM configs |
| 2 — Hermes `✅` | `✅\n{"name":"f","arguments":{...}}` | Hermes-style line prefix |
| 3 — Qwen native plain text | `Function: NAME\nArguments: {...}` | Qwen native chat template, plain text |

The companion `_strip_tool_call_markup` removes these blocks before the content is shown to the user or stored as assistant_content.

Both functions are pure (no DB, no LLM, no async), called from three sites:

| Site | Line | Path |
|---|---|---|
| Non-streaming | 6715 | `_call_llm_with_tools` |
| Streaming | 7401 | `_stream_llm_with_tools` |
| SSE content-replace | 9688 | v3 streaming |

---

## 3. The new format

### 3.1 Canonical Qwen3 / Hermes-Qwen tag

```
<tool_call>
{"name": "func_name", "arguments": {"arg1": "value1", "arg2": "value2"}}
</tool_call>
```

Key properties observed in production:

- **Tag is XML-like** but with surrounding whitespace tolerated by vLLM (`<\n  tool_call\n  >`, `<\n  /tool_call\n  >`).
- **Inner content is JSON** with the same shape as legacy Format 3 (`name` + `arguments`).
- **Multiple tool calls** in one response appear as multiple `<tool_call>...</tool_call>` blocks, possibly interleaved with prose / thinking.
- **Under load**, vLLM may emit a truncated or malformed block (e.g. `<tool_call>{"name":"f","arguments":` with no closing tag). The parser must skip these without poisoning other blocks.

### 3.2 Why this format exists

`--tool-call-parser=qwen3_xml` on vLLM is supposed to parse this format and raise it into `response.tool_calls`. In some configurations (oversized tool schema, certain Qwen3 chat templates, partial completion), it falls back to leaving the markup in `response.content`. The backend's `_extract_tool_calls_from_content` is the defensive fallback that recovers tool calls from such content.

---

## 4. Design

### 4.1 Detection algorithm

Add a 4th block at the **end** of `_extract_tool_calls_from_content`, after the existing Format 3 block and before `return tool_calls`:

```python
# ── Format 4: canonical Qwen3 / Hermes-Qwen <tool_call>{json}</tool_call> ──
# Emitted by vLLM with `--tool-call-parser=qwen3_xml` when the structured
# tool_calls path is bypassed (e.g. mid-stream truncation, oversized tool
# schema, or as a defensive fallback in some Qwen3 chat templates).  The
# inner JSON is the same shape as legacy Format 3 (`Function:/Arguments:`).
#
# Whitespace-tolerant to handle observed real-world drift:
#   - extra whitespace inside the tag (`<  tool_call  >`)
#   - leading/trailing whitespace around the JSON
#   - newlines between tag and JSON
#
# Per-tag try/except so one malformed block does not poison the rest of
# the response (vLLM can emit half-finished tags under load).
_hermes_tag_re = re.compile(
    r"<\s*tool_call\s*>\s*(\{.*?\})\s*<\s*/\s*tool_call\s*>",
    re.DOTALL,
)
for _m in _hermes_tag_re.finditer(content):
    raw = _m.group(1).strip()
    try:
        _obj = json.loads(raw)
    except (ValueError, TypeError):
        logger.debug("Format 4: skipping malformed JSON in tool_call tag: %r", raw[:80])
        continue
    if not isinstance(_obj, dict):
        continue
    _fn = _obj.get("name")
    if not isinstance(_fn, str) or not _fn:
        continue
    _args = _obj.get("arguments", {})
    if not isinstance(_args, dict):
        _args = {}
    tool_calls.append({
        "id": f"call_{_uuid.uuid4().hex[:24]}",
        "type": "function",
        "function": {
            "name": _fn,
            "arguments": json.dumps(_args, ensure_ascii=False),
        },
    })

if tool_calls:
    return tool_calls
```

### 4.2 Strip algorithm

Add a 4th `re.sub` **first** (before the existing three) in `_strip_tool_call_markup`:

```python
# Strip canonical Qwen3 / Hermes-Qwen <tool_call>...</tool_call> blocks
# FIRST — these blocks contain raw JSON that the existing legacy regexes
# would partially match (false positive on the inner "name"/"arguments"
# strings).  Whitespace-tolerant for the same reasons as the extractor.
clean = re.sub(
    r"<\s*tool_call\s*>.*?<\s*/\s*tool_call\s*>",
    "",
    content,
    flags=re.DOTALL,
).strip()
clean = re.sub(r"<function=\w+>.*?</function>", "", clean, flags=re.DOTALL).strip()
clean = re.sub(r"✅\s*\n?\s*\{.*?\}", "", clean, flags=re.DOTALL).strip()
clean = re.sub(r"Function:\s*\w+\s*\n\s*Arguments:\s*\{.*?\}", "", clean, flags=re.DOTALL).strip()
```

The downstream `len(clean) <= 60` short-circuit and the "thinking monologue" heuristic (line 7024+) remain unchanged.

### 4.3 Why append-only

- Formats 1, 2, 3 continue to work for any model that emits them (DeepSeek, Moonshot, OpenAI, older qwen3).
- The new block's `if tool_calls: return tool_calls` short-circuit means **once Format 4 matches, the function returns only Format 4 calls**. This is intentional — Format 4 is the most specific format and is the canonical Qwen3 output. If a model emits a mix of Format 1 + Format 4, the test `test_extract_priority_order` documents the FIFO semantics.
- The strip pipeline always runs all four regexes, so display content is correctly cleaned regardless of which format the extractor found.

---

## 5. Test coverage

Single file: `backend/tests/test_extract_tool_calls_qwen3_hermes.py`. Pure-function tests; no DB, no LLM, no async.

| # | Name | Input | Expected |
|---|---|---|---|
| 1 | `test_format_4_happy_path` | `<tool_call>{"name":"get_weather","arguments":{"city":"北京"}}</tool_call>` | 1 call, name=`get_weather`, args parsed |
| 2 | `test_format_4_with_whitespace` | `<\n  tool_call\n  >\n  {"name":"f","arguments":{}}\n  <\n  /tool_call\n  >` | 1 call (proves tolerance) |
| 3 | `test_format_4_multiple_in_one_content` | Two tool calls back-to-back with prose between | 2 calls in order |
| 4 | `test_format_4_malformed_json_skipped` | `<tool_call>{"name":"x","arguments":</tool_call>` (truncated) | `[]` (no crash) |
| 5 | `test_format_4_non_dict_json_skipped` | `<tool_call>null</tool_call>` | `[]` |
| 6 | `test_format_4_missing_name_skipped` | `<tool_call>{"arguments":{}}</tool_call>` | `[]` |
| 7 | `test_format_4_args_as_string_normalized` | `<tool_call>{"name":"f","arguments":"{}"}</tool_call>` | call with `args={}` (coerced) |
| 8 | `test_format_4_unicode_in_args` | `{"name":"查询","arguments":{"sql":"SELECT '你好'"}}` | args round-trip with CJK |
| 9 | `test_strip_removes_hermes_tag` | `"preamble<tool_call>{\"name\":\"f\",\"arguments\":{}}</tool_call>postamble"` | `"preamblepostamble"` (re.sub removes the entire tag block; surrounding text concatenates) |
| 10 | `test_strip_handles_malformed_hermes_tag` | `"text <tool_call> broken` (no closing) | original text (no crash) |
| 11 | `test_extract_priority_order` | Content mixing Format 1 AND Format 4 | Format 1's call wins (extracted first); second `if tool_calls: return` ensures no Format 4 in result |
| 12 | `test_strip_priority_order` | Same mix | Both formats stripped, plain text remains |

---

## 6. Error handling guarantees

| Input | Behaviour |
|---|---|
| Empty / `None` content | Returns `[]` (existing guard, unchanged) |
| Single malformed tag | Skipped silently (`logger.debug`); other tags in same content still extracted |
| Non-JSON inside tag | Skipped silently |
| Non-dict JSON inside tag (e.g. `null`, `"string"`) | Skipped silently |
| `name` missing or empty | Skipped silently |
| `arguments` not a dict (e.g. string) | Coerced to `{}` |
| Unclosed tag | Regex non-greedy `.*?` won't match; tag is left as prose (acceptable — strip is best-effort) |
| Output shape | Always a list of OpenAI-shaped dicts; downstream call sites need no change |

---

## 7. Multi-model regression check

The change is append-only. To verify other models are unaffected, the existing chat tests that exercise DeepSeek / OpenAI tool calling should pass unchanged. Specifically:

- `test_call_llm_failover.py` — exercises provider failover; not affected (uses structured `tool_calls`, not parser)
- `test_agents_token_streaming.py` — exercises streaming; the Format 4 block returns `tool_calls` via the same `tc_buffer` path as structured tool_calls

The risk surface is the strip pipeline. Adding the new `re.sub` first could in theory affect content that legitimately contained the literal string `<tool_call>` (e.g. user messages describing the syntax). To eliminate that risk, the strip test `test_strip_handles_malformed_hermes_tag` proves unclosed tags are not stripped — and the strip pipeline is best-effort: the user-facing bubble still shows the markup, but the tool call extraction succeeds. This is acceptable.

---

## 8. Files modified

| File | Change | Approx. lines |
|---|---|---|
| `backend/app/routers/agents.py` | Add Format 4 block to `_extract_tool_calls_from_content` (~30 LoC) | +30 |
| `backend/app/routers/agents.py` | Add new `re.sub` first in `_strip_tool_call_markup` (~5 LoC) | +5 |
| `backend/tests/test_extract_tool_calls_qwen3_hermes.py` | New test file with 12 tests | +140 |

Total: ~175 LoC across 3 files. Single PR, < 1 hour review.

---

## 9. Rollout

1. Code change in `backend/app/routers/agents.py` (single file, two locations).
2. Test file added.
3. `docker restart zhanlu-backend` — picks up the new code (no migration, no flag change).
4. Manual smoke test: chat from `Data Analyst` (or any agent) with `qwen3.6-27b` selected; expect assistant bubble with content, not "Sorry, I hit an error".
5. No frontend changes, no DB migration, no env flag change.

---

## 10. Open follow-ups (out of scope, tracked)

- **UI error surfacing**: replace generic "Sorry, I hit an error" with truncated real backend reason (File: `frontend/src/pages/Chat.jsx` ~line 2333).
- **Pre-save validation**: `POST/PUT /api/llm/models` probe `/v1/models` to catch bad base_url / model_id at config time (File: `backend/app/routers/llm.py` ~line 172).
- **Diagnose endpoint**: `POST /api/llm/models/{id}/diagnose` runs a full chat+tool round-trip (File: `backend/app/routers/llm.py`).
- **Reasoning passthrough**: capture `reasoning_content` for Qwen3 like DeepSeek already does (File: `backend/app/services/sub_agent_reliability.py:62` + v3 streaming path).