# Qwen3 Hermes-Qwen Tool-Call Parser — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Format 4 parsing (`<tool_call>{json}</tool_call>`) to Zhanlu's tool-call extractor so the Qwen3 family served via vLLM with `--tool-call-parser=qwen3_xml` works out of the box.

**Architecture:** Append-only change to `_extract_tool_calls_from_content` and `_strip_tool_call_markup` in `backend/app/routers/agents.py`. New regex handles the canonical Hermes-Qwen tag with whitespace tolerance and defensive JSON parsing. 12 unit tests cover happy path, edge cases, mixed formats, CJK args, and malformed input. Existing Formats 1-3 (DeepSeek, Moonshot, OpenAI, legacy qwen3) are untouched.

**Tech Stack:** Python 3.11+, FastAPI, pytest, stdlib `re` + `json`.

**Spec:** `docs/superpowers/specs/2026-08-24-qwen3-hermes-tool-call-parser-design.md`

---

## Global Constraints

- **Tests use pytest**, function or class scope, no DB, no async, no LLM mocks.
- **Test invocation (local):** `cd backend && venv/bin/python -m pytest tests/test_extract_tool_calls_qwen3_hermes.py -p no:jaxtyping -p no:cacheprovider -v`
- **Test invocation (container):** `docker exec zhanlu-backend /usr/local/bin/python -m pytest tests/test_extract_tool_calls_qwen3_hermes.py -p no:jaxtyping -p no:cacheprovider -v`
- **Backend redeploy:** `docker restart zhanlu-backend` (no migration, no flag change, no rebuild).
- **Format 4 detection regex:** `<\s*tool_call\s*>\s*(\{.*?\})\s*<\s*/\s*tool_call\s*>` with `re.DOTALL`.
- **Strip regex (run FIRST in pipeline):** `<\s*tool_call\s*>.*?<\s*/\s*tool_call\s*>` with `re.DOTALL`.
- **Per-tag JSON parsing wrapped in `try/except (ValueError, TypeError)`** — one malformed block must not poison other blocks.
- **`isinstance(_obj, dict)` guard** before accessing `.get("name")` / `.get("arguments")`.
- **`_args` coerced to dict** if the model emits it as a string (downstream OpenAI strict-type check).
- **Function names (`_extract_tool_calls_from_content`, `_strip_tool_call_markup`) and their call sites (agents.py lines 6687, 6692, 7424, 7427, 9703) are unchanged.**
- **Append-only:** Formats 1-3 logic is not modified. Multi-model users (DeepSeek, Moonshot, OpenAI) are unaffected.

---

## Task Structure

| Task | Deliverable | Files touched | Commit boundary |
|---|---|---|---|
| 1 | Failing tests for Format 4 detection | new test file | one commit |
| 2 | Implement Format 4 detection | `agents.py` + test file | one commit |
| 3 | Failing tests for Format 4 strip | test file (expand) | one commit |
| 4 | Implement Format 4 strip | `agents.py` + test file | one commit |
| 5 | Full backend regression + backend restart | none (verify only) | one commit if logs/memos |

---

## Task 1: Write failing tests for Format 4 detection

**Files:**
- Create: `backend/tests/test_extract_tool_calls_qwen3_hermes.py`

**Interfaces:**
- Consumes: `from app.routers.agents import _extract_tool_calls_from_content` (already exists)
- Produces: 12 test functions (8 detect + 4 strip), grouped in two classes

- [ ] **Step 1: Create the test file scaffold**

Create `backend/tests/test_extract_tool_calls_qwen3_hermes.py`:

```python
"""Tests for Format 4 (canonical Qwen3 / Hermes-Qwen <tool_call>{json}</tool_call>)
parsing in Zhanlu's tool-call extractor.

The Format 4 block is emitted by vLLM with `--tool-call-parser=qwen3_xml`
when the structured tool_calls path is bypassed (oversized tool schema,
partial completion, or as a fallback in some Qwen3 chat templates).  The
inner JSON shape matches legacy Format 3 (`Function:/Arguments:`).

These tests cover: happy path, whitespace tolerance, multiple tool calls
in one content, malformed input, mixed legacy+new formats, CJK args,
missing name, args-as-string coercion.
"""

from __future__ import annotations

from app.routers.agents import (
    _extract_tool_calls_from_content,
    _strip_tool_call_markup,
)


class TestFormat4Detect:
    """Tests for _extract_tool_calls_from_content with Format 4 input."""


class TestFormat4Strip:
    """Tests for _strip_tool_call_markup with Format 4 input."""
```

