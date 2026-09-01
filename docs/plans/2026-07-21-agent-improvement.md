# Agent Improvement Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Deepen sub-agent prompts, wire swarm/OHMO subsystems, add tool retry/self-healing, and activate the planning layer across all non-data agents.

**Architecture:** The Zhanlu backend has 6 builtin sub-agents (general-purpose, explore, plan, worker, verification, data_agent) and 3 system meta-agents (agent_builder, skill_agent, automation_agent). The swarm coordinator and OHMO workspace exist as stubs. The tool execution path (`execute_tool`) has no retry logic. The `SynexiaFSM` planning layer exists but is never called from the chat loop.

**Tech Stack:** Python, FastAPI, SQLAlchemy, asyncio, pytest

---

## Task 1: Deepen Sub-Agent Prompts — `general-purpose`

**Files:**
- Modify: `backend/app/services/agent_definitions/__init__.py:79-97`

**Step 1: Write the failing test**

```python
# backend/tests/test_agent_definitions_prompts.py
def test_general_use_prompt_has_anti_hallucination():
    from app.services.agent_definitions import GENERAL_PURPOSE_PROMPT
    assert "NO HALLUCINATION" in GENERAL_PURPOSE_PROMPT
    assert "FILE-FORMAT INTENT" in GENERAL_PURPOSE_PROMPT

def test_general_use_prompt_has_tool_guidelines():
    from app.services.agent_definitions import GENERAL_PURPOSE_PROMPT
    assert "TOOL USAGE GUIDELINES" in GENERAL_PURPOSE_PROMPT
    assert "web_search" in GENERAL_PURPOSE_PROMPT
    assert "execute_code" in GENERAL_PURPOSE_PROMPT
```

**Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_agent_definitions_prompts.py::test_general_use_prompt_has_anti_hallucination -v`
Expected: FAIL with "assert 'NO HALLUCINATION' in GENERAL_PURPOSE_PROMPT"

**Step 3: Rewrite GENERAL_PURPOSE_PROMPT**

Replace the current 20-line prompt with a comprehensive prompt that includes:
- Identity and operating principles (keep existing)
- `NO HALLUCINATION` block (strict anti-hallucination rule with tool-first mandate)
- `TOOL USAGE GUIDELINES` section listing all available tools with usage notes
- `FILE-FORMAT INTENT` block (docx/pptx/xlsx/pdf/md detection and run_sandbox_skill flow)
- `AUTONOMY CONTRACT` (never ask caller to install packages or export CSVs)
- Error recovery guidance (tool failure → classify → retry or escalate)
- Response format (keep existing + add citation requirement)

**Step 4: Run test to verify it passes**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_agent_definitions_prompts.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/tests/test_agent_definitions_prompts.py backend/app/services/agent_definitions/__init__.py
git commit -m "feat: deepen general-purpose agent prompt with anti-hallucination + tool guidelines"
```

---

## Task 2: Deepen Sub-Agent Prompts — `explore`

**Files:**
- Modify: `backend/app/services/agent_definitions/__init__.py:99-111`

**Step 1: Write the failing test**

```python
def test_explore_prompt_has_search_strategy():
    from app.services.agent_definitions import EXPLORE_PROMPT
    assert "SEARCH STRATEGY" in EXPLORE_PROMPT
    assert "entry point" in EXPLORE_PROMPT.lower()

def test_explore_prompt_has_output_structure():
    from app.services.agent_definitions import EXPLORE_PROMPT
    assert "ARCHITECTURE MAPPING" in EXPLORE_PROMPT
    assert "dependency" in EXPLORE_PROMPT.lower()
```

**Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_agent_definitions_prompts.py::test_explore_prompt_has_search_strategy -v`
Expected: FAIL

**Step 3: Rewrite EXPLORE_PROMPT**

Replace with a comprehensive prompt including:
- Identity (read-only exploration agent)
- `SEARCH STRATEGY` block: entry points → imports → call chains, breadth-first then depth-first
- `ARCHITECTURE MAPPING` output format: components, data flow, dependency graph
- `OUTPUT FORMAT` section: structured findings with file:line citations, severity classification
- `CONSTRAINTS` (keep read-only, add "never modify any file or record")
- Dependency analysis rules (trace imports, identify circular deps, flag dead code)

**Step 4: Run test to verify it passes**

**Step 5: Commit**

```bash
git add backend/tests/test_agent_definitions_prompts.py backend/app/services/agent_definitions/__init__.py
git commit -m "feat: deepen explore agent prompt with search strategy + architecture mapping"
```

---

## Task 3: Deepen Sub-Agent Prompts — `plan`

**Files:**
- Modify: `backend/app/services/agent_definitions/__init__.py:113-126`

**Step 1: Write the failing test**

```python
def test_plan_prompt_has_risk_framework():
    from app.services.agent_definitions import PLAN_PROMPT
    assert "RISK" in PLAN_PROMPT
    assert "ESTIMATION" in PLAN_PROMPT or "effort" in PLAN_PROMPT.lower()

def test_plan_prompt_has_dependency_analysis():
    from app.services.agent_definitions import PLAN_PROMPT
    assert "dependency" in PLAN_PROMPT.lower()
    assert "alternative" in PLAN_PROMPT.lower()
```

**Step 2: Run test to verify it fails**

**Step 3: Rewrite PLAN_PROMPT**

Replace with a comprehensive prompt including:
- Identity (planning agent, PLAN MODE, no writes)
- `RISK ASSESSMENT` matrix: impact (high/medium/low) × likelihood (high/medium/low)
- `ESTIMATION` framework: S (< 2h), M (2-8h), L (1-3d), XL (3d+)
- `DEPENDENCY ANALYSIS` block: identify blocking vs non-blocking deps, critical path
- `ALTERNATIVE COMPARISON` framework: pros/cons table for each alternative
- `OUTPUT FORMAT`: recommended approach → rationale → step-by-step plan → risks → alternatives

**Step 4: Run test to verify it passes**

**Step 5: Commit**

```bash
git add backend/tests/test_agent_definitions_prompts.py backend/app/services/agent_definitions/__init__.py
git commit -m "feat: deepen plan agent prompt with risk framework + estimation"
```

---

## Task 4: Deepen Sub-Agent Prompts — `worker`

**Files:**
- Modify: `backend/app/services/agent_definitions/__init__.py:128-145`

**Step 1: Write the failing test**

```python
def test_worker_prompt_has_verification_step():
    from app.services.agent_definitions import WORKER_PROMPT
    assert "VERIFY" in WORKER_PROMPT.upper() or "verification" in WORKER_PROMPT.lower()
    assert "confirm" in WORKER_PROMPT.lower()

def test_worker_prompt_has_error_classification():
    from app.services.agent_definitions import WORKER_PROMPT
    assert "transient" in WORKER_PROMPT.lower() or "retry" in WORKER_PROMPT.lower()
```

**Step 2: Run test to verify it fails**

**Step 3: Rewrite WORKER_PROMPT**

Replace with a comprehensive prompt including:
- Identity (implementation agent)
- `OPERATING PRINCIPLES` (keep existing, expand with autonomy contract)
- `VERIFICATION STEP` block: after each write operation, confirm the record/file exists
- `ERROR CLASSIFICATION` block: transient (retry with backoff) vs permanent (escalate)
- `ROLLBACK GUIDANCE`: how to undo partial changes on failure
- `RESPONSE FORMAT` (keep existing)

**Step 4: Run test to verify it passes**

**Step 5: Commit**

```bash
git add backend/tests/test_agent_definitions_prompts.py backend/app/services/agent_definitions/__init__.py
git commit -m "feat: deepen worker agent prompt with verification + error classification"
```

---

## Task 5: Deepen Sub-Agent Prompts — `verification`

**Files:**
- Modify: `backend/app/services/agent_definitions/__init__.py:147-159`

**Step 1: Write the failing test**

```python
def test_verification_prompt_has_severity_levels():
    from app.services.agent_definitions import VERIFICATION_PROMPT
    assert "CRITICAL" in VERIFICATION_PROMPT
    assert "MAJOR" in VERIFICATION_PROMPT
    assert "MINOR" in VERIFICATION_PROMPT

