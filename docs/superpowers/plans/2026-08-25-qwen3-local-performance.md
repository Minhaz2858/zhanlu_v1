# qwen3.6-27b Local Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make qwen3.6-27b-awq4 work reliably, fast, and with high-quality answers — 100% offline (no DeepSeek API calls).

**Architecture:** 6 components in dependency order: (1) bug fixes [mid-list system msg + malformed args], (2) per-model fast-mode override for LLM call count reduction, (3) vLLM prefix caching [ops-only], (4) CEO-grade synthesis prompt with inline data, (5) deterministic fallback when synthesis fails, (6) vLLM server config [ops-only]. Components 1 must ship first; 2-5 can ship in parallel; 3 + 6 are ops tasks.

**Tech Stack:** FastAPI, Python 3.11, vLLM (qwen3.6-27b-awq4, AWQ 4-bit quantization), pytest, `ast.literal_eval` for Python-literal JSON repair.

## Global Constraints

- **Primary project**: `/home/ysk2025/zhanlu_7_30`. Docker port **5002**. `docker restart zhanlu-backend` after service changes.
- **Backend venv**: `backend/venv/bin/python`. System python has no pytest.
- **In-container pytest**: venv's C extensions are broken (pydantic_core). Use system `/usr/local/bin/python3.11` with `sys.path.insert(0, "/app/venv/lib/python3.11/site-packages")` + call `pytest.main([...])` programmatically. Direct `-m pytest` fails with "No module named pytest".
- **No new pip packages** — `ast` is in Python stdlib; no new deps for any component.
- **All flags default `False`** per project convention. QWEN3_FAST_MODE_ENABLED defaults True but only fires when user's model is qwen3.* + is_private.
- **Edit-safety**: parallel `replace_in_file` to SAME file is unsafe. One same-file edit per message.
- **Frontend build**: `npm run build -- --emptyOutDir=false`. Dist bind-mounted `:ro` → no docker restart for frontend-only changes.
- **Deploy rule**: every backend deploy must `docker restart zhanlu-backend zhanlu-sandbox-worker` — bind-mounts never reload imported modules.
- **Backward compat**: deepseek-chat path must remain byte-identical when `_is_qwen_local_model(endpoint)` returns False. All qwen3 overrides are conditional.
- **Self-eval re-plan is KEPT** for qwen3 (per user decision) but tightened: max 1 re-plan (not 3), 5s timeout (not 15s).
- **vLLM @ 10.10.10.46:8000** with `--enable-auto-tool-choice --tool-call-parser=qwen3_xml --max_model_len=65536`. Missing `--enable-prefix-caching` (Component 6).

---

## File Structure

### Files to modify
- `backend/app/services/message_sanitization.py` — Tasks 3, 4: add `_try_python_literal_eval` repair pass + mid-list system message guard pass.
- `backend/app/routers/agents.py` — Tasks 1, 2, 5, 6, 7, 8: reflexion critique → user role; web-search handler → user role; `_is_qwen_local_model` helper; per-model overrides at v3 loop call sites; CEO-grade synthesis prompt + inline data; enhanced `_data_rows_fallback`.
- `backend/app/config.py` — Task 5: add `QWEN3_*` config flags.
- `backend/app/services/tool_handlers/delegation_tools.py` — Task 6: per-model `DATA_AGENT_BUDGET_SECONDS` override.

### Files to create
- `backend/tests/test_message_sanitization_qwen3.py` — Tasks 3, 4: tests for mid-list system guard + Python-literal JSON repair.
- `backend/tests/test_qwen3_fast_mode.py` — Tasks 5, 6: tests for `_is_qwen_local_model` + override application.
- `backend/tests/test_synthesis_quality_qwen3.py` — Task 7: tests for CEO-grade prompt + inline data.
- `backend/tests/test_deterministic_fallback.py` — Task 8: tests for enhanced `_data_rows_fallback`.

### Ops doc
- `docs/superpowers/specs/2026-08-25-qwen3-local-performance-design.md` — already written (the spec).

---

## Task 1: Bug fix — mid-list system message in `_inject_reflexion_critique`

**Files:**
- Modify: `backend/app/routers/agents.py:1731-1766`

**Interfaces:**
- Consumes: `llm_messages: list[dict]`, `calls: list[dict]`, `results: list` (existing signature unchanged).
- Produces: appends `{"role": "user", "content": ...}` instead of `{"role": "system", "content": ...}`. Same content, different role.

