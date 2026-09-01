# P0 Agent Reliability — Loop Guardrails, Iteration Budget, Tool Result Persistence

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Port three P0 reliability capabilities from Hermes into Zhanlu agents: (1) rich tool-call loop guardrails, (2) a per-conversation iteration budget, and (3) a 3-layer tool result persistence system.

**Architecture:** Three independent service modules under `app/services/`, each wired into the existing `add_message` / resume / stream turn loops in `app/routers/agents.py`. The existing `_detect_tool_call_loop` history-scan stays as a complementary defense; the new modules add finer-grained, per-turn, incremental detection. All three modules are pure-Python, no new dependencies, fully testable in isolation.

**Tech Stack:** Python 3.11+, asyncio, pytest, SQLAlchemy (existing). No new pip dependencies.

---

## Current State (what already exists)

| Capability | Current Zhanlu | Gap vs Hermes |
|---|---|---|
| Loop detection | `_detect_tool_call_loop()` — full-history scan, detects exact-same-tool+args repeat at `TOOL_CALL_HARD_CAP=6`. Success-aware (failed calls get cap+1). | Only 1 of 3 patterns. No same-tool-different-args failure detection. No no-progress (identical result) detection. No warn-before-block escalation. Full rescan each iteration (O(n)). |
| Iteration cap | `MAX_TOOL_ITERATIONS=10` hardcoded per-turn. Resume loop adds another 10. `AgentApp.max_iterations` / `max_call_count` fields exist in the model but are NOT wired into the backend loop. | Not configurable per-agent. No total-across-resumes cap. No refund for `execute_code`. |
| Tool output | Layer 1 only: per-handler `truncate_output(data, TOOL_MAX_OUTPUT_CHARS=8000)`. Applied inconsistently (some handlers skip it). Raw `json.dumps(result)` stored into `llm_messages` with NO size limit at the storage point. | No Layer 2 (persist-to-disk + preview). No Layer 3 (per-turn aggregate budget). No context-window-scaled budget. |

## Key Files

- **Turn loops (3 sites, same structure):** `app/routers/agents.py` lines ~2197 (main), ~3208 (resume), ~4562 (stream)
- **Existing loop guard:** `app/routers/agents.py` lines 83-217 (constants + `_detect_tool_call_loop`)
- **Tool execution:** `app/services/agent_tools.py` (`execute_tool`, `execute_tool_with_retry`)
- **Tool retry:** `app/services/tool_retry.py`
- **Config:** `app/config.py` (Settings class)
- **Compaction:** `app/services/compaction/microcompact.py` (`COMPACTABLE_TOOLS`)
- **Tool security:** `app/services/tool_security.py` (`truncate_output`)
- **Tests:** `backend/tests/` (pytest, asyncio_mode=auto)

---

## Task 1: Create `tool_loop_guardrails.py` — per-turn guardrail controller

**Files:**
- Create: `backend/app/services/tool_loop_guardrails.py`
- Test: `backend/tests/test_tool_loop_guardrails.py`

**Step 1: Write the failing test**