def test_verification_prompt_has_evidence_standard():
    from app.services.agent_definitions import VERIFICATION_PROMPT
    assert "file:" in VERIFICATION_PROMPT.lower() or "citation" in VERIFICATION_PROMPT.lower()
```

**Step 2: Run test to verify it fails**

**Step 3: Rewrite VERIFICATION_PROMPT**

Replace with a comprehensive prompt including:
- Identity (verification agent, READ-ONLY)
- `SEVERITY CLASSIFICATION` block: CRITICAL (blocks deployment), MAJOR (degrades function), MINOR (cosmetic)
- `EVIDENCE STANDARD`: every finding must cite file:line, tool output, or specific record
- `STRUCTURED CHECKLIST` format: for each verification area, PASS/FAIL/PARTIAL with evidence
- `COVERAGE CRITERIA`: verify all claimed work, check edge cases, validate error handling
- `OUTPUT FORMAT` (keep PASS/FAIL/PARTIAL, add severity + evidence requirements)

**Step 4: Run test to verify it passes**

**Step 5: Commit**

```bash
git add backend/tests/test_agent_definitions_prompts.py backend/app/services/agent_definitions/__init__.py
git commit -m "feat: deepen verification agent prompt with severity levels + evidence standard"
```

---

## Task 6: Tool Retry & Self-Healing — Retry Wrapper

**Files:**
- Modify: `backend/app/services/agent_tools.py:77-229`
- Create: `backend/app/services/tool_retry.py`

**Step 1: Write the failing test**

```python
# backend/tests/test_tool_retry.py
import asyncio
import pytest
from app.services.tool_retry import retry_with_backoff

@pytest.mark.asyncio
async def test_retry_succeeds_on_third_attempt():
    attempts = 0
    async def flaky_handler(args, db, user_id, context=None):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionError("transient")
        return {"success": True, "data": "ok"}
    
    result = await retry_with_backoff(flaky_handler, {}, None, None, max_retries=3)
    assert result["success"] is True
    assert attempts == 3

@pytest.mark.asyncio
async def test_retry_exhausts_and_returns_error():
    async def always_fails(args, db, user_id, context=None):
        raise ValueError("permanent")
    
    result = await retry_with_backoff(always_fails, {}, None, None, max_retries=2)
    assert result["success"] is False
    assert "permanent" in result["error"]
```

**Step 2: Run test to verify it fails**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_tool_retry.py -v`
Expected: FAIL with "No module named 'app.services.tool_retry'"

**Step 3: Create tool_retry.py**

Create `backend/app/services/tool_retry.py` with:
- `RETRYABLE_ERRORS` frozenset: `ConnectionError`, `TimeoutError`, `asyncio.TimeoutError`, `OSError` (network-related)
- `async def retry_with_backoff(handler, arguments, db, user_id, context, max_retries=3, base_delay=1.0)` — exponential backoff wrapper
- `def is_retryable(error: Exception) -> bool` — classify errors as transient vs permanent
- `async def reformulate_tool_args(tool_name, arguments, error, llm_fn)` — ask LLM to fix arguments on failure (future enhancement, stub for now)

**Step 4: Integrate retry into execute_tool**

In `backend/app/services/agent_tools.py`, wrap the tool dispatch (lines 194-216) with `retry_with_backoff`. Only retry on `RETRYABLE_ERRORS`. Non-retryable errors return immediately.

**Step 5: Run test to verify it passes**