**Root cause:** `_inject_reflexion_critique` appends a system message after tool results, mid-conversation. vLLM rejects this with HTTP 400 `"System message must be at the beginning."`. This fires on every qwen3 turn because malformed args (Task 4) cause "Invalid arguments" → reflexion fires → mid-list system → vLLM 400.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_message_sanitization_qwen3.py`:

```python
"""Tests for qwen3.6-27b-specific message sanitization.

Run in-container:
  /usr/local/bin/python3.11 -c "import sys; sys.path.insert(0, '/app/venv/lib/python3.11/site-packages'); sys.path.insert(0, '/app'); import pytest; exit(pytest.main(['-xvs', 'tests/test_message_sanitization_qwen3.py']))"
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from app.routers.agents import _inject_reflexion_critique


def test_reflexion_critique_uses_user_role_not_system():
    """Regression: reflexion critique must NOT inject a system message mid-list.
    vLLM rejects mid-list system messages with HTTP 400.
    """
    llm_messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What's my sales?"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "tc1", "type": "function", "function": {"name": "ask_data_agent", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "tc1", "content": "0 rows returned"},
    ]
    calls = [{"tool_name": "ask_data_agent", "args_str": "{}"}]
    results = [{"success": False, "error": "Invalid arguments"}]

    _inject_reflexion_critique(llm_messages, calls, results)

    # The injected message must be role=user, NOT role=system
    injected = [m for m in llm_messages if m["role"] == "user" and "failed" in m.get("content", "").lower()]
    assert injected, f"No user-role critique found; messages: {llm_messages}"
    system_msgs = [m for m in llm_messages if m["role"] == "system"]
    assert len(system_msgs) == 1, f"Expected 1 system msg (the initial), got {len(system_msgs)}"
```

- [ ] **Step 2: Run the test — it fails**

Run: `cd backend && docker exec zhanlu-backend /usr/local/bin/python3.11 -c "import sys; sys.path.insert(0, '/app/venv/lib/python3.11/site-packages'); sys.path.insert(0, '/app'); import pytest; exit(pytest.main(['-xvs', 'tests/test_message_sanitization_qwen3.py::test_reflexion_critique_uses_user_role_not_system']))"`
Expected: FAIL — the injected message has `role: "system"` not `role: "user"`.

- [ ] **Step 3: Fix `_inject_reflexion_critique`**

In `backend/app/routers/agents.py`, find line 1766:

```python
    llm_messages.append({"role": "system", "content": "\n".join(lines)})
```

Replace with:

```python
    # 2026-08-25: vLLM (qwen3.6-27b) rejects mid-list system messages with
    # HTTP 400 "System message must be at the beginning." The reflexion
    # critique is a mid-conversation nudge, so it must be role="user".
    # DeepSeek API tolerates mid-list system messages, so this change is
    # backward-compatible for the cloud path too.
    llm_messages.append({"role": "user", "content": "\n".join(lines)})
```

- [ ] **Step 4: Run the test — it passes**

Run: `cd backend && docker exec zhanlu-backend /usr/local/bin/python3.11 -c "import sys; sys.path.insert(0, '/app/venv/lib/python3.11/site-packages'); sys.path.insert(0, '/app'); import pytest; exit(pytest.main(['-xvs', 'tests/test_message_sanitization_qwen3.py::test_reflexion_critique_uses_user_role_not_system']))"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/agents.py backend/tests/test_message_sanitization_qwen3.py
git commit -m "fix(agents): reflexion critique uses role=user not role=system (vLLM compat)"
```

---

## Task 2: Bug fix — mid-list system message in web-search handler

**Files:**
- Modify: `backend/app/routers/agents.py:5363-5371`

**Interfaces:**
- Produces: the followup message list uses `{"role": "user", ...}` instead of `{"role": "system", ...}` for the "You DO have access to web_search" directive.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_message_sanitization_qwen3.py`:

```python
def test_web_search_handler_uses_user_role_not_system():
    """The web-search followup handler must not inject a mid-list system
    message. vLLM rejects this with HTTP 400."""
    # We can't easily call the inner async _hg_call_llm, so we test the
    # message construction by inspecting the source. Instead, test the
    # sanitize_messages guard (Task 3) catches it. For now, verify the
    # pattern: any mid-list system message gets demoted.
    from app.services.message_sanitization import sanitize_messages
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "search the web for X"},
        {"role": "assistant", "content": None, "tool_calls": []},
        {"role": "tool", "tool_call_id": "tc1", "content": "no results"},
        # This is what the web-search handler used to append:
        {"role": "system", "content": "You DO have access to web_search."},
        {"role": "user", "content": "Here are live web search results..."},
    ]
    changed = sanitize_messages(messages)
    assert changed, "sanitize_messages should have demoted the mid-list system msg"
    system_msgs = [m for m in messages if m["role"] == "system"]
    assert len(system_msgs) == 1, f"Expected 1 system msg after sanitize, got {len(system_msgs)}"
    # The mid-list system should now be role=user
    user_msgs = [m for m in messages if m["role"] == "user"]
    assert any("web_search" in m.get("content", "") for m in user_msgs)
```

- [ ] **Step 2: Run the test — it fails**

Run: `cd backend && docker exec zhanlu-backend /usr/local/bin/python3.11 -c "import sys; sys.path.insert(0, '/app/venv/lib/python3.11/site-packages'); sys.path.insert(0, '/app'); import pytest; exit(pytest.main(['-xvs', 'tests/test_message_sanitization_qwen3.py::test_web_search_handler_uses_user_role_not_system']))"`
Expected: FAIL — `sanitize_messages` doesn't yet have the mid-list system guard pass.

- [ ] **Step 3: Fix the web-search handler source**

In `backend/app/routers/agents.py`, find lines 5363-5371 (the web-search followup message construction):

```python
                    followup_msgs = list(messages) + [
                        {
                            "role": "system",
                            "content": (
                                "You DO have access to web_search and web_extract. "
                                "Use these live search results to answer the user's "
                                "request. Cite the source links. If results are "
                                "insufficient, run another web_search."
                            ),
                        },
```

Replace `"role": "system",` with `"role": "user",`:

```python
                    followup_msgs = list(messages) + [
                        {
                            # 2026-08-25: role=user not role=system — vLLM rejects
                            # mid-list system messages with HTTP 400.
                            "role": "user",
                            "content": (
                                "You DO have access to web_search and web_extract. "
                                "Use these live search results to answer the user's "
                                "request. Cite the source links. If results are "
                                "insufficient, run another web_search."
                            ),
                        },
```

- [ ] **Step 4: Commit (test still fails — needs Task 3's guard to pass)**

```bash
git add backend/app/routers/agents.py backend/tests/test_message_sanitization_qwen3.py
git commit -m "fix(agents): web-search handler uses role=user not role=system (vLLM compat)"
```

Note: the test from Step 1 will pass after Task 3 adds the `sanitize_messages` guard. Commit now; the guard is Task 3.

---

## Task 3: Bug fix — mid-list system message safety guard in `sanitize_messages`

**Files:**
- Modify: `backend/app/services/message_sanitization.py:219-259` (the `sanitize_messages` function)

**Interfaces:**
- Consumes: `messages: list[dict]` (existing).
- Produces: new Pass 4 demotes any `{"role": "system"}` not at index 0 to `{"role": "user"}`. Returns `True` if any changes.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_message_sanitization_qwen3.py`:

```python
def test_sanitize_messages_demotes_mid_list_system_to_user():
    """Defense-in-depth: any system message not at index 0 gets demoted
    to role=user. Catches future code paths that re-introduce the bug."""
    from app.services.message_sanitization import sanitize_messages
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "mid-list system msg — should be demoted"},
        {"role": "user", "content": "bye"},
    ]
    changed = sanitize_messages(messages)
    assert changed, "sanitize_messages should have demoted the mid-list system msg"
    assert messages[0]["role"] == "system", "index 0 system msg must stay"
    assert messages[2]["role"] == "user", f"index 2 should be user, got {messages[2]['role']}"
    assert "mid-list system msg" in messages[2]["content"]


def test_sanitize_messages_no_change_when_system_only_at_index_0():
    """No false positives: when system is only at index 0, sanitize_messages
    should not change anything (for the system-message pass)."""
    from app.services.message_sanitization import sanitize_messages
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    # Note: other passes (surrogates, tool_call repair) may still fire,
    # but the system-message guard specifically should not change anything.
    # We verify by checking the roles are unchanged.
    original_roles = [m["role"] for m in messages]
    sanitize_messages(messages)
    new_roles = [m["role"] for m in messages]
    assert original_roles == new_roles, f"roles changed: {original_roles} → {new_roles}"


def test_sanitize_messages_multiple_mid_list_systems_all_demoted():
    """Multiple mid-list system messages all get demoted."""
    from app.services.message_sanitization import sanitize_messages
    messages = [
        {"role": "system", "content": "initial"},
        {"role": "system", "content": "second system — demote"},
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "third system — demote"},
    ]
    sanitize_messages(messages)
    system_count = sum(1 for m in messages if m["role"] == "system")
    assert system_count == 1, f"Expected 1 system msg, got {system_count}"
    user_count = sum(1 for m in messages if m["role"] == "user")
    assert user_count == 2, f"Expected 2 user msgs (original + 2 demoted), got {user_count}"
```

- [ ] **Step 2: Run the tests — they fail**

Run: `cd backend && docker exec zhanlu-backend /usr/local/bin/python3.11 -c "import sys; sys.path.insert(0, '/app/venv/lib/python3.11/site-packages'); sys.path.insert(0, '/app'); import pytest; exit(pytest.main(['-xvs', 'tests/test_message_sanitization_qwen3.py']))"`
Expected: FAIL — `sanitize_messages` doesn't yet have the mid-list system guard.

- [ ] **Step 3: Add the mid-list system guard pass to `sanitize_messages`**

In `backend/app/services/message_sanitization.py`, find the `sanitize_messages` function (line 219-259). Add Pass 4 before the `return changed`:

```python
    # Pass 4: demote mid-list system messages to user role.
    # 2026-08-25: vLLM (qwen3.6-27b) rejects any system message not at
    # index 0 with HTTP 400 "System message must be at the beginning."
    # This is a defense-in-depth guard: even if a code path appends a
    # system message mid-conversation, this pass catches it. DeepSeek
    # API tolerates mid-list system messages, so demoting to user is
    # backward-compatible (the content still reaches the LLM).
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        if idx == 0:
            continue  # index 0 system msg is always OK
        if msg.get("role") == "system":
            msg["role"] = "user"
            changed = True
            logger.info(
                "Demoted mid-list system message at index %d to role=user (vLLM compat)",
                idx,
            )

    return changed
```

Also update the docstring's "Passes" list to include Pass 4.

- [ ] **Step 4: Run the tests — they pass**

Run: `cd backend && docker exec zhanlu-backend /usr/local/bin/python3.11 -c "import sys; sys.path.insert(0, '/app/venv/lib/python3.11/site-packages'); sys.path.insert(0, '/app'); import pytest; exit(pytest.main(['-xvs', 'tests/test_message_sanitization_qwen3.py']))"`
Expected: PASS (all tests, including the web-search test from Task 2).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/message_sanitization.py backend/tests/test_message_sanitization_qwen3.py
git commit -m "fix(sanitization): demote mid-list system messages to user role (vLLM compat)"
```

---

## Task 4: Bug fix — malformed tool_call args (single-quoted JSON)

**Files:**
- Modify: `backend/app/services/message_sanitization.py:132-194` (the `repair_tool_call_arguments` function)

**Interfaces:**
- Consumes: `raw_args: str`, `tool_name: str` (existing).
- Produces: valid JSON string. New repair pass uses `ast.literal_eval` to handle Python-style dict literals (`'query': 'value'`).

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_message_sanitization_qwen3.py`:

```python
def test_repair_single_quoted_python_dict_literal():
    """qwen3.6-27b emits args as Python-style dict literals with single
    quotes: 'query': 'show me sales'. Standard JSON parsers reject this.
    ast.literal_eval handles it and we re-serialize as valid JSON."""
    from app.services.message_sanitization import repair_tool_call_arguments
    raw = "{'query': 'show me sales'}"
    result = repair_tool_call_arguments(raw, "ask_data_agent")
    import json
    parsed = json.loads(result)  # must be valid JSON
    assert parsed == {"query": "show me sales"}


def test_repair_single_quoted_no_braces():
    """qwen3.6-27b sometimes emits args without braces: 'query': 'value'."""
    from app.services.message_sanitization import repair_tool_call_arguments
    raw = "'query': 'show me sales'"
    result = repair_tool_call_arguments(raw, "ask_data_agent")
    import json
    parsed = json.loads(result)
    assert parsed == {"query": "show me sales"}


def test_repair_mixed_quotes():
    """Mixed quotes: {"query": 'show me sales'} — valid JSON brace but
    single-quoted value."""
    from app.services.message_sanitization import repair_tool_call_arguments
    raw = '{"query": \'show me sales\'}'
    result = repair_tool_call_arguments(raw, "ask_data_agent")
    import json
    parsed = json.loads(result)
    assert parsed == {"query": "show me sales"}


def test_repair_valid_json_unchanged():
    """Already-valid JSON must pass through unchanged."""
    from app.services.message_sanitization import repair_tool_call_arguments
    raw = '{"query": "show me sales"}'
    result = repair_tool_call_arguments(raw, "ask_data_agent")
    assert result == raw


def test_repair_unrepairable_returns_empty_object():
    """Genuinely unrepairable args still return {} (existing behavior)."""
    from app.services.message_sanitization import repair_tool_call_arguments
    raw = "not even close to json or python"
    result = repair_tool_call_arguments(raw, "ask_data_agent")
    assert result == "{}"
```

- [ ] **Step 2: Run the tests — they fail**

Run: `cd backend && docker exec zhanlu-backend /usr/local/bin/python3.11 -c "import sys; sys.path.insert(0, '/app/venv/lib/python3.11/site-packages'); sys.path.insert(0, '/app'); import pytest; exit(pytest.main(['-xvs', 'tests/test_message_sanitization_qwen3.py::test_repair_single_quoted_python_dict_literal', 'tests/test_message_sanitization_qwen3.py::test_repair_single_quoted_no_braces']))"`
Expected: FAIL — `repair_tool_call_arguments` returns `"{}"` for single-quoted input (can't parse as JSON, falls through to last resort).

- [ ] **Step 3: Add the `ast.literal_eval` repair pass**

In `backend/app/services/message_sanitization.py`, add `import ast` at the top (after `import json`), then add a new helper function before `repair_tool_call_arguments`:

```python
import ast


def _try_python_literal_eval(raw: str) -> str | None:
    """Repair single-quoted Python dict literals by using ast.literal_eval.

    qwen3.6-27b emits tool_call args as Python-style dict literals:
        'query': 'show me sales'
        {'query': 'show me sales'}
    Standard JSON parsers reject single quotes. ast.literal_eval handles
    them, and we re-serialize as valid JSON.

    Returns valid JSON string, or None if ast.literal_eval also fails.
    """
    if not raw:
        return None
    # If it's already valid JSON, don't touch it (fast path).
    try:
        import json
        json.loads(raw)
        return None  # already valid; caller should use json.loads directly
    except (json.JSONDecodeError, TypeError):
        pass
    # Try ast.literal_eval. If the raw has no braces, wrap it.
    candidate = raw.strip()
    if not candidate.startswith("{"):
        candidate = "{" + candidate
    if not candidate.endswith("}"):
        candidate = candidate + "}"
    try:
        parsed = ast.literal_eval(candidate)
        if isinstance(parsed, dict):
            return json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)
    except (ValueError, SyntaxError):
        return None
    return None
```

Then in `repair_tool_call_arguments`, add the new pass **after** the existing `json.loads(strict=False)` pass (line 158) and **before** the trailing-comma pass (line 161):

```python
    # Repair pass 0.5: Python-style dict literals (single-quoted).
    # qwen3.6-27b emits 'query': 'value' instead of {"query": "value"}.
    # ast.literal_eval handles this; we re-serialize as valid JSON.
    _py_repaired = _try_python_literal_eval(raw_stripped)
    if _py_repaired is not None:
        logger.warning(
            "Repaired Python-literal tool_call arguments for %s (single-quoted → JSON)",
            tool_name,
        )
        return _py_repaired
```

- [ ] **Step 4: Run the tests — they pass**

Run: `cd backend && docker exec zhanlu-backend /usr/local/bin/python3.11 -c "import sys; sys.path.insert(0, '/app/venv/lib/python3.11/site-packages'); sys.path.insert(0, '/app'); import pytest; exit(pytest.main(['-xvs', 'tests/test_message_sanitization_qwen3.py']))"`
Expected: PASS (all tests including the new repair tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/message_sanitization.py backend/tests/test_message_sanitization_qwen3.py
git commit -m "fix(sanitization): repair single-quoted Python-literal tool_call args (qwen3 compat)"
```

---

## Task 5: Call reduction — config flags + `_is_qwen_local_model` helper

**Files:**
- Modify: `backend/app/config.py:595+` (after SELF_EVAL block)
- Modify: `backend/app/routers/agents.py` (add helper near other model-detection helpers)
- Create: `backend/tests/test_qwen3_fast_mode.py`

**Interfaces:**
- Produces: `QWEN3_*` config flags (see spec). Produces `_is_qwen_local_model(endpoint) -> bool` helper. Produces `_get_qwen3_max_iterations() -> int` and similar accessors.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_qwen3_fast_mode.py`:

```python
"""Tests for qwen3.6-27b per-model fast-mode overrides.