Run: `cd backend && venv/bin/python -m pytest tests/test_extract_tool_calls_qwen3_hermes.py -p no:jaxtyping -p no:cacheprovider --collect-only`
Expected: PASS (collect-only shows 0 tests; the import alone validates the path is correct).

- [ ] **Step 2: Add `test_format_4_happy_path`**

Add inside `TestFormat4Detect`:

```python
def test_format_4_happy_path():
    """Single tool call in canonical Hermes-Qwen tag format."""
    content = (
        'Let me check the weather.\n'
        '<tool_call>'
        '{"name": "get_weather", "arguments": {"city": "北京"}}'
        '</tool_call>'
    )
    calls = _extract_tool_calls_from_content(content)
    assert len(calls) == 1, f"expected 1 call, got {len(calls)}: {calls}"
    assert calls[0]["function"]["name"] == "get_weather"
    import json as _json
    assert _json.loads(calls[0]["function"]["arguments"]) == {"city": "北京"}
```

Run: `cd backend && venv/bin/python -m pytest tests/test_extract_tool_calls_qwen3_hermes.py::TestFormat4Detect::test_format_4_happy_path -p no:jaxtyping -p no:cacheprovider -v`
Expected: FAIL — `[]` (no parser branch matches the canonical tag yet).

- [ ] **Step 3: Add `test_format_4_with_whitespace`**

```python
def test_format_4_with_whitespace():
    """Tag with whitespace drift inside and around the JSON."""
    content = (
        '<\n  tool_call\n  >\n'
        '  {"name": "f", "arguments": {"k": "v"}}\n'
        '  <\n  /tool_call\n  >'
    )
    calls = _extract_tool_calls_from_content(content)
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "f"
    import json as _json
    assert _json.loads(calls[0]["function"]["arguments"]) == {"k": "v"}
```

Run: same command, swap test name.
Expected: FAIL — parser does not match the whitespace variant yet.

- [ ] **Step 4: Add `test_format_4_multiple_in_one_content`**

```python
def test_format_4_multiple_in_one_content():
    """Two tool calls back-to-back with prose between."""
    content = (
        '<tool_call>'
        '{"name": "first", "arguments": {}}'
        '</tool_call>'
        ' Some text between.\n'
        '<tool_call>'
        '{"name": "second", "arguments": {"x": 1}}'
        '</tool_call>'
    )
    calls = _extract_tool_calls_from_content(content)
    assert len(calls) == 2
    assert calls[0]["function"]["name"] == "first"
    assert calls[1]["function"]["name"] == "second"
```

Run: same pattern. Expected: FAIL — 0 calls.

- [ ] **Step 5: Add `test_format_4_malformed_json_skipped`**

```python
def test_format_4_malformed_json_skipped():
    """Truncated JSON inside the tag is skipped silently (no crash)."""
    content = (
        '<tool_call>'
        '{"name": "x", "arguments":'
        '</tool_call>'
    )
    calls = _extract_tool_calls_from_content(content)
    assert calls == []
```

Run: expected: FAIL — currently raises `json.JSONDecodeError` or returns `[]`; either way the test asserts the safe behavior.

- [ ] **Step 6: Add `test_format_4_non_dict_json_skipped`**

```python
def test_format_4_non_dict_json_skipped():
    """Non-object JSON (null, array, string) is silently skipped."""
    for raw in ("null", "[1, 2, 3]", '"just a string"', "42"):
        content = f'<tool_call>{raw}</tool_call>'
        calls = _extract_tool_calls_from_content(content)
        assert calls == [], f"raw={raw!r} returned {calls!r}"
```

Run: expected: FAIL (will either crash on `null` (`.get` on NoneType) or return `[]`).