Run: `cd /root/zhanlu/backend && python -m pytest tests/test_tool_retry.py -v`
Expected: PASS

**Step 6: Commit**

```bash
git add backend/app/services/tool_retry.py backend/app/services/agent_tools.py backend/tests/test_tool_retry.py
git commit -m "feat: add tool retry with exponential backoff for transient errors"
```

---

## Task 7: Tool Retry & Self-Healing — Success-Aware Loop Guard

**Files:**
- Modify: `backend/app/routers/agents.py:80-150`

**Step 1: Write the failing test**

```python
# backend/tests/test_loop_guard_success_aware.py
def test_loop_guard_counts_only_failures():
    """The loop guard should only count failed tool calls toward the cap,
    not successful ones. A successful call resets the failure counter."""
    from app.routers.agents import _get_tool_call_cap
    # After 2 successful calls, the cap should still be available
    # After 2 failed calls, the cap should be exhausted
    caps = _get_tool_call_cap()
    assert "ask_data_agent" in caps
    # The key assertion: success doesn't decrement the cap
```

**Step 2: Run test to verify it fails**

**Step 3: Modify the loop guard logic**

In `agents.py`, find the loop guard that tracks tool call counts. Change from cardinality-only (count all calls) to success-aware:
- Only failed calls (`success=False`) count toward the cap
- Successful calls reset the failure counter for that tool
- The cap is `settings.TOOL_CAP_ASK_DATA` (default 2) for failures only

**Step 4: Run test to verify it passes**

**Step 5: Commit**

```bash
git add backend/routers/agents.py backend/tests/test_loop_guard_success_aware.py
git commit -m "feat: make loop guard success-aware — only failed calls count toward cap"
```

---

## Task 8: Swarm Coordinator — Real Runtime Integration

**Files:**
- Modify: `backend/app/services/swarm/__init__.py:166-219`
- Create: `backend/app/services/swarm/runtime.py`

**Step 1: Write the failing test**

```python
# backend/tests/test_swarm_runtime.py
import pytest
from app.services.swarm.runtime import SwarmRuntime

@pytest.mark.asyncio
async def test_spawn_agent_with_tool_access():
    """Spawned agents should have access to the real tool-calling loop."""
    runtime = SwarmRuntime()
    member = await runtime.spawn_agent(
        team_id="test-team",
        agent_name="general-purpose",
        task="What is 2+2?",
    )
    assert member is not None
    # The agent should use execute_code tool, not just call_llm
```

**Step 2: Run test to verify it fails**

**Step 3: Create swarm/runtime.py**

Create `backend/app/services/swarm/runtime.py` with:
- `SwarmRuntime` class that wraps `SwarmCoordinator`
- `spawn_agent()` creates an `AgentConversation` in the DB, calls the real `add_message_stream` tool loop
- `_run_agent()` uses the full agent loop (system prompt, tool calling, memory injection) instead of `call_llm(prompt=task)`
- Agent definitions are resolved from `AgentDefinitionLoader`
- Tool access is granted based on the agent definition's `tools` list

**Step 4: Modify swarm/__init__.py**

Update `SwarmCoordinator._run_agent` to delegate to `SwarmRuntime._run_agent_with_tools()`.

**Step 5: Run test to verify it passes**

**Step 6: Commit**

```bash
git add backend/app/services/swarm/runtime.py backend/app/services/swarm/__init__.py backend/tests/test_swarm_runtime.py
git commit -m "feat: wire swarm coordinator into real tool-calling runtime"
```

---

## Task 9: Swarm Coordinator — Persistent Mailbox

**Files:**
- Create: `backend/app/models/swarm_mailbox.py`
- Modify: `backend/app/services/swarm/__init__.py`

**Step 1: Write the failing test**