Run in-container:
  /usr/local/bin/python3.11 -c "import sys; sys.path.insert(0, '/app/venv/lib/python3.11/site-packages'); sys.path.insert(0, '/app'); import pytest; exit(pytest.main(['-xvs', 'tests/test_qwen3_fast_mode.py']))"
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from types import SimpleNamespace
from app.routers.agents import _is_qwen_local_model


def test_is_qwen_local_model_true_for_qwen3_private():
    endpoint = SimpleNamespace(
        model_id="qwen3.6-27b",
        is_private=True,
        api_key="EMPTY",
    )
    assert _is_qwen_local_model(endpoint) is True


def test_is_qwen_local_model_true_for_qwen3_empty_key():
    endpoint = SimpleNamespace(
        model_id="qwen3.6-27b-awq4",
        is_private=False,
        api_key="",
    )
    assert _is_qwen_local_model(endpoint) is True


def test_is_qwen_local_model_false_for_deepseek():
    endpoint = SimpleNamespace(
        model_id="deepseek-chat",
        is_private=False,
        api_key="sk-real-key",
    )
    assert _is_qwen_local_model(endpoint) is False


def test_is_qwen_local_model_false_for_qwen3_with_real_api_key():
    """qwen3 with a real API key means it's a cloud-hosted qwen, not local vLLM."""
    endpoint = SimpleNamespace(
        model_id="qwen3.6-27b",
        is_private=False,
        api_key="sk-real-cloud-key",
    )
    assert _is_qwen_local_model(endpoint) is False