- [ ] **Step 7: Add `test_format_4_missing_name_skipped`**

```python
def test_format_4_missing_name_skipped():
    """Tag with no `name` field is silently skipped."""
    content = '<tool_call>{"arguments": {"x": 1}}</tool_call>'
    calls = _extract_tool_calls_from_content(content)
    assert calls == []
```

Run: expected: FAIL — currently raises `AttributeError: 'NoneType' object has no attribute 'startswith'` or similar (in existing Format 1 code), because no branch handles this.

- [ ] **Step 8: Add `test_format_4_args_as_string_normalized`**

```python
def test_format_4_args_as_string_normalized():
    """`arguments` emitted as a JSON string is coerced to {}."""
    content = '<tool_call>{"name": "f", "arguments": "{}"}</tool_call>'
    calls = _extract_tool_calls_from_content(content)
    assert len(calls) == 1
    import json as _json
    assert _json.loads(calls[0]["function"]["arguments"]) == {}
```

Run: expected: FAIL — currently `arguments` would be passed through unchanged, which downstream OpenAI strict-type check would reject.

- [ ] **Step 9: Add `test_format_4_unicode_in_args`**

```python
def test_format_4_unicode_in_args():
    """CJK characters in name and arguments round-trip cleanly."""
    content = (
        '<tool_call>'
        '{"name": "查询", "arguments": {"sql": "SELECT \'你好\'"}}'
        '</tool_call>'
    )
    calls = _extract_tool_calls_from_content(content)
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "查询"
    import json as _json
    assert _json.loads(calls[0]["function"]["arguments"]) == {"sql": "SELECT '你好'"}
```

Run: expected: FAIL (parser does not handle Format 4).

- [ ] **Step 10: Run all 8 detection tests; confirm the breakdown matches expectations**

Run: `cd backend && venv/bin/python -m pytest tests/test_extract_tool_calls_qwen3_hermes.py::TestFormat4Detect -p no:jaxtyping -p no:cacheprovider -v`
Expected breakdown:
- **6 tests FAIL** (happy path, whitespace, multiple, malformed JSON, args-as-string, unicode) — parser doesn't extract Format 4 today, so it returns `[]` instead of the expected call. These drive the implementation.
- **2 tests PASS vacuously today** (non-dict JSON, missing name) — parser doesn't try Format 4 at all, so `[]` matches `[]`. These document safety properties the implementation MUST preserve (i.e., after the impl lands, these tests must still pass — proving the parser safely skips non-callable content).

After implementation lands, **all 8 tests PASS**.

- [ ] **Step 11: Commit the failing tests**

```bash
git add backend/tests/test_extract_tool_calls_qwen3_hermes.py
git commit -m "test(agents): add failing tests for Format 4 tool-call detection"
```

---

## Task 2: Implement Format 4 detection in `_extract_tool_calls_from_content`

**Files:**
- Modify: `backend/app/routers/agents.py:6854-6972` — append Format 4 block before the final `return tool_calls`

**Interfaces:**
- Consumes: existing function signature `(content: str) -> list[dict]` (unchanged)
- Produces: same shape — list of OpenAI tool-call dicts (Format 4 entries)

- [ ] **Step 1: Locate the exact insertion point**

Run: `grep -n 'return tool_calls\|^def _extract_tool_calls_from_content\|^def _strip_tool_call_markup' backend/app/routers/agents.py`

You should see lines like `6854: def _extract_tool_calls_from_content(...)` and `6972: return tool_calls` (the final fallback). The new block goes between line **6969** (the last existing `if tool_calls: return tool_calls`) and **6972**.

If the file structure has shifted, search for the line that says `return tool_calls` **at the end of the function body** — the new block goes immediately before it.

- [ ] **Step 2: Read the existing final block to anchor the edit**

Run:
```bash
sed -n '6965,6975p' backend/app/routers/agents.py
```

Expected output (approximate):
```python
        else:
            continue
        if raw_hermes:
            tool_calls.append({...})
            continue
        # Format 3: ...
        ...

        if tool_calls:
            return tool_calls
```