```python
# backend/tests/test_swarm_mailbox.py
def test_mailbox_persists_across_instances():
    """Messages should survive SwarmCoordinator restarts."""
    from app.services.swarm import TeamRegistry
    registry1 = TeamRegistry()
    team = registry1.create_team("test")
    registry1.add_member(team.id, "alice")
    registry1.send_message(team.id, "alice", "main", "hello")
    
    # Simulate restart — new registry instance should see the message
    registry2 = TeamRegistry()
    messages = registry2.get_messages(team.id, "main")
    assert len(messages) == 1
    assert messages[0].content == "hello"
```

**Step 2: Run test to verify it fails**

**Step 3: Create swarm_mailbox model**

Create `backend/app/models/swarm_mailbox.py`:
- `SwarmMailboxMessage` model: id, team_id, sender, recipient, content, summary, timestamp, read
- Uses SQLAlchemy with the existing database session

**Step 4: Modify TeamRegistry to use DB-backed mailbox**

Replace the in-memory `mailbox: list[MailboxMessage]` on `TeamMember` with DB-backed storage. Keep the same API surface.

**Step 5: Run test to verify it passes**

**Step 6: Commit**

```bash
git add backend/app/models/swarm_mailbox.py backend/app/services/swarm/__init__.py backend/tests/test_swarm_mailbox.py
git commit -m "feat: persist swarm mailbox messages to database"
```

---

## Task 10: Swarm Tools — Register as Agent Tools

**Files:**
- Create: `backend/app/services/tool_handlers/swarm_tools.py`
- Modify: `backend/app/services/tool_registry.py`

**Step 1: Write the failing test**

```python
# backend/tests/test_swarm_tools.py
def test_create_team_tool():
    from app.services.tool_handlers.swarm_tools import handle_create_team
    result = handle_create_team({"name": "test-team", "description": "Test"}, None, None)
    assert result["success"] is True
    assert "team_id" in result
```

**Step 2: Run test to verify it fails**

**Step 3: Create swarm_tools.py**

Create `backend/app/services/tool_handlers/swarm_tools.py` with tool handlers:
- `create_team(name, description)` → creates team, returns team_id
- `spawn_agent(team_id, agent_name, task)` → spawns agent via SwarmRuntime
- `send_message(team_id, sender, recipient, content)` → sends mailbox message
- `get_messages(team_id, member_name)` → reads mailbox messages
- `list_teams()` → lists all teams

**Step 4: Register tools in tool_registry.py**

Add the 5 swarm tools to the tool registry so agents can call them in the ReAct loop.

**Step 5: Run test to verify it passes**

**Step 6: Commit**

```bash
git add backend/app/services/tool_handlers/swarm_tools.py backend/app/services/tool_registry.py backend/tests/test_swarm_tools.py
git commit -m "feat: register swarm tools for agent use in ReAct loop"
```

---

## Task 11: OHMO Workspace — Wire into Agent Resolution

**Files:**
- Modify: `backend/app/services/agent_prompts.py` (or wherever `get_system_prompt` lives)

**Step 1: Write the failing test**

```python
# backend/tests/test_ohmo_integration.py
def test_ohmo_workspace_builds_system_prompt():
    from app.services.ohmo import OhmoWorkspace
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        ws = OhmoWorkspace(tmpdir)
        ws.init_workspace()
        prompt = ws.build_system_prompt()
        assert "Agent Soul" in prompt
        assert "Agent Identity" in prompt
        assert "User Profile" in prompt
```

**Step 2: Run test to verify it fails**

**Step 3: Wire OHMO into system prompt resolution**

Find where `get_system_prompt` resolves agent prompts. When an agent has an OHMO workspace configured, prepend `ws.build_system_prompt()` to the system prompt.

**Step 4: Add user profile learning hook**

After each conversation turn, extract user preferences/facts and append to `user.md`. This runs as a post-conversation hook.

**Step 5: Run test to verify it passes**

**Step 6: Commit**