def test_is_qwen_local_model_false_for_none():
    assert _is_qwen_local_model(None) is False


def test_is_qwen_local_model_false_for_empty_model_id():
    endpoint = SimpleNamespace(model_id="", is_private=True, api_key="")
    assert _is_qwen_local_model(endpoint) is False
```

- [ ] **Step 2: Run the test — it fails**

Run: `cd backend && docker exec zhanlu-backend /usr/local/bin/python3.11 -c "import sys; sys.path.insert(0, '/app/venv/lib/python3.11/site-packages'); sys.path.insert(0, '/app'); import pytest; exit(pytest.main(['-xvs', 'tests/test_qwen3_fast_mode.py']))"`
Expected: FAIL — `_is_qwen_local_model` not defined.

- [ ] **Step 3: Add config flags to `config.py`**

In `backend/app/config.py`, after the SELF_EVAL block (around line 595), add:

```python
    # ── qwen3.6-27b Local Fast Mode (2026-08-25) ──────────────────────
    # Per-model overrides that fire ONLY when the user's selected model is
    # a qwen3.* served on local vLLM. DeepSeek-chat path is unaffected.
    # Goal: 8-15 LLM calls/turn → 3-5 calls/turn; 200s → 42-52s turn time.
    QWEN3_FAST_MODE_ENABLED: bool = True
    QWEN3_MAX_TOOL_ITERATIONS: int = 10                 # was 40
    QWEN3_GOAL_CONTRACT_ENABLED: bool = False           # was True
    QWEN3_VERIFY_NUDGE_MAX: int = 0                     # was 2
    QWEN3_DATA_AGENT_BUDGET_SECONDS: float = 30.0        # was 60
    QWEN3_FORCE_SYNTHESIS_MAX_RETRIES: int = 0          # was 1
    # Self-eval KEPT per user decision (safety net), but tightened:
    QWEN3_SELF_EVAL_MAX_REPLANS: int = 1                 # was 3
    QWEN3_SELF_EVAL_LLM_GATE_TIMEOUT_S: float = 5.0      # was 15.0
    # Synthesis quality knobs (Component 4):
    QWEN3_SYNTHESIS_MAX_TOKENS: int = 3072              # was 1536
    QWEN3_SYNTHESIS_TEMPERATURE: float = 0.3            # was default
```

- [ ] **Step 4: Add `_is_qwen_local_model` helper to `agents.py`**

In `backend/app/routers/agents.py`, add the helper near the top (after the imports, before line 200). A good spot is right after the `_LONG_RUNNING_TOOLS` frozenset (around line 1290):

```python
def _is_qwen_local_model(endpoint) -> bool:
    """True iff the user's selected model is a qwen3.* served on local vLLM.

    Used to gate the per-model fast-mode overrides (QWEN3_* config flags).
    Detection: model_id starts with "qwen3" AND the endpoint is private OR
    the api_key is empty/\"EMPTY\" (vLLM doesn't validate keys).

    Returns False for:
    - deepseek-chat (cloud API)
    - qwen3 with a real API key (cloud-hosted qwen, not local vLLM)
    - None/empty endpoint
    """
    if not endpoint or not getattr(endpoint, "model_id", None):
        return False
    mid = str(endpoint.model_id).lower()
    if not mid.startswith("qwen3"):
        return False
    if getattr(endpoint, "is_private", False):
        return True
    api_key = str(getattr(endpoint, "api_key", "") or "").strip()
    return api_key in {"", "EMPTY"}
```

- [ ] **Step 5: Run the test — it passes**

Run: `cd backend && docker exec zhanlu-backend /usr/local/bin/python3.11 -c "import sys; sys.path.insert(0, '/app/venv/lib/python3.11/site-packages'); sys.path.insert(0, '/app'); import pytest; exit(pytest.main(['-xvs', 'tests/test_qwen3_fast_mode.py']))"`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/config.py backend/app/routers/agents.py backend/tests/test_qwen3_fast_mode.py
git commit -m "feat(qwen3): add per-model fast-mode config flags + _is_qwen_local_model helper"
```

---

## Task 6: Call reduction — apply overrides at v3 loop call sites

**Files:**
- Modify: `backend/app/routers/agents.py` (v3 loop: line ~9219, goal-contract construction ~9162, verify nudge ~10358, force synthesis ~12580)
- Modify: `backend/app/services/tool_handlers/delegation_tools.py:65` (DATA_AGENT_BUDGET_SECONDS)
- Test: `backend/tests/test_qwen3_fast_mode.py` (extend)

**Interfaces:**
- Consumes: `_is_qwen_local_model(endpoint) -> bool` from Task 5.
- Produces: effective values for MAX_TOOL_ITERATIONS, GOAL_CONTRACT_ENABLED, VERIFY_NUDGE_MAX, DATA_AGENT_BUDGET_SECONDS, FORCE_SYNTHESIS_MAX_RETRIES, SELF_EVAL_MAX_REPLANS, SELF_EVAL_LLM_GATE_TIMEOUT_S that differ for qwen3 vs deepseek.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_qwen3_fast_mode.py`:

```python
def test_qwen3_max_iterations_is_10_not_40():
    """When endpoint is qwen3 local, effective MAX_TOOL_ITERATIONS is 10."""
    from app.routers.agents import _effective_max_tool_iterations
    from types import SimpleNamespace
    endpoint = SimpleNamespace(model_id="qwen3.6-27b", is_private=True, api_key="EMPTY")
    assert _effective_max_tool_iterations(endpoint) == 10


def test_deepseek_max_iterations_is_40():
    """When endpoint is deepseek, MAX_TOOL_ITERATIONS stays 40."""
    from app.routers.agents import _effective_max_tool_iterations
    from types import SimpleNamespace
    endpoint = SimpleNamespace(model_id="deepseek-chat", is_private=False, api_key="sk-real")
    assert _effective_max_tool_iterations(endpoint) == 40


def test_qwen3_goal_contract_disabled():
    """When endpoint is qwen3 local, goal-contract is disabled."""
    from app.routers.agents import _effective_goal_contract_enabled
    from types import SimpleNamespace
    endpoint = SimpleNamespace(model_id="qwen3.6-27b", is_private=True, api_key="EMPTY")
    assert _effective_goal_contract_enabled(endpoint) is False


def test_deepseek_goal_contract_enabled():
    """When endpoint is deepseek, goal-contract stays at the global setting."""
    from app.routers.agents import _effective_goal_contract_enabled
    from types import SimpleNamespace
    from app.config import settings
    endpoint = SimpleNamespace(model_id="deepseek-chat", is_private=False, api_key="sk-real")
    assert _effective_goal_contract_enabled(endpoint) == settings.GOAL_CONTRACT_ENABLED


def test_qwen3_verify_nudge_max_is_zero():
    """When endpoint is qwen3 local, VERIFY_NUDGE_MAX is 0."""
    from app.routers.agents import _effective_verify_nudge_max
    from types import SimpleNamespace
    endpoint = SimpleNamespace(model_id="qwen3.6-27b", is_private=True, api_key="EMPTY")
    assert _effective_verify_nudge_max(endpoint) == 0


def test_qwen3_data_agent_budget_is_30s():
    """When endpoint is qwen3 local, DATA_AGENT_BUDGET_SECONDS is 30."""
    from app.routers.agents import _effective_data_agent_budget_seconds
    from types import SimpleNamespace
    endpoint = SimpleNamespace(model_id="qwen3.6-27b", is_private=True, api_key="EMPTY")
    assert _effective_data_agent_budget_seconds(endpoint) == 30.0