```python
# tests/test_tool_loop_guardrails.py
"""Tests for the per-turn tool-call loop guardrail controller.

Covers the 3 loop patterns Hermes detects that Zhanlu's history-scan
_detect_tool_call_loop does not:
  1. same-tool-failure (same tool, DIFFERENT args, all failing)
  2. no-progress (idempotent tool returning identical results)
  3. warn-before-block escalation
"""
import json
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.tool_loop_guardrails import (
    ToolLoopGuardController,
    ToolGuardrailConfig,
)


def test_same_tool_different_args_failure_loop_trips():
    """Same tool failing with DIFFERENT args 8 times should halt."""
    ctrl = ToolLoopGuardController(ToolGuardrailConfig(hard_stop_enabled=True))
    args_sequence = [{"query": f"q{i}"} for i in range(8)]
    for args in args_sequence:
        ctrl.before_call("web_search", args)
        decision = ctrl.after_call("web_search", args, json.dumps({"success": False, "error": "boom"}))
        if decision.should_halt:
            break
    assert ctrl.halt_decision is not None
    assert ctrl.halt_decision.code == "same_tool_failure_halt"


def test_no_progress_idempotent_loop_trips():
    """read_file returning the same content 5 times should block."""
    ctrl = ToolLoopGuardController(ToolGuardrailConfig(hard_stop_enabled=True))
    result = json.dumps({"success": True, "content": "same file body"})
    for _ in range(5):
        ctrl.before_call("read_file", {"path": "/a.txt"})
        decision = ctrl.after_call("read_file", {"path": "/a.txt"}, result)
    assert ctrl.halt_decision is not None
    assert ctrl.halt_decision.code == "no_progress_block"


def test_warn_before_block_when_warnings_enabled():
    """With warnings on + hard_stop off, repeated exact failures warn but don't block."""
    ctrl = ToolLoopGuardController(ToolGuardrailConfig(warnings_enabled=True, hard_stop_enabled=False))
    args = {"path": "/missing.txt"}
    for _ in range(3):
        ctrl.before_call("read_file", args)
        decision = ctrl.after_call("read_file", args, json.dumps({"success": False, "error": "nope"}))
    # Should have warned (action=warn), not halted
    assert not ctrl.halt_decision
    assert decision.action == "warn"


def test_success_resets_failure_counter():
    """A success after failures resets the exact-failure counter."""
    ctrl = ToolLoopGuardController(ToolGuardrailConfig(hard_stop_enabled=True))
    args = {"path": "/x.txt"}
    # 4 failures (under halt threshold of 8)
    for _ in range(4):
        ctrl.before_call("read_file", args)
        ctrl.after_call("read_file", args, json.dumps({"success": False}))
    # One success
    ctrl.before_call("read_file", args)
    ctrl.after_call("read_file", args, json.dumps({"success": True}))
    # 4 more failures — should NOT trip because counter was reset
    for _ in range(4):
        ctrl.before_call("read_file", args)
        ctrl.after_call("read_file", args, json.dumps({"success": False}))
    assert ctrl.halt_decision is None


def test_different_results_do_not_trip_no_progress():
    """read_file returning DIFFERENT content each time is NOT a no-progress loop."""
    ctrl = ToolLoopGuardController(ToolGuardrailConfig(hard_stop_enabled=True))
    for i in range(6):
        result = json.dumps({"success": True, "content": f"version {i}"})
        ctrl.before_call("read_file", {"path": "/a.txt"})
        ctrl.after_call("read_file", {"path": "/a.txt"}, result)
    assert ctrl.halt_decision is None


def test_mutation_tool_never_trips_no_progress():
    """write_file is not idempotent; repeated identical calls are not no-progress."""
    ctrl = ToolLoopGuardController(ToolGuardrailConfig(hard_stop_enabled=True))
    result = json.dumps({"success": True})
    for _ in range(6):
        ctrl.before_call("write_file", {"path": "/a.txt", "content": "x"})
        ctrl.after_call("write_file", {"path": "/a.txt", "content": "x"}, result)
    assert ctrl.halt_decision is None
```

**Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_tool_loop_guardrails.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.tool_loop_guardrails'`

**Step 3: Write minimal implementation**