```bash
git add backend/app/services/ohmo/__init__.py backend/app/services/agent_prompts.py backend/tests/test_ohmo_integration.py
git commit -m "feat: wire OHMO workspace into agent system prompt resolution"
```

---

## Task 12: Planning Layer — Wire SynexiaFSM into Chat Loop

**Files:**
- Modify: `backend/app/routers/agents.py:3024-3200` (the `add_message_stream` function)

**Step 1: Write the failing test**

```python
# backend/tests/test_planning_layer.py
def test_complex_request_triggers_planning():
    """Requests that need 3+ tool calls should trigger the planning layer."""
    from app.routers.agents import should_trigger_planning
    assert should_trigger_planning("Create a report, then email it, then schedule a follow-up")
    assert not should_trigger_planning("What is 2+2?")
```

**Step 2: Run test to verify it fails**

**Step 3: Add planning trigger logic**

In `add_message_stream`, before the tool loop starts:
1. Call `should_trigger_planning(user_message)` — heuristic: count action verbs, check for "then"/"and then"/"after that"
2. If triggered, invoke `SynexiaFSM` to generate a plan DAG
3. Save the plan to the conversation metadata
4. Execute plan steps in order, adapting on failure

**Step 4: Add plan persistence**

Store the plan DAG in `AgentConversation.metadata` so users can see what the agent is doing.

**Step 5: Run test to verify it passes**

**Step 6: Commit**

```bash
git add backend/routers/agents.py backend/tests/test_planning_layer.py
git commit -m "feat: wire SynexiaFSM planning layer into chat loop for complex requests"
```

---

## Task 13: Automation Agent — Event-Driven Triggers + Conditional Logic

**Files:**
- Modify: `backend/app/services/agent_prompts.py:459-501`

**Step 1: Write the failing test**

```python
def test_automation_prompt_mentions_event_triggers():
    from app.services.agent_prompts import AUTOMATION_AGENT_SYSTEM_PROMPT
    assert "event" in AUTOMATION_AGENT_SYSTEM_PROMPT.lower()
    assert "webhook" in AUTOMATION_AGENT_SYSTEM_PROMPT.lower()
    assert "condition" in AUTOMATION_AGENT_SYSTEM_PROMPT.lower()
```

**Step 2: Run test to verify it fails**

**Step 3: Expand AUTOMATION_AGENT_SYSTEM_PROMPT**

Add to the prompt:
- `EVENT-DRIVEN TRIGGERS` section: webhook received, record created, threshold crossed
- `CONDITIONAL LOGIC` section: if/then/else branches, filters, comparison operators
- `RETRY POLICY` section: max retries, backoff strategy, dead-letter queue
- `OUTPUT DESTINATIONS` section: send_message, email, webhook callback, file write
- `VALIDATION CHECKLIST` section: verify data source exists, verify action is valid, verify schedule is parseable

**Step 4: Run test to verify it passes**

**Step 5: Commit**

```bash
git add backend/app/services/agent_prompts.py backend/tests/test_agent_definitions_prompts.py
git commit -m "feat: expand automation agent prompt with event triggers + conditional logic"
```

---

## Task 14: Skill Agent — Quality Gates

**Files:**
- Modify: `backend/app/services/agent_prompts.py:388-452`

**Step 1: Write the failing test**

```python
def test_skill_prompt_has_quality_gates():
    from app.services.agent_prompts import SKILL_AGENT_SYSTEM_PROMPT
    assert "QUALITY" in SKILL_AGENT_SYSTEM_PROMPT or "quality" in SKILL_AGENT_SYSTEM_PROMPT
    assert "required sections" in SKILL_AGENT_SYSTEM_PROMPT.lower() or "minimum" in SKILL_AGENT_SYSTEM_PROMPT.lower()
```

**Step 2: Run test to verify it fails**

**Step 3: Expand SKILL_AGENT_SYSTEM_PROMPT**