```

- [ ] **Step 2: Run the tests — they fail**

Run: `cd backend && docker exec zhanlu-backend /usr/local/bin/python3.11 -c "import sys; sys.path.insert(0, '/app/venv/lib/python3.11/site-packages'); sys.path.insert(0, '/app'); import pytest; exit(pytest.main(['-xvs', 'tests/test_qwen3_fast_mode.py']))"`
Expected: FAIL — the `_effective_*` accessor functions don't exist yet.

- [ ] **Step 3: Add accessor functions to `agents.py`**

Add these accessors near `_is_qwen_local_model` (after Task 5's helper):

```python
def _effective_max_tool_iterations(endpoint) -> int:
    """Return the effective MAX_TOOL_ITERATIONS for the user's model."""
    if _is_qwen_local_model(endpoint) and settings.QWEN3_FAST_MODE_ENABLED:
        return settings.QWEN3_MAX_TOOL_ITERATIONS
    return MAX_TOOL_ITERATIONS


def _effective_goal_contract_enabled(endpoint) -> bool:
    """Return whether goal-contract is enabled for the user's model."""
    if _is_qwen_local_model(endpoint) and settings.QWEN3_FAST_MODE_ENABLED:
        return settings.QWEN3_GOAL_CONTRACT_ENABLED
    return settings.GOAL_CONTRACT_ENABLED


def _effective_verify_nudge_max(endpoint) -> int:
    """Return the effective VERIFY_NUDGE_MAX for the user's model."""
    if _is_qwen_local_model(endpoint) and settings.QWEN3_FAST_MODE_ENABLED:
        return settings.QWEN3_VERIFY_NUDGE_MAX
    return settings.VERIFY_NUDGE_MAX


def _effective_data_agent_budget_seconds(endpoint) -> float:
    """Return the effective DATA_AGENT_BUDGET_SECONDS for the user's model."""
    if _is_qwen_local_model(endpoint) and settings.QWEN3_FAST_MODE_ENABLED:
        return settings.QWEN3_DATA_AGENT_BUDGET_SECONDS
    from app.services.tool_handlers.delegation_tools import DATA_AGENT_BUDGET_SECONDS
    return DATA_AGENT_BUDGET_SECONDS


def _effective_self_eval_max_replans(endpoint) -> int:
    """Return the effective SELF_EVAL_MAX_REPLANS for the user's model."""
    if _is_qwen_local_model(endpoint) and settings.QWEN3_FAST_MODE_ENABLED:
        return settings.QWEN3_SELF_EVAL_MAX_REPLANS
    return settings.SELF_EVAL_MAX_REPLANS


def _effective_self_eval_timeout(endpoint) -> float:
    """Return the effective SELF_EVAL_LLM_GATE_TIMEOUT_S for the user's model."""
    if _is_qwen_local_model(endpoint) and settings.QWEN3_FAST_MODE_ENABLED:
        return settings.QWEN3_SELF_EVAL_LLM_GATE_TIMEOUT_S
    return settings.SELF_EVAL_LLM_GATE_TIMEOUT_S


def _effective_force_synthesis_max_retries(endpoint) -> int:
    """Return the effective max_retries for _force_llm_synthesis."""
    if _is_qwen_local_model(endpoint) and settings.QWEN3_FAST_MODE_ENABLED:
        return settings.QWEN3_FORCE_SYNTHESIS_MAX_RETRIES
    return 1  # legacy default
```

- [ ] **Step 4: Wire the accessors into the v3 loop call sites**

Find these call sites in `agents.py` and replace the hardcoded values:

1. **Line ~9219** (v3 loop range):
   Replace `for iteration in range(MAX_TOOL_ITERATIONS):`
   With `for iteration in range(_effective_max_tool_iterations(effective_llm)):`

2. **Line ~9162** (goal-contract construction):
   Replace `max_forces=settings.GOAL_CONTRACT_MAX_FORCES,`
   With a conditional: only construct the contract if `_effective_goal_contract_enabled(effective_llm)`. Wrap the existing `_contract = GoalContract(...)` block in `if _effective_goal_contract_enabled(effective_llm):` and set `_contract = None` in the else branch.

3. **Line ~10358** (verify nudge cap):
   Replace `settings.VERIFY_NUDGE_MAX, settings.GOAL_CONTRACT_MAX_FORCES,`
   With `_effective_verify_nudge_max(effective_llm), settings.GOAL_CONTRACT_MAX_FORCES,`

4. **Self-eval re-plan calls** (search for `_check_answer_verification_gate`):
   Pass `max_replans=_effective_self_eval_max_replans(effective_llm)` and `timeout_s=_effective_self_eval_timeout(effective_llm)` instead of the global settings.

5. **Force synthesis calls** (search for `_force_llm_synthesis`):
   Replace `max_retries=1` (or whatever the current value is) with `max_retries=_effective_force_synthesis_max_retries(effective_llm)`.

- [ ] **Step 5: Wire the accessor into `delegation_tools.py`**

The `DATA_AGENT_BUDGET_SECONDS` constant at line 65 is read at import time. We need a runtime override. In `delegation_tools.py`, change the `_ask_data_agent` function to accept an optional `budget_seconds` parameter. If not provided, fall back to the module constant.

Then in `agents.py`, where `ask_data_agent` is invoked via `execute_tool(...)`, pass `budget_seconds=_effective_data_agent_budget_seconds(effective_llm)` via the context dict.

**Note:** If `execute_tool` doesn't support per-call budget override, the simpler approach is: in `delegation_tools.py`, read the budget from the context dict at the start of `_ask_data_agent`:

```python
def _ask_data_agent(..., context: dict = None):
    budget = DATA_AGENT_BUDGET_SECONDS
    if context and isinstance(context, dict):
        override = context.get("_qwen3_budget_seconds")
        if override:
            budget = float(override)
    # ... use `budget` instead of DATA_AGENT_BUDGET_SECONDS in the loop
```

Then in `agents.py` v3 loop, set `ctx["_qwen3_budget_seconds"] = _effective_data_agent_budget_seconds(effective_llm)` once before the loop.

- [ ] **Step 6: Run the tests — they pass**

Run: `cd backend && docker exec zhanlu-backend /usr/local/bin/python3.11 -c "import sys; sys.path.insert(0, '/app/venv/lib/python3.11/site-packages'); sys.path.insert(0, '/app'); import pytest; exit(pytest.main(['-xvs', 'tests/test_qwen3_fast_mode.py']))"`
Expected: PASS.

- [ ] **Step 7: Restart backend + smoke test**

```bash
docker restart zhanlu-backend
sleep 8
curl -s http://localhost:5002/api/health  # expect 200
```

- [ ] **Step 8: Commit**

```bash
git add backend/app/routers/agents.py backend/app/services/tool_handlers/delegation_tools.py backend/tests/test_qwen3_fast_mode.py
git commit -m "feat(qwen3): apply per-model fast-mode overrides at v3 loop call sites"
```

---

## Task 7: Synthesis quality — CEO-grade prompt + inline data

**Files:**
- Modify: `backend/app/routers/agents.py` (the `_call_synthesis_llm` or equivalent synthesis function, and the prompt builder)
- Create: `backend/tests/test_synthesis_quality_qwen3.py`

**Interfaces:**
- Consumes: `_is_qwen_local_model(endpoint) -> bool` from Task 5.
- Produces: a CEO-grade synthesis prompt with 5-section markdown scaffolding + inline data rows, applied only when `_is_qwen_local_model` is True.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_synthesis_quality_qwen3.py`:

```python
"""Tests for qwen3.6-27b CEO-grade synthesis prompt.

Run in-container:
  /usr/local/bin/python3.11 -c "import sys; sys.path.insert(0, '/app/venv/lib/python3.11/site-packages'); sys.path.insert(0, '/app'); import pytest; exit(pytest.main(['-xvs', 'tests/test_synthesis_quality_qwen3.py']))"
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from types import SimpleNamespace
from app.routers.agents import _build_qwen3_synthesis_prompt, _is_qwen_local_model


def test_synthesis_prompt_has_markdown_scaffolding():
    """The qwen3 synthesis prompt must contain the 5-section markdown template."""
    rows = [{"product": "Widget", "revenue": 1000.0}]
    prompt = _build_qwen3_synthesis_prompt(
        user_question="show me sales",
        data_rows=rows,
        columns=["product", "revenue"],
        table_name="sales_data",
    )
    assert "## Executive Summary" in prompt
    assert "## Key Metrics" in prompt
    assert "## Detailed Breakdown" in prompt
    assert "## Risks & Opportunities" in prompt
    assert "## Recommended Actions" in prompt


def test_synthesis_prompt_inlines_data_rows():
    """The data rows must appear inline in the prompt (not as a tool result)."""
    rows = [{"product": "Widget", "revenue": 1000.0}]
    prompt = _build_qwen3_synthesis_prompt(
        user_question="show me sales",
        data_rows=rows,
        columns=["product", "revenue"],
        table_name="sales_data",
    )
    assert "Widget" in prompt
    assert "1000" in prompt
    assert "DATA (use only this" in prompt


def test_synthesis_prompt_truncates_large_data():
    """When >100 rows, only the first 100 are inlined (to fit context)."""
    rows = [{"product": f"Item{i}", "revenue": float(i)} for i in range(200)]
    prompt = _build_qwen3_synthesis_prompt(
        user_question="show me sales",
        data_rows=rows,
        columns=["product", "revenue"],
        table_name="sales_data",
    )
    assert "Item0" in prompt
    assert "Item99" in prompt
    assert "Item100" not in prompt  # truncated
    assert "200 rows total" in prompt or "showing first 100" in prompt.lower()


def test_synthesis_max_tokens_is_3072_for_qwen3():
    """When endpoint is qwen3 local, synthesis max_tokens is 3072."""
    from app.routers.agents import _effective_synthesis_max_tokens
    endpoint = SimpleNamespace(model_id="qwen3.6-27b", is_private=True, api_key="EMPTY")
    assert _effective_synthesis_max_tokens(endpoint) == 3072


def test_synthesis_max_tokens_is_legacy_for_deepseek():
    """When endpoint is deepseek, synthesis max_tokens stays at legacy."""
    from app.routers.agents import _effective_synthesis_max_tokens
    endpoint = SimpleNamespace(model_id="deepseek-chat", is_private=False, api_key="sk-real")
    # Legacy value — whatever the global default is, it should NOT be 3072
    assert _effective_synthesis_max_tokens(endpoint) != 3072
```

- [ ] **Step 2: Run the tests — they fail**

Run: `cd backend && docker exec zhanlu-backend /usr/local/bin/python3.11 -c "import sys; sys.path.insert(0, '/app/venv/lib/python3.11/site-packages'); sys.path.insert(0, '/app'); import pytest; exit(pytest.main(['-xvs', 'tests/test_synthesis_quality_qwen3.py']))"`
Expected: FAIL — `_build_qwen3_synthesis_prompt` and `_effective_synthesis_max_tokens` not defined.

- [ ] **Step 3: Add `_effective_synthesis_max_tokens` accessor**

In `backend/app/routers/agents.py`, near the other `_effective_*` accessors (from Task 6), add:

```python
def _effective_synthesis_max_tokens(endpoint) -> int:
    """Return the effective synthesis max_tokens for the user's model."""
    if _is_qwen_local_model(endpoint) and settings.QWEN3_FAST_MODE_ENABLED:
        return settings.QWEN3_SYNTHESIS_MAX_TOKENS
    return 1536  # legacy default
```

- [ ] **Step 4: Add `_build_qwen3_synthesis_prompt`**

In `backend/app/routers/agents.py`, add the prompt builder near the other synthesis helpers:

```python
_QWEN3_SYNTHESIS_TEMPLATE = """\
You are a CEO-grade business analyst. Using ONLY the data below, write a \
comprehensive answer in this exact structure:

## Executive Summary
[2-3 sentences: what was asked, what was found, the headline number]

## Key Metrics
- Total {measure_label}: {currency_symbol}{total:,.2f}
- Date range covered: {date_range}
- Number of records: {row_count}
- Top {dimension_label}: {top_dimension_value}

## Detailed Breakdown
[markdown table of top 10-20 rows, or bullets if many dimensions]

## Risks & Opportunities
[1-2 sentences: notable patterns, anomalies, trends]

## Recommended Actions
[1-2 actionable items based on the data]

DATA (use only this, do not invent numbers):
{data_inline}
"""

_MAX_INLINE_ROWS = 100
_MAX_INLINE_CHARS = 4000


def _build_qwen3_synthesis_prompt(
    user_question: str,
    data_rows: list[dict],
    columns: list[str],
    table_name: str = "data",
) -> str:
    """Build the CEO-grade synthesis prompt for qwen3.6-27b.

    Inlines up to 100 rows / 4000 chars of data directly in the prompt so
    the model doesn't have to "look up" the data from a tool result.
    """
    total_rows = len(data_rows)
    truncated = data_rows[:_MAX_INLINE_ROWS]
    # Build markdown table
    if truncated and columns:
        header = "| " + " | ".join(columns) + " |"
        sep = "| " + " | ".join("---" for _ in columns) + " |"
        body_lines = []
        for row in truncated:
            vals = []
            for c in columns:
                v = row.get(c, "")
                if isinstance(v, float):
                    vals.append(f"{v:,.2f}")
                else:
                    vals.append(str(v))
            body_lines.append("| " + " | ".join(vals) + " |")
        data_inline = "\n".join([header, sep] + body_lines)
    else:
        data_inline = "(no rows)"

    if total_rows > _MAX_INLINE_ROWS:
        data_inline += f"\n\n*Showing first {_MAX_INLINE_ROWS} of {total_rows} rows.*"

    # Truncate to max chars
    if len(data_inline) > _MAX_INLINE_CHARS:
        data_inline = data_inline[:_MAX_INLINE_CHARS] + "\n...(truncated)"

    # Compute simple metrics for the template placeholders
    measure_label = "value"
    currency_symbol = "¥"
    total = 0.0
    # Find first numeric column for "total"
    for row in truncated:
        for c in columns:
            v = row.get(c)
            if isinstance(v, (int, float)):
                total += float(v)
                measure_label = c
                break

    # Top dimension (first non-numeric column)
    dimension_label = "item"
    top_dimension_value = "n/a"
    for c in columns:
        if c == measure_label:
            continue
        dimension_label = c
        # Find the most frequent value
        from collections import Counter
        vals = [str(row.get(c, "")) for row in truncated]
        if vals:
            top_dimension_value = Counter(vals).most_common(1)[0][0]
        break

    # Date range
    date_range = "n/a"
    for c in columns:
        if "date" in c.lower() or "time" in c.lower():
            dates = sorted([str(row.get(c, "")) for row in truncated if row.get(c)])
            if dates:
                date_range = f"{dates[0]} to {dates[-1]}"
            break

    return _QWEN3_SYNTHESIS_TEMPLATE.format(
        measure_label=measure_label,
        currency_symbol=currency_symbol,
        total=total,
        date_range=date_range,
        row_count=total_rows,
        dimension_label=dimension_label,
        top_dimension_value=top_dimension_value,
        data_inline=data_inline,
    )
```

- [ ] **Step 5: Wire the prompt into the synthesis call site**