```python
# app/services/tool_loop_guardrails.py
"""Per-turn tool-call loop guardrail controller.

Complements the history-scan ``_detect_tool_call_loop`` in agents.py with
finer-grained, incremental, per-turn detection of three loop patterns:

1. **Exact-failure loop**: same tool + same args failing repeatedly.
2. **Same-tool-failure loop**: same tool failing with DIFFERENT args.
3. **No-progress loop**: idempotent (read-only) tool returning identical results.

The controller is stateful but side-effect free: it tracks per-turn
observations and returns decisions. The turn loop owns whether a "block"
decision becomes a synthetic tool result or a turn halt.

Inspired by Hermes' ``agent/tool_guardrails.py``, adapted for Zhanlu's
OpenAI-compatible message format (dicts, not dataclasses).
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping

logger = logging.getLogger(__name__)

# Read-only tools where identical output = no progress.
IDEMPOTENT_TOOL_NAMES: frozenset[str] = frozenset({
    "read_file", "web_search", "web_extract", "list_tools",
    "list_market_agents", "list_knowledge_bases", "search_skills",
    "skills", "skills_hub",
})

# Mutating tools — repeated calls are never "no-progress" (the world may have changed).
MUTATING_TOOL_NAMES: frozenset[str] = frozenset({
    "write_file", "create_agent", "update_agent", "create_skill",
    "update_skill", "create_automation", "update_automation",
    "execute_code", "ask_data_agent", "memory",
})


@dataclass(frozen=True)
class ToolGuardrailConfig:
    """Thresholds for per-turn loop detection."""
    warnings_enabled: bool = True
    hard_stop_enabled: bool = True
    exact_failure_warn_after: int = 2
    exact_failure_block_after: int = 5
    same_tool_failure_warn_after: int = 3
    same_tool_failure_halt_after: int = 8
    no_progress_warn_after: int = 2
    no_progress_block_after: int = 5
    idempotent_tools: frozenset[str] = field(default_factory=lambda: IDEMPOTENT_TOOL_NAMES)
    mutating_tools: frozenset[str] = field(default_factory=lambda: MUTATING_TOOL_NAMES)


@dataclass(frozen=True)
class ToolGuardrailDecision:
    """Decision returned by the controller."""
    action: str = "allow"  # allow | warn | block | halt
    code: str = "allow"
    message: str = ""
    tool_name: str = ""
    count: int = 0

    @property
    def allows_execution(self) -> bool:
        return self.action in {"allow", "warn"}

    @property
    def should_halt(self) -> bool:
        return self.action in {"block", "halt"}

    def to_metadata(self) -> dict[str, Any]:
        return {
            "action": self.action, "code": self.code,
            "message": self.message, "tool_name": self.tool_name,
            "count": self.count,
        }


def _canonical_args(args: Mapping[str, Any] | None) -> str:
    if not isinstance(args, Mapping):
        args = {}
    return json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _args_hash(args: Mapping[str, Any] | None) -> str:
    return hashlib.sha256(_canonical_args(args).encode("utf-8", "surrogatepass")).hexdigest()


def _result_hash(result: str | None) -> str:
    """Stable hash of a tool result string."""
    return hashlib.sha256((result or "").encode("utf-8", "surrogatepass")).hexdigest()


def _is_failure(result_content: str | None) -> bool:
    """Classify a tool result JSON string as failure or success."""
    if not result_content:
        return False
    try:
        payload = json.loads(result_content)
    except (ValueError, TypeError):
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get("requires_approval"):
        return False
    return payload.get("success") is False


class ToolLoopGuardController:
    """Per-turn controller for repeated failed / non-progressing tool calls.

    Usage in a turn loop::

        ctrl = ToolLoopGuardController()
        for call in tool_calls:
            decision = ctrl.before_call(name, args)
            if not decision.allows_execution:
                # inject synthetic blocked result
                continue
            result = await execute_tool(...)
            decision = ctrl.after_call(name, args, result_json)
            if decision.should_halt:
                break
    """

    def __init__(self, config: ToolGuardrailConfig | None = None):
        self.config = config or ToolGuardrailConfig()
        self.reset_for_turn()

    def reset_for_turn(self) -> None:
        self._exact_failure_counts: dict[str, int] = {}  # key = name+args_hash
        self._same_tool_failure_counts: dict[str, int] = {}  # key = name
        self._no_progress: dict[str, tuple[str, int]] = {}  # key = name+args_hash -> (result_hash, count)
        self._halt_decision: ToolGuardrailDecision | None = None

    @property
    def halt_decision(self) -> ToolGuardrailDecision | None:
        return self._halt_decision

    def _signature_key(self, tool_name: str, args: Mapping[str, Any] | None) -> str:
        return f"{tool_name}:{_args_hash(args)}"

    def _is_idempotent(self, tool_name: str) -> bool:
        if tool_name in self.config.mutating_tools:
            return False
        return tool_name in self.config.idempotent_tools

    def before_call(self, tool_name: str, args: Mapping[str, Any] | None) -> ToolGuardrailDecision:
        """Check before executing a tool. Returns block decision if a hard-stop threshold is met."""
        if not self.config.hard_stop_enabled or self._halt_decision:
            return ToolGuardrailDecision(tool_name=tool_name)

        sig_key = self._signature_key(tool_name, args)

        # Exact-failure block check
        exact_count = self._exact_failure_counts.get(sig_key, 0)
        if exact_count >= self.config.exact_failure_block_after:
            decision = ToolGuardrailDecision(
                action="block", code="repeated_exact_failure_block",
                message=(
                    f"Blocked {tool_name}: the same call failed {exact_count} times. "
                    "Stop retrying it unchanged; change strategy or explain the blocker."
                ),
                tool_name=tool_name, count=exact_count,
            )
            self._halt_decision = decision
            return decision

        # No-progress block check (idempotent only)
        if self._is_idempotent(tool_name):
            record = self._no_progress.get(sig_key)
            if record is not None and record[1] >= self.config.no_progress_block_after:
                decision = ToolGuardrailDecision(
                    action="block", code="no_progress_block",
                    message=(
                        f"Blocked {tool_name}: returned the same result {record[1]} times. "
                        "Use the result already provided or try a different query."
                    ),
                    tool_name=tool_name, count=record[1],
                )
                self._halt_decision = decision
                return decision

        return ToolGuardrailDecision(tool_name=tool_name)

    def after_call(
        self,
        tool_name: str,
        args: Mapping[str, Any] | None,
        result_content: str | None,
    ) -> ToolGuardrailDecision:
        """Record the outcome and return a warn/halt decision if a threshold is crossed."""
        if self._halt_decision:
            return self._halt_decision

        sig_key = self._signature_key(tool_name, args)
        failed = _is_failure(result_content)

        if failed:
            # Exact-failure tracking
            exact_count = self._exact_failure_counts.get(sig_key, 0) + 1
            self._exact_failure_counts[sig_key] = exact_count
            self._no_progress.pop(sig_key, None)

            # Same-tool-failure tracking (different args, same tool)
            same_count = self._same_tool_failure_counts.get(tool_name, 0) + 1
            self._same_tool_failure_counts[tool_name] = same_count

            # Hard stop: same tool failing too many times regardless of args
            if self.config.hard_stop_enabled and same_count >= self.config.same_tool_failure_halt_after:
                decision = ToolGuardrailDecision(
                    action="halt", code="same_tool_failure_halt",
                    message=(
                        f"Stopped {tool_name}: it failed {same_count} times this turn. "
                        "Choose a different approach."
                    ),
                    tool_name=tool_name, count=same_count,
                )
                self._halt_decision = decision
                return decision

            # Warning: exact failure repeated
            if self.config.warnings_enabled and exact_count >= self.config.exact_failure_warn_after:
                return ToolGuardrailDecision(
                    action="warn", code="repeated_exact_failure_warning",
                    message=(
                        f"{tool_name} failed {exact_count} times with identical arguments. "
                        "Inspect the error and change strategy."
                    ),
                    tool_name=tool_name, count=exact_count,
                )

            # Warning: same tool failing with different args
            if self.config.warnings_enabled and same_count >= self.config.same_tool_failure_warn_after:
                return ToolGuardrailDecision(
                    action="warn", code="same_tool_failure_warning",
                    message=(
                        f"{tool_name} failed {same_count} times this turn with different arguments. "
                        "Diagnose the root cause before retrying."
                    ),
                    tool_name=tool_name, count=same_count,
                )

            return ToolGuardrailDecision(tool_name=tool_name, count=exact_count)

        # Success — reset failure counters for this signature
        self._exact_failure_counts.pop(sig_key, None)
        self._same_tool_failure_counts.pop(tool_name, None)

        # No-progress tracking (idempotent only)
        if not self._is_idempotent(tool_name):
            self._no_progress.pop(sig_key, None)
            return ToolGuardrailDecision(tool_name=tool_name)

        r_hash = _result_hash(result_content)
        previous = self._no_progress.get(sig_key)
        repeat_count = 1
        if previous is not None and previous[0] == r_hash:
            repeat_count = previous[1] + 1
        self._no_progress[sig_key] = (r_hash, repeat_count)

        if self.config.warnings_enabled and repeat_count >= self.config.no_progress_warn_after:
            return ToolGuardrailDecision(
                action="warn", code="no_progress_warning",
                message=(
                    f"{tool_name} returned the same result {repeat_count} times. "
                    "Use the result already provided or change the query."
                ),
                tool_name=tool_name, count=repeat_count,
            )

        return ToolGuardrailDecision(tool_name=tool_name, count=repeat_count)


def synthetic_blocked_result(decision: ToolGuardrailDecision) -> str:
    """Build a JSON tool-result string for a blocked call."""
    return json.dumps({
        "success": False,
        "error": decision.message,
        "guardrail": decision.to_metadata(),
    }, ensure_ascii=False)
```

**Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_tool_loop_guardrails.py -v`
Expected: PASS (6 tests)

**Step 5: Commit**

```bash
git add app/services/tool_loop_guardrails.py tests/test_tool_loop_guardrails.py
git commit -m "feat: add per-turn tool-call loop guardrail controller (P0.1)"
```

---

## Task 2: Wire guardrail controller into the 3 turn loops

**Files:**
- Modify: `backend/app/routers/agents.py` (3 loop sites: ~2197, ~3208, ~4562)

**Approach:** At the top of each `for iteration in range(...)` loop body, instantiate a `ToolLoopGuardController`. After each tool execution batch, call `ctrl.after_call()` for each tool. If any decision `should_halt`, inject a nudge and break. If `before_call` returns a block, inject a synthetic result instead of executing.

The wiring is identical at all 3 sites. For each site:
1. Import `ToolLoopGuardController` at top of file.
2. Instantiate `guard_ctrl = ToolLoopGuardController()` before the `for` loop.
3. Inside the tool-execution block, for each parsed call: call `guard_ctrl.before_call(name, args)`. If `not decision.allows_execution`, use `synthetic_blocked_result(decision)` as the result instead of calling `execute_tool`.
4. After getting each result, call `guard_ctrl.after_call(name, args, result_json_str)`. If `decision.should_halt`, set a flag to break after the batch.
5. After the batch, if halt flag is set, inject the nudge and break.

**Testing:** The existing `test_loop_guard_*.py` tests must still pass (they test the history-scan `_detect_tool_call_loop`, which is unchanged). Add a new integration test verifying the controller halts a same-tool-failure loop.

**Commit:**

```bash
git add app/routers/agents.py
git commit -m "feat: wire per-turn guardrail controller into all 3 turn loops (P0.1)"
```

---

## Task 3: Create `iteration_budget.py` — per-conversation iteration budget

**Files:**
- Create: `backend/app/services/iteration_budget.py`
- Test: `backend/tests/test_iteration_budget.py`

**Step 1: Write the failing test**

```python
# tests/test_iteration_budget.py
"""Tests for the per-conversation iteration budget."""
import os
import sys
import threading

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.iteration_budget import IterationBudget