Add to the prompt:
- `SKILL QUALITY GATES` section: required sections (purpose, trigger conditions, step-by-step instructions, examples), minimum content length
- `SKILL TESTING` section: after creating a skill, invoke it in a dry-run to verify it produces useful guidance
- `SKILL CATEGORIZATION` section: auto-suggest category based on content analysis
- `SKILL VERSIONING` section: track changes, allow rollback

**Step 4: Run test to verify it passes**

**Step 5: Commit**

```bash
git add backend/app/services/agent_prompts.py backend/tests/test_agent_definitions_prompts.py
git commit -m "feat: add skill quality gates + testing + versioning to skill agent prompt"
```

---

## Task 15: Agent Builder — Structured Output Mode

**Files:**
- Modify: `backend/app/services/agent_prompts.py:280-381` (agent_builder section)

**Step 1: Write the failing test**

```python
def test_agent_builder_uses_json_output():
    from app.services.agent_prompts import AGENT_BUILDER_SYSTEM_PROMPT
    assert "json" in AGENT_BUILDER_SYSTEM_PROMPT.lower()
    assert "structured" in AGENT_BUILDER_SYSTEM_PROMPT.lower() or "JSON mode" in AGENT_BUILDER_SYSTEM_PROMPT
```

**Step 2: Run test to verify it fails**

**Step 3: Update AGENT_BUILDER_SYSTEM_PROMPT**

Add to the prompt:
- `STRUCTURED OUTPUT MODE` section: request JSON from the LLM instead of parsing free-text with regex
- `VALIDATION PIPELINE` section: after agent creation, verify the agent's tools exist, model is available, prompt is non-empty
- `AGENT TESTING` section: auto-invoke the new agent with a test message to verify it responds correctly

**Step 4: Run test to verify it passes**

**Step 5: Commit**

```bash
git add backend/app/services/agent_prompts.py backend/tests/test_agent_definitions_prompts.py
git commit -m "feat: add structured output mode + validation pipeline to agent builder"
```

---

## Dependency Graph

```
Task 1 (general-purpose)  ──┐
Task 2 (explore)           ──┤
Task 3 (plan)              ──┼── Task 6 (retry wrapper)
Task 4 (worker)            ──┤         │
Task 5 (verification)      ──┘         ├── Task 7 (loop guard)
                                        │
Task 8 (swarm runtime)     ────────────┤
Task 9 (swarm mailbox)     ────────────┤
Task 10 (swarm tools)      ────────────┘
                                        
Task 11 (OHMO integration) ──────────── Task 12 (planning layer)

Task 13 (automation agent) ──────────── (independent)
Task 14 (skill agent)      ──────────── (independent)
Task 15 (agent builder)    ──────────── (independent)
```

## Execution Order

**Phase 1 (Parallel):** Tasks 1-5 (sub-agent prompts) — no dependencies, can run simultaneously
**Phase 2 (Sequential):** Task 6 → Task 7 (tool retry + loop guard)
**Phase 3 (Sequential):** Task 8 → Task 9 → Task 10 (swarm)
**Phase 4 (Parallel):** Tasks 11-15 (OHMO, planning, automation, skill, agent builder)

## Verification Checklist

After all tasks complete:
- [ ] All 6 builtin sub-agents have prompts > 40 lines
- [ ] All 6 builtin sub-agents pass prompt content tests
- [ ] Tool retry wrapper handles transient errors with exponential backoff
- [ ] Loop guard only counts failed calls toward cap
- [ ] Swarm coordinator uses real tool-calling loop (not raw call_llm)
- [ ] Swarm mailbox persists to database
- [ ] Swarm tools registered in tool registry
- [ ] OHMO workspace injects into system prompt
- [ ] SynexiaFSM triggers for complex requests (3+ steps)
- [ ] Automation agent prompt covers event triggers + conditions
- [ ] Skill agent prompt covers quality gates + testing
- [ ] Agent builder uses structured output mode
- [ ] All existing tests still pass