The exact text will vary; the goal is to find the **last existing parser branch** and insert the Format 4 block immediately **after** it but **before** the closing `return tool_calls`.

- [ ] **Step 3: Insert the Format 4 detection block**

In `backend/app/routers/agents.py`, inside `_extract_tool_calls_from_content`, immediately before the final `return tool_calls`, insert:

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

The block uses local-scope names (`_m`, `_obj`, `_fn`, `_args`, `raw`, `_hermes_tag_re`) to avoid colliding with anything else in the function. The regex is compiled once per call (cheap) — premature optimization to compile at module scope.

- [ ] **Step 4: Run all 8 detection tests; confirm they all PASS**

Run: `cd backend && venv/bin/python -m pytest tests/test_extract_tool_calls_qwen3_hermes.py::TestFormat4Detect -p no:jaxtyping -p no:cacheprovider -v`
Expected: **8 passed**.

- [ ] **Step 5: Verify no regression in existing call sites**

Run a smoke check that the parser still produces the same output for legacy formats (DeepSeek, OpenAI):

```bash
cd backend && venv/bin/python -m pytest tests/test_call_llm_failover.py tests/test_agents_token_streaming.py -p no:jaxtyping -p no:cacheprovider -v --no-header -q 2>&1 | tail -25
```

Expected: same pass/fail pattern as before this change (no new failures). If a new failure appears, the most likely cause is a regex collision — re-read the inserted block and verify the `_hermes_tag_re` does not over-match.

- [ ] **Step 6: Commit the implementation**

```bash
git add backend/app/routers/agents.py
git commit -m "feat(agents): parse Qwen3 Hermes-Qwen tool_call tag (Format 4)"
```

---

## Task 3: Write failing tests for Format 4 strip

**Files:**
- Modify: `backend/tests/test_extract_tool_calls_qwen3_hermes.py` (expand `TestFormat4Strip` class)

**Interfaces:**
- Consumes: `from app.routers.agents import _strip_tool_call_markup`
- Produces: 4 test functions in `TestFormat4Strip`

- [ ] **Step 1: Add `test_strip_removes_hermes_tag`**

```python
def test_strip_removes_hermes_tag():
    """Tag block is fully removed; surrounding text remains concatenated."""
    content = 'preamble<tool_call>{"name":"f","arguments":{}}</tool_call>postamble'
    result = _strip_tool_call_markup(content)
    # re.sub removes the entire tag block; surrounding text concatenates.
    assert "tool_call" not in result
    assert "preamble" in result
    assert "postamble" in result
```

Run: `cd backend && venv/bin/python -m pytest tests/test_extract_tool_calls_qwen3_hermes.py::TestFormat4Strip::test_strip_removes_hermes_tag -p no:jaxtyping -p no:cacheprovider -v`
Expected: FAIL — current strip pipeline has no branch for `<tool_call>`.

- [ ] **Step 2: Add `test_strip_handles_malformed_hermes_tag`**

```python
def test_strip_handles_malformed_hermes_tag():
    """Unclosed tag is not stripped (best-effort strip; no crash)."""
    content = "text <tool_call> broken but no closing"
    result = _strip_tool_call_markup(content)
    # Best-effort: the regex (non-greedy) does not match; original text is kept.
    assert "text" in result
    assert "broken" in result
```

Run: expected: FAIL — current strip has no Hermes tag handling; result is unchanged for now (the test only asserts no crash). Once implementation lands, this test still passes.

- [ ] **Step 3: Add `test_extract_priority_order`**

```python
def test_extract_priority_order():
    """Content mixing Format 1 (legacy XML) AND Format 4 (Hermes tag) — Format 1 wins (extracted first)."""
    content = (
        '<function=legacy_one><parameter=arg>1</parameter></function>'
        ' some prose '
        '<tool_call>{"name": "hermes_one", "arguments": {}}</tool_call>'
    )
    calls = _extract_tool_calls_from_content(content)
    # Format 1 has its own `if tool_calls: return tool_calls` early-exit.
    # Once Format 1 finds one call, Format 4 never runs.
    names = [c["function"]["name"] for c in calls]
    assert names == ["legacy_one"], f"expected Format 1 first, got {names}"
```