def test_consume_until_exhausted():
    budget = IterationBudget(max_total=3)
    assert budget.consume() is True
    assert budget.consume() is True
    assert budget.consume() is True
    assert budget.consume() is False  # exhausted
    assert budget.remaining == 0


def test_refund_restores_one():
    budget = IterationBudget(max_total=2)
    budget.consume()
    budget.consume()
    assert budget.remaining == 0
    budget.refund()
    assert budget.remaining == 1
    assert budget.consume() is True


def test_refund_below_zero_clamped():
    budget = IterationBudget(max_total=1)
    budget.refund()  # no-op, used is 0
    assert budget.used == 0


def test_thread_safe_concurrent_consume():
    budget = IterationBudget(max_total=1000)
    consumed = []
    def worker():
        while budget.consume():
            consumed.append(1)
    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(consumed) == 1000  # exactly max_total, no over-consume


def test_used_and_remaining():
    budget = IterationBudget(max_total=10)
    budget.consume()
    budget.consume()
    assert budget.used == 2
    assert budget.remaining == 8
```

**Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_iteration_budget.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write minimal implementation**

```python
# app/services/iteration_budget.py
"""Per-conversation iteration budget — thread-safe consume/refund counter.

Bounds the TOTAL number of tool-loop iterations across all turns in a
conversation (including resumed conversations after approval pauses).
This complements the per-turn ``MAX_TOOL_ITERATIONS`` cap: a single turn
is bounded by the for-loop range, while the conversation-level budget
prevents a long multi-turn session from running indefinitely.

``execute_code`` iterations are refunded via :meth:`refund` so
programmatic tool-calling doesn't eat the budget.

Inspired by Hermes' ``agent/iteration_budget.py``.
"""
from __future__ import annotations

import threading


class IterationBudget:
    """Thread-safe iteration counter for a conversation.

    Args:
        max_total: Maximum total iterations allowed.
    """

    def __init__(self, max_total: int):
        self.max_total = max_total
        self._used = 0
        self._lock = threading.Lock()

    def consume(self) -> bool:
        """Try to consume one iteration. Returns True if allowed."""
        with self._lock:
            if self._used >= self.max_total:
                return False
            self._used += 1
            return True

    def refund(self) -> None:
        """Give back one iteration (e.g. for execute_code turns)."""
        with self._lock:
            if self._used > 0:
                self._used -= 1

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.max_total - self._used)


__all__ = ["IterationBudget"]
```

**Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_iteration_budget.py -v`
Expected: PASS (5 tests)

**Step 5: Commit**

```bash
git add app/services/iteration_budget.py tests/test_iteration_budget.py
git commit -m "feat: add per-conversation iteration budget (P0.2)"
```

---