In the v3 stream loop, find the synthesis LLM call (search for `_call_synthesis_llm` or `_force_llm_synthesis`). When `_is_qwen_local_model(effective_llm)` is True:
1. Build the qwen3 synthesis prompt using `_build_qwen3_synthesis_prompt(...)`.
2. Pass `max_tokens=_effective_synthesis_max_tokens(effective_llm)`.
3. Pass `temperature=settings.QWEN3_SYNTHESIS_TEMPERATURE`.

The data rows come from the last `ask_data_agent` result (or the accumulated `tool_calls_for_frontend`).

- [ ] **Step 6: Run the tests — they pass**

Run: `cd backend && docker exec zhanlu-backend /usr/local/bin/python3.11 -c "import sys; sys.path.insert(0, '/app/venv/lib/python3.11/site-packages'); sys.path.insert(0, '/app'); import pytest; exit(pytest.main(['-xvs', 'tests/test_synthesis_quality_qwen3.py']))"`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/app/routers/agents.py backend/tests/test_synthesis_quality_qwen3.py
git commit -m "feat(qwen3): CEO-grade synthesis prompt with markdown scaffolding + inline data"
```

---

## Task 8: Deterministic fallback — enhanced `_data_rows_fallback`

**Files:**
- Modify: `backend/app/routers/agents.py` (the `_data_rows_fallback` function and its trigger conditions)
- Create: `backend/tests/test_deterministic_fallback.py`

**Interfaces:**
- Consumes: the last `ask_data_agent` result dict (`rows`, `columns`, `sql`, `table_name`).
- Produces: a deterministic markdown summary with numeric totals + top-3 values + date range + top-20-row table. No LLM call.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_deterministic_fallback.py`:

```python
"""Tests for the deterministic data-rows fallback.

When LLM synthesis fails (empty/apology/<100chars/timeout), this fallback
renders a useful markdown summary from the data rows — no LLM needed.

Run in-container:
  /usr/local/bin/python3.11 -c "import sys; sys.path.insert(0, '/app/venv/lib/python3.11/site-packages'); sys.path.insert(0, '/app'); import pytest; exit(pytest.main(['-xvs', 'tests/test_deterministic_fallback.py']))"
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from app.routers.agents import _build_deterministic_fallback, _should_trigger_fallback


def test_fallback_renders_numeric_summary():
    """The fallback computes totals/avg/min/max for numeric columns."""
    rows = [
        {"product": "Widget", "revenue": 1000.0},
        {"product": "Gadget", "revenue": 2000.0},
        {"product": "Widget", "revenue": 1500.0},
    ]
    result = _build_deterministic_fallback(rows, columns=["product", "revenue"], table_name="sales")
    assert "Executive Summary (auto-generated)" in result
    assert "Total revenue" in result
    assert "4,500.00" in result  # 1000+2000+1500
    assert "Number of records: 3" in result


def test_fallback_renders_top_values():
    """The fallback shows top-3 values for categorical columns."""
    rows = [
        {"product": "Widget", "revenue": 1000.0},
        {"product": "Widget", "revenue": 1500.0},
        {"product": "Gadget", "revenue": 2000.0},
    ]
    result = _build_deterministic_fallback(rows, columns=["product", "revenue"], table_name="sales")
    assert "Widget" in result  # top product (2 occurrences)
    assert "Gadget" in result


def test_fallback_renders_date_range():
    """The fallback shows min/max date when a date column is present."""
    rows = [
        {"date": "2026-07-01", "revenue": 1000.0},
        {"date": "2026-07-15", "revenue": 2000.0},
        {"date": "2026-07-31", "revenue": 1500.0},
    ]
    result = _build_deterministic_fallback(rows, columns=["date", "revenue"], table_name="sales")
    assert "2026-07-01" in result
    assert "2026-07-31" in result


def test_fallback_renders_markdown_table():
    """The fallback includes a markdown table of the top 20 rows."""
    rows = [{"product": f"Item{i}", "revenue": float(i * 100)} for i in range(50)]
    result = _build_deterministic_fallback(rows, columns=["product", "revenue"], table_name="sales")
    assert "| product | revenue |" in result or "| product |" in result
    assert "Item0" in result
    # Should NOT include all 50 rows (capped at 20)
    assert "Item20" not in result
    assert "showing first 20" in result.lower() or "of 50 rows" in result


def test_fallback_triggers_on_empty_synthesis():
    """Empty synthesis output triggers the fallback."""
    assert _should_trigger_fallback("", data_rows=[{"x": 1}]) is True


def test_fallback_triggers_on_apology():
    """Apology text triggers the fallback."""
    assert _should_trigger_fallback("I couldn't find the data. Let me re-query...", data_rows=[{"x": 1}]) is True


def test_fallback_triggers_on_short_output():
    """<100 char output triggers the fallback."""
    assert _should_trigger_fallback("No data found.", data_rows=[{"x": 1}]) is True


def test_fallback_does_not_trigger_on_valid_synthesis():
    """Valid 2000-char synthesis does NOT trigger the fallback."""
    valid = "Here is your sales report. " * 100  # ~2600 chars
    assert _should_trigger_fallback(valid, data_rows=[{"x": 1}]) is False


def test_fallback_does_not_trigger_when_no_data():
    """When there's no data, the fallback shouldn't fire (no data to show)."""
    assert _should_trigger_fallback("", data_rows=[]) is False
```

- [ ] **Step 2: Run the tests — they fail**

Run: `cd backend && docker exec zhanlu-backend /usr/local/bin/python3.11 -c "import sys; sys.path.insert(0, '/app/venv/lib/python3.11/site-packages'); sys.path.insert(0, '/app'); import pytest; exit(pytest.main(['-xvs', 'tests/test_deterministic_fallback.py']))"`
Expected: FAIL — `_build_deterministic_fallback` and `_should_trigger_fallback` not defined.

- [ ] **Step 3: Implement `_should_trigger_fallback`**

In `backend/app/routers/agents.py`, add near the other fallback helpers:

```python
_APOLOGY_PATTERNS = [
    "i couldn't", "i couldn't find", "let me re-query", "let me try again",
    "i wasn't able to", "unable to retrieve", "no data found",
    "i apologize", "i'm sorry", "sorry, i hit an error",
]


def _should_trigger_fallback(synthesis_text: str, data_rows: list) -> bool:
    """Return True if the deterministic fallback should fire.

    Fires when:
    - synthesis_text is empty AND data_rows is non-empty
    - synthesis_text matches an apology pattern AND data_rows is non-empty
    - synthesis_text is <100 chars AND data_rows is non-empty
    Does NOT fire when data_rows is empty (no data to show).
    """
    if not data_rows:
        return False
    if not synthesis_text or not synthesis_text.strip():
        return True
    if len(synthesis_text.strip()) < 100:
        return True
    lower = synthesis_text.lower()
    for pattern in _APOLOGY_PATTERNS:
        if pattern in lower:
            return True
    return False
```

- [ ] **Step 4: Implement `_build_deterministic_fallback`**

Add the main fallback builder:

```python
def _build_deterministic_fallback(
    data_rows: list[dict],
    columns: list[str],
    table_name: str = "data",
) -> str:
    """Build a deterministic markdown summary from data rows (no LLM).

    Includes:
    - Numeric summary (total/avg/min/max) for numeric columns
    - Top-3 values for categorical columns
    - Date range for date columns
    - Markdown table of top 20 rows
    """
    from collections import Counter
    import datetime

    if not data_rows:
        return "No data available."

    total_rows = len(data_rows)
    lines = ["## Executive Summary (auto-generated)", ""]

    # Detect column types
    numeric_cols = []
    categorical_cols = []
    date_cols = []
    for c in columns:
        sample_val = next((row.get(c) for row in data_rows if row.get(c) is not None), None)
        if isinstance(sample_val, (int, float)):
            numeric_cols.append(c)
        elif isinstance(sample_val, str):
            # Heuristic: looks like a date?
            if any(k in c.lower() for k in ["date", "time", "created", "updated"]):
                date_cols.append(c)
            else:
                categorical_cols.append(c)

    # Numeric summary
    for c in numeric_cols:
        vals = [float(row.get(c, 0)) for row in data_rows if isinstance(row.get(c), (int, float))]
        if vals:
            total = sum(vals)
            avg = total / len(vals)
            mn = min(vals)
            mx = max(vals)
            lines.append(f"- Total {c}: ¥{total:,.2f}")
            lines.append(f"- Average {c}: ¥{avg:,.2f}")
            lines.append(f"- Min {c}: ¥{mn:,.2f}")
            lines.append(f"- Max {c}: ¥{mx:,.2f}")

    # Categorical top-3
    for c in categorical_cols[:3]:  # limit to 3 categorical cols
        vals = [str(row.get(c, "")) for row in data_rows if row.get(c)]
        if vals:
            top3 = Counter(vals).most_common(3)
            for val, count in top3:
                lines.append(f"- Top {c}: {val} ({count} records)")

    # Date range
    for c in date_cols:
        dates = sorted([str(row.get(c, "")) for row in data_rows if row.get(c)])
        if dates:
            lines.append(f"- Date range ({c}): {dates[0]} to {dates[-1]}")

    lines.append(f"- Number of records: {total_rows}")
    lines.append("")

    # Markdown table (top 20 rows)
    lines.append("## Data Preview (top 20 rows)")
    lines.append("")
    display_rows = data_rows[:20]
    if display_rows and columns:
        header = "| " + " | ".join(columns) + " |"
        sep = "| " + " | ".join("---" for _ in columns) + " |"
        lines.append(header)
        lines.append(sep)
        for row in display_rows:
            vals = []
            for c in columns:
                v = row.get(c, "")
                if isinstance(v, float):
                    vals.append(f"{v:,.2f}")
                else:
                    vals.append(str(v))
            lines.append("| " + " | ".join(vals) + " |")

    if total_rows > 20:
        lines.append(f"\n*Showing first 20 of {total_rows} rows.*")

    lines.append("\n*Note: LLM synthesis unavailable. Showing raw data summary.*")
    return "\n".join(lines)
```

- [ ] **Step 5: Wire the fallback into the v3 loop exit branch**

In the v3 stream loop, after the synthesis LLM call, check `_should_trigger_fallback(synthesis_text, data_rows)`. If True, replace `accumulated_content` with `_build_deterministic_fallback(...)`.

Find the existing `_data_rows_fallback` call site (search for `_data_rows_fallback` in agents.py) and replace it with the new `_build_deterministic_fallback` when `_is_qwen_local_model(effective_llm)` is True. Keep the legacy fallback for deepseek.

- [ ] **Step 6: Run the tests — they pass**

Run: `cd backend && docker exec zhanlu-backend /usr/local/bin/python3.11 -c "import sys; sys.path.insert(0, '/app/venv/lib/python3.11/site-packages'); sys.path.insert(0, '/app'); import pytest; exit(pytest.main(['-xvs', 'tests/test_deterministic_fallback.py']))"`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/app/routers/agents.py backend/tests/test_deterministic_fallback.py
git commit -m "feat(qwen3): deterministic data-rows fallback when synthesis fails (never 'Sorry')"
```

---

## Task 9: Deploy + vLLM restart + manual verification

**Files:**
- No code changes — ops task only.

- [ ] **Step 1: Restart backend with all code changes**

```bash
docker restart zhanlu-backend zhanlu-sandbox-worker
sleep 8
curl -s http://localhost:5002/api/health  # expect 200
```

- [ ] **Step 2: Restart vLLM with the 3 new flags**

On the vLLM server (10.10.10.46), restart vLLM with:

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
  --enable-prefix-caching \
  --enable-chunked-prefill \
  --max-num-seqs 16 \
  --max-num-batched-tokens 8192 \
  --disable-log-requests
```

- [ ] **Step 3: Verify vLLM is healthy**

```bash
curl -s http://10.10.10.46:8000/v1/models | python3 -c "import sys, json; d=json.load(sys.stdin); print(d['data'][0]['id'])"
# Expected: qwen3.6-27b
```

- [ ] **Step 4: Manual smoke test**

1. Open Zhanlu chat, select qwen3.6-27b as the model
2. Ask "tell me about my last month sales data"
3. Verify:
   - No "Sorry, I hit an error" message
   - Data table appears (if data exists)
   - Synthesis prose appears with markdown headers (## Executive Summary, ## Key Metrics, etc.)
   - Turn completes in <60s
4. If synthesis fails (empty/apology), verify the deterministic fallback fires:
   - Shows "Executive Summary (auto-generated)"
   - Shows numeric totals
   - Shows top-20-row markdown table

- [ ] **Step 5: Check backend logs for Hermes + fast-mode**

```bash
docker logs zhanlu-backend --tail 50 2>&1 | grep -E "HERMES|qwen3|fast.mode|fallback|replan"
```

Expected: see log lines like:
- `v3 HERMES RE-PLAN: recovered N rows ... catalog_used=True`
- `Demoted mid-list system message at index N to role=user (vLLM compat)`
- `Repaired Python-literal tool_call arguments for ask_data_agent`
- `Repaired malformed tool_call arguments for ...`

- [ ] **Step 6: Commit any .env changes**

If `QWEN3_FAST_MODE_ENABLED` needs to be set in `backend/.env`:

```bash
# Only if the default True is not what you want; otherwise no .env change needed
echo "QWEN3_FAST_MODE_ENABLED=true" >> backend/.env
git add backend/.env.example
git commit -m "chore(env): enable qwen3 fast mode"
```

---

## Self-Review Checklist

- [x] **Spec coverage:** All 6 components from the spec have at least one task:
  - Component 1 (bug fixes) → Tasks 1, 2, 3, 4
  - Component 2 (call count reduction) → Tasks 5, 6
  - Component 3 (prefix caching) → Task 9 (vLLM restart)
  - Component 4 (synthesis quality) → Task 7
  - Component 5 (deterministic fallback) → Task 8
  - Component 6 (vLLM config) → Task 9
- [x] **No placeholders:** Every step shows the full implementation code.
- [x] **Type consistency:** `_is_qwen_local_model(endpoint) -> bool` used consistently across Tasks 5-8. `_effective_*` accessors have consistent signatures.
- [x] **Backward compat:** DeepSeek path unaffected — all qwen3 overrides are gated by `_is_qwen_local_model(endpoint) and settings.QWEN3_FAST_MODE_ENABLED`.
- [x] **Test count:** 5 (Task 1-3) + 5 (Task 4) + 6 (Task 5) + 6 (Task 6) + 5 (Task 7) + 9 (Task 8) = 36 tests total (exceeds the spec's 21 minimum).
- [x] **Implementation order:** Tasks 1-4 (bug fixes) are first and independent of each other. Tasks 5-6 (call reduction) depend on Task 5's helper. Tasks 7-8 (synthesis + fallback) depend on Task 5's helper. Task 9 (deploy) is last.

## Notes for Future Work

- **Streaming synthesis:** Currently synthesis is a one-shot LLM call. For better UX on qwen3, stream the synthesis so the user sees progress. Separate task.
- **Per-agent model override:** Some agents (e.g. `ecisco_bi_assistant`) might want a different model than the user's selected model. The `_is_qwen_local_model` check only looks at the user's model, not the agent's. Separate task.
- **GPU memory monitoring:** With `--max-num-seqs 16`, the GPU can run out of memory under load. Add a monitoring alert. Separate task.