Run: expected: PASS — the existing Format 1 code already finds the legacy call and returns early. This test documents the priority order; the new Format 4 block never runs in this case.

- [ ] **Step 4: Add `test_strip_priority_order`**

```python
def test_strip_priority_order():
    """Strip pipeline removes BOTH Format 1 and Format 4, plain text remains."""
    content = (
        'preamble '
        '<function=foo><parameter=x>1</parameter></function>'
        ' middle '
        '<tool_call>{"name":"bar","arguments":{}}</tool_call>'
        ' end'
    )
    result = _strip_tool_call_markup(content)
    # Both formats stripped; prose survives.
    assert "function=" not in result
    assert "tool_call" not in result
    assert "preamble" in result
    assert "middle" in result
    assert "end" in result
```

Run: expected: FAIL — Format 4 is not stripped yet.

- [ ] **Step 5: Run all 4 strip tests; confirm test 1 + 4 FAIL, test 2 + 3 are unaffected**

Run: `cd backend && venv/bin/python -m pytest tests/test_extract_tool_calls_qwen3_hermes.py::TestFormat4Strip -p no:jaxtyping -p no:cacheprovider -v`
Expected:
- `test_strip_removes_hermes_tag` — FAIL (test 1)
- `test_strip_handles_malformed_hermes_tag` — PASS (test passes vacuously today; documents contract for the implementation phase)
- `test_extract_priority_order` — PASS (test is in `TestFormat4Strip` class but exercises extractor; passes today because Format 1 short-circuits)
- `test_strip_priority_order` — FAIL (test 4)

- [ ] **Step 6: Commit the failing strip tests**

```bash
git add backend/tests/test_extract_tool_calls_qwen3_hermes.py
git commit -m "test(agents): add failing tests for Format 4 strip"
```

---

## Task 4: Implement Format 4 strip in `_strip_tool_call_markup`

**Files:**
- Modify: `backend/app/routers/agents.py:6975-7020` — add a new `re.sub` **first** in the strip pipeline (before the three existing substitutions)

**Interfaces:**
- Consumes: existing function signature `(content: str) -> str` (unchanged)
- Produces: same shape — string with all four markup formats removed

- [ ] **Step 1: Locate the strip pipeline**

Run:
```bash
grep -n '^def _strip_tool_call_markup\|re\.sub(r"<function' backend/app/routers/agents.py
```

You should see `def _strip_tool_call_markup(...)` around line 6975 and the first `re.sub(r"<function=...` shortly after. The new `re.sub` for Format 4 goes immediately **before** the existing `<function=` regex line.

- [ ] **Step 2: Insert the Format 4 strip regex**

In `backend/app/routers/agents.py`, inside `_strip_tool_call_markup`, find the line `clean = re.sub(r"<function=\w+>.*?</function>", ...)` and insert the Format 4 strip **immediately above** it:

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
```

The variable `clean` is then reassigned by the three subsequent `re.sub` calls — they continue to operate on the already-stripped string.

- [ ] **Step 3: Run all 4 strip tests; confirm they all PASS**

Run: `cd backend && venv/bin/python -m pytest tests/test_extract_tool_calls_qwen3_hermes.py::TestFormat4Strip -p no:jaxtyping -p no:cacheprovider -v`
Expected: **4 passed**.

- [ ] **Step 4: Run the FULL new test file (12 tests)**

Run: `cd backend && venv/bin/python -m pytest tests/test_extract_tool_calls_qwen3_hermes.py -p no:jaxtyping -p no:cacheprovider -v`
Expected: **12 passed, 0 failed**.

- [ ] **Step 5: Commit the strip implementation**

```bash
git add backend/app/routers/agents.py
git commit -m "feat(agents): strip Qwen3 Hermes-Qwen tool_call tag (Format 4)"
```

---

## Task 5: Full backend regression check + restart + manual smoke test

**Files:**
- No code changes.

**Interfaces:**
- Consumes: a deployed `zhanlu-backend` container.

- [ ] **Step 1: Run a broader regression suite to confirm no other tests broke**

Run:
```bash
cd backend && venv/bin/python -m pytest \
  tests/test_extract_tool_calls_qwen3_hermes.py \
  tests/test_call_llm_failover.py \
  tests/test_agents_token_streaming.py \
  tests/test_agents_force_synthesis.py \
  tests/test_answer_verification.py \
  -p no:jaxtyping -p no:cacheprovider -q 2>&1 | tail -15