## Task 4: Wire iteration budget into turn loops + config

**Files:**
- Modify: `backend/app/config.py` — add `AGENT_MAX_ITERATIONS` setting
- Modify: `backend/app/routers/agents.py` — instantiate budget per conversation, consume per iteration, refund for execute_code

**Step 1: Add config setting**

In `config.py`, after the `DELEGATE_MAX_ITERATIONS` line (~113):

```python
    # Per-conversation total iteration budget across all turns (including
    # resumes). Bounds runaway multi-turn sessions. Per-agent overrides via
    # AgentApp.max_call_count take precedence when set.
    AGENT_MAX_ITERATIONS: int = 50
```

**Step 2: Wire into turn loops**

At each of the 3 loop sites:
1. Before the `for` loop, create the budget:
   ```python
   from app.services.iteration_budget import IterationBudget
   max_iters = getattr(agent_app, "max_call_count", None) or settings.AGENT_MAX_ITERATIONS
   conv_budget = IterationBudget(max_total=max_iters)
   ```
2. At the top of each iteration body: `if not conv_budget.consume(): break`
3. After tool execution, if the only tool called was `execute_code` and it succeeded: `conv_budget.refund()`

**Step 3: Commit**

```bash
git add app/config.py app/routers/agents.py
git commit -m "feat: wire iteration budget into turn loops + config (P0.2)"
```

---

## Task 5: Create `tool_result_persistence.py` — 3-layer tool result system

**Files:**
- Create: `backend/app/services/tool_result_persistence.py`
- Test: `backend/tests/test_tool_result_persistence.py`

**Step 1: Write the failing test**

```python
# tests/test_tool_result_persistence.py
"""Tests for the 3-layer tool result persistence system.

Layer 1: per-tool cap (truncation) — already exists in tool_security.py.
Layer 2: per-result persistence to disk + preview replacement.
Layer 3: per-turn aggregate budget (spill largest results to disk).
"""
import json
import os
import sys
import tempfile

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.tool_result_persistence import (
    PersistenceConfig,
    persist_tool_result,
    apply_turn_budget,
)


def test_small_result_not_persisted():
    """Results under the per-result threshold stay inline — no disk write."""
    config = PersistenceConfig(result_threshold_chars=1000, preview_chars=200)
    with tempfile.TemporaryDirectory() as tmpdir:
        result_str = json.dumps({"success": True, "content": "small result"})
        new_str, meta = persist_tool_result(
            "read_file", result_str, tmpdir, config
        )
        assert new_str == result_str  # unchanged
        assert meta["persisted"] is False


def test_large_result_persisted_with_preview():
    """Results over the threshold get written to disk; inline preview replaces them."""
    config = PersistenceConfig(result_threshold_chars=100, preview_chars=50)
    with tempfile.TemporaryDirectory() as tmpdir:
        big_content = "X" * 500
        result_str = json.dumps({"success": True, "content": big_content})
        new_str, meta = persist_tool_result(
            "read_file", result_str, tmpdir, config
        )
        assert meta["persisted"] is True
        assert "stored_path" in meta
        assert os.path.exists(meta["stored_path"])
        assert len(new_str) < len(result_str)  # preview is smaller
        assert "read_file" in new_str or "stored_path" in new_str  # tells LLM where to find full


def test_read_file_never_persisted():
    """read_file results are never persisted (prevents persist->read->persist loops)."""
    config = PersistenceConfig(result_threshold_chars=10, preview_chars=5)
    with tempfile.TemporaryDirectory() as tmpdir:
        result_str = json.dumps({"success": True, "content": "X" * 500})
        new_str, meta = persist_tool_result(
            "read_file", result_str, tmpdir, config
        )
        assert meta["persisted"] is False
        assert new_str == result_str


def test_turn_budget_spills_largest_results():
    """When total turn output exceeds the budget, largest results get persisted."""
    config = PersistenceConfig(
        result_threshold_chars=10000,  # high so Layer 2 doesn't fire first
        preview_chars=200,
        turn_budget_chars=500,  # low to trigger Layer 3
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        results = [
            ("web_search", json.dumps({"success": True, "content": "A" * 400})),
            ("web_extract", json.dumps({"success": True, "content": "B" * 300})),
            ("read_file", json.dumps({"success": True, "content": "C" * 200})),
        ]
        spilled = apply_turn_budget(results, tmpdir, config)
        # Total = 900 chars, budget = 500, so at least the largest should be spilled
        assert len(spilled) >= 1
        total_inline = sum(len(r) for _, r in spilled)
        assert total_inline <= config.turn_budget_chars + 200  # some tolerance for previews
```

**Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_tool_result_persistence.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write minimal implementation**

```python
# app/services/tool_result_persistence.py
"""3-layer tool result persistence for context overflow protection.

Layer 1: per-tool cap (truncation) — handled by tool_security.truncate_output.
Layer 2: per-result persistence — write large results to disk, replace inline
         with a preview + a pointer the LLM can use to read the full result.
Layer 3: per-turn aggregate budget — if the total output of all tools in a
         single turn exceeds the budget, spill the largest results to disk.

Inspired by Hermes' ``tools/tool_result_storage.py`` + ``tools/budget_config.py``,
adapted for Zhanlu's file-based persistence.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Sequence

logger = logging.getLogger(__name__)

# Tools whose results must NEVER be persisted to disk. read_file is the
# critical one: persisting its output creates a read->persist->read loop
# where the LLM reads the persisted file, which gets persisted again.
PINNED_NO_PERSIST: frozenset[str] = frozenset({"read_file"})

# Default budget values (chars). Scaled to context window in budget_for_context.
DEFAULT_RESULT_THRESHOLD: int = 20_000
DEFAULT_TURN_BUDGET: int = 80_000
DEFAULT_PREVIEW_CHARS: int = 1_500

# Floor so tiny models still get usable previews.
_MIN_RESULT_THRESHOLD: int = 4_000
_MIN_TURN_BUDGET: int = 16_000

# Token<->char ratio (conservative, matches token_estimator.py).
_CHARS_PER_TOKEN: int = 4


@dataclass(frozen=True)
class PersistenceConfig:
    """Budget constants for the 3-layer persistence system."""
    result_threshold_chars: int = DEFAULT_RESULT_THRESHOLD
    turn_budget_chars: int = DEFAULT_TURN_BUDGET
    preview_chars: int = DEFAULT_PREVIEW_CHARS
    no_persist_tools: frozenset[str] = frozenset(PINNED_NO_PERSIST)


def budget_for_context_window(context_length: int | None) -> PersistenceConfig:
    """Return a PersistenceConfig scaled to the model's context window.

    Large models (200K+ tokens) get the default budget. Smaller models get
    proportionally smaller budgets, floored so previews remain usable.
    """
    if not context_length or context_length <= 0:
        return PersistenceConfig()

    window_chars = context_length * _CHARS_PER_TOKEN
    per_result = int(window_chars * 0.10)   # 10% of window per single result
    per_turn = int(window_chars * 0.25)     # 25% of window per turn total

    per_result = max(_MIN_RESULT_THRESHOLD, min(per_result, DEFAULT_RESULT_THRESHOLD))
    per_turn = max(_MIN_TURN_BUDGET, min(per_turn, DEFAULT_TURN_BUDGET))

    return PersistenceConfig(
        result_threshold_chars=per_result,
        turn_budget_chars=per_turn,
        preview_chars=DEFAULT_PREVIEW_CHARS,
    )


def persist_tool_result(
    tool_name: str,
    result_str: str,
    storage_dir: str,
    config: PersistenceConfig | None = None,
    conversation_id: str | None = None,
) -> tuple[str, dict]:
    """Layer 2: persist a large tool result to disk, return a preview string.

    If the result is under the threshold, or the tool is in the no-persist
    set, the original string is returned unchanged.

    Returns:
        (new_result_str, metadata) where metadata has keys:
        - persisted: bool
        - stored_path: str (only if persisted)
        - original_size: int
    """
    config = config or PersistenceConfig()
    metadata: dict = {"persisted": False, "original_size": len(result_str)}

    if tool_name in config.no_persist_tools:
        return result_str, metadata

    if len(result_str) <= config.result_threshold_chars:
        return result_str, metadata

    # Write to disk
    os.makedirs(storage_dir, exist_ok=True)
    file_id = uuid.uuid4().hex[:12]
    conv_prefix = (conversation_id or "conv")[:8]
    filename = f"toolresult_{conv_prefix}_{tool_name}_{file_id}.json"
    filepath = os.path.join(storage_dir, filename)

    try:
        with open(filepath, "w", encoding="utf-8", surrogatepass") as f:
            f.write(result_str)
    except OSError as e:
        logger.warning("Failed to persist tool result for %s: %s", tool_name, e)
        return result_str, metadata

    # Build preview: first N chars + pointer to full result
    preview = result_str[:config.preview_chars]
    pointer = (
        f"\n\n[Full result ({len(result_str)} chars) stored at: {filepath}]\n"
        f"[Use read_file with path '{filepath}' to retrieve the complete output.]"
    )
    new_str = preview + "...[truncated]\n" + pointer

    metadata["persisted"] = True
    metadata["stored_path"] = filepath
    logger.info(
        "Persisted tool result for '%s': %d chars -> disk (%s), inline preview %d chars",
        tool_name, len(result_str), filepath, len(new_str),
    )
    return new_str, metadata


def apply_turn_budget(
    results: Sequence[tuple[str, str]],
    storage_dir: str,
    config: PersistenceConfig | None = None,
    conversation_id: str | None = None,
) -> list[tuple[str, str]]:
    """Layer 3: if total turn output exceeds budget, spill largest results to disk.

    Args:
        results: List of (tool_name, result_str) tuples for one turn.
        storage_dir: Directory to write spilled results.
        config: Persistence config.
        conversation_id: Optional conversation ID for filename.

    Returns:
        List of (tool_name, new_result_str) tuples, where some may have been
        replaced with previews.
    """
    config = config or PersistenceConfig()

    total_chars = sum(len(r) for _, r in results)
    if total_chars <= config.turn_budget_chars:
        return list(results)

    # Sort by size descending — spill largest first
    indexed = list(enumerate(results))
    indexed.sort(key=lambda x: len(x[1][1]), reverse=True)

    output: dict[int, tuple[str, str]] = {}
    current_total = total_chars
    spilled = set()

    for idx, (tool_name, result_str) in indexed:
        if current_total <= config.turn_budget_chars:
            output[idx] = (tool_name, result_str)
            continue
        # Don't spill no-persist tools (read_file) — they'd loop
        if tool_name in config.no_persist_tools:
            output[idx] = (tool_name, result_str)
            continue
        new_str, meta = persist_tool_result(
            tool_name, result_str, storage_dir, config, conversation_id
        )
        if meta["persisted"]:
            current_total -= len(result_str) - len(new_str)
            spilled.add(idx)
        output[idx] = (tool_name, new_str)

    result_list = [output[i] for i in range(len(results))]
    if spilled:
        logger.info(
            "Turn budget spill: %d/%d results persisted to disk (total %d -> %d chars)",
            len(spilled), len(results), total_chars, sum(len(r) for _, r in result_list),
        )
    return result_list
```

**Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_tool_result_persistence.py -v`
Expected: PASS (4 tests)

**Step 5: Commit**

```bash
git add app/services/tool_result_persistence.py tests/test_tool_result_persistence.py
git commit -m "feat: add 3-layer tool result persistence system (P0.3)"
```

---

## Task 6: Wire result persistence into turn loops

**Files:**
- Modify: `backend/app/routers/agents.py` (3 loop sites)
- Modify: `backend/app/config.py` — add storage dir setting

**Step 1: Add config setting**

In `config.py`:
```python
    # Directory for persisted tool results (Layer 2/3 of result persistence)
    TOOL_RESULT_STORAGE_DIR: str = "tool_results"
```

**Step 2: Wire into turn loops**

At each loop site, after tool results are collected (after `asyncio.gather` / single execute), apply:
1. Layer 2: `persist_tool_result()` for each result over the threshold.
2. Layer 3: `apply_turn_budget()` if total turn output exceeds budget.

The budget config is resolved from the model's context window if available, else defaults.

**Step 3: Commit**

```bash
git add app/config.py app/routers/agents.py
git commit -m "feat: wire 3-layer tool result persistence into turn loops (P0.3)"
```

---

## Task 7: Run full test suite + fix regressions

Run: `cd /root/zhanlu/backend && python -m pytest tests/ -x -q --timeout=120`
Expected: All existing tests pass + new tests pass.

Commit any regression fixes.

---

## Summary

| Task | Capability | New Module | New Tests |
|------|-----------|------------|-----------|
| 1-2 | Tool-call loop guardrails | `tool_loop_guardrails.py` | `test_tool_loop_guardrails.py` |
| 3-4 | Iteration budget | `iteration_budget.py` | `test_iteration_budget.py` |
| 5-6 | Tool result persistence | `tool_result_persistence.py` | `test_tool_result_persistence.py` |
| 7 | Regression verification | — | — |