```

Expected: no new failures compared to before this change. (Pre-existing failures unrelated to the parser should be the same as before.)

- [ ] **Step 2: Restart the backend**

Run:
```bash
docker restart zhanlu-backend
sleep 5
docker logs zhanlu-backend --tail 20
```

Expected: backend starts cleanly, no import error, no traceback.

- [ ] **Step 3: Smoke-test from the chat UI**

1. Open the Zhanlu UI in a browser.
3. Switch to the **Global** project, **Data Analyst** agent.
4. Confirm the effective LLM badge reads `qwen3.6-27b`.
5. Send a question that triggers a tool call (e.g., "What tables are in the data source?" → triggers `inspect_data_source` or `describe_schema`).
6. Confirm the assistant bubble populates within ~10-60 s with a coherent answer (not "Sorry, I hit an error").
7. Tail the backend log while sending:
   ```bash
   docker logs zhanlu-backend --tail 100 -f | grep -iE "tool_call|LLM request|Format 4|qwen"
   ```
   Expect: zero 400s, zero "Format 4: skipping malformed JSON" warnings on a clean request.

- [ ] **Step 4: Verify other models are unaffected (regression check for multi-model promise)**

1. In the LLM Models dialog, switch the agent's effective LLM back to `gpt-4o-mini` (or `deepseek-chat`) by binding a different row.
2. Send the same question.
3. Confirm the response works identically to before this change.

- [ ] **Step 5: Update memory + commit (if any memos)**

If any diagnostic findings (e.g. unexpected 400s from a specific model) were uncovered during the smoke test, append a brief note to `.codebuddy/memory/2026-08-24.md` and commit:

```bash
git add .codebuddy/memory/2026-08-24.md
git commit -m "docs(memo): qwen3 hermes parser rollout verified"
```

---

## Self-Review

**1. Spec coverage:**
- ✅ Section 4.1 detection algorithm → Task 2 Step 3 (Format 4 regex + try/except + dict guard + args coercion).
- ✅ Section 4.2 strip algorithm → Task 4 Step 2 (new `re.sub` first in pipeline).
- ✅ Section 5 test coverage → Tasks 1 + 3 (12 tests across `TestFormat4Detect` and `TestFormat4Strip`).
- ✅ Section 6 error handling guarantees → Tests 4 (malformed JSON), 5 (non-dict JSON), 6 (missing name), 7 (args as string), 10 (malformed tag in strip).
- ✅ Section 7 multi-model regression → Task 5 Step 1 (existing test files) + Step 4 (manual switch-and-verify).
- ✅ Section 9 rollout → Task 5 Steps 2-3 (`docker restart` + smoke test).

**2. Placeholder scan:** No "TBD", "TODO", "implement later", or similar markers. Every step has either code, a command, or a concrete description. No "similar to Task N" cross-references without repeating the actual content.

**3. Type consistency:**
- `_extract_tool_calls_from_content(content: str) -> list[dict]` — used unchanged throughout.
- `_strip_tool_call_markup(content: str) -> str` — used unchanged throughout.
- `tool_calls[i]["function"]["name"]` and `tool_calls[i]["function"]["arguments"]` — consistent across all 8 detection tests.
- `tool_calls[i]["id"]` is set to `f"call_{_uuid.uuid4().hex[:24]}"` — matches existing Format 1/2/3 convention (verified by inspection of agents.py lines 6910, 6940, 6970).
- Local-scope names (`_m`, `_obj`, `_fn`, `_args`, `raw`, `_hermes_tag_re`) are unique to the new block — no collision risk.
- Imports: `re`, `json`, `_uuid` are already imported at module top of `agents.py` (verified by earlier reads).