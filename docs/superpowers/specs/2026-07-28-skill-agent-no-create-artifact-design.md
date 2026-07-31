# Skill Agent — Remove `create_artifact` From Toolset

**Status:** Approved (brainstorming 2026-07-28)
**Date:** 2026-07-28
**Scope:** `backend/app/services/system_agents.py`, `backend/app/services/agent_prompts.py`, plus 1 new test file
**Out of scope:** Agent Builder, Automation, general_assistant, the `'.skill' file packaging` flow (becomes unreachable from the Skill Agent — acceptable trade-off), `create_artifact` in any other agent's toolset.

---

## 1. Problem

The Skill Agent has been observed producing PPTX/PDF/DOCX stub artifacts in the chat after creating a SKILL.md skill — exactly the "show what the skill does" anti-pattern. User screenshot 2026-07-28 captured the failure: a "PPTX database data-report-builder" artifact (30 KB stub) appeared in the file panel right after the `ppt-data-report-builder` skill was created.

The existing prompt rule at `agent_prompts.py:647-682` ("## Artifacts (`create_artifact`)") explicitly forbade this behavior with hard rules, but the model violated the rule in the observed session. A prompt rule is advisory; the model is free to ignore it.

The reliable fix: **remove the tool entirely** from the Skill Agent's toolset so the model literally cannot call it.

---

## 2. Goal

After this change:

- The Skill Agent's `tool_config.enabled_tools` does NOT include `create_artifact`.
- The Skill Agent's `manifest_json.boundaries.allowed` does NOT include `create_artifact`.
- The Skill Agent's prompt does NOT mention `create_artifact` at all (silent removal).
- The model has no schema to call `create_artifact`, so it cannot produce stray sample/demo artifacts.
- The `.skill` file packaging flow (the only legitimate use of `create_artifact` in this agent) becomes unreachable from the Skill Agent. **Accepted trade-off per the 2026-07-28 brainstorm.**

---

## 3. Design

### 3.1 Two sources of `create_artifact` in the Skill Agent's toolset (both must be removed)

The Skill Agent's runtime toolset is computed in `agent_prompts.py:1757-1811` (`get_tools(agent_name, tool_config, agent_app)`). The function checks 4 sources in priority order; for system agents, the first matching branch wins. The Skill Agent matches branch 3 (the static list fallback) **OR** branch 4 (the agent's `tool_config` — `system_agents.py:184-191` is what populates this in the DB).

Two sources currently add `create_artifact`:

1. **`tool_config.enabled_tools`** — `system_agents.py:191`:
   ```python
   "tool_config": {
       "enabled_tools": _tools_in_registry(registry, ALL_TOOL_NAMES),
   }
   ```
   `ALL_TOOL_NAMES` (line 29-68) includes `"create_artifact"` at line 58. The `_tools_in_registry(registry, ALL_TOOL_NAMES)` call resolves to the full set of registered tools that the agent can use, including `create_artifact`.

2. **`manifest_json.boundaries.allowed`** — `system_agents.py:200`:
   ```python
   "boundaries": {
       "allowed": ["create_skill", "update_skill", "search_skills", "list_tools", "create_artifact"],
       "forbidden": ["delete_skill", "modify_system_skills"],
   }
   ```
   The permission check at the dispatch layer (search confirmed: `system_agents.py:336-342` shows the permission check happens before tool execution) reads this list. The Skill Agent's permission to call `create_artifact` comes from this entry.

### 3.2 Concrete changes

#### 3.2.1 `backend/app/services/system_agents.py:191` — replace `ALL_TOOL_NAMES` with a Skill-Agent-specific list

Add a new constant after the `ALL_TOOL_NAMES` definition (line 68):

```python
# 2026-07-28: Skill Agent must not call `create_artifact` to produce
# sample/demo artifacts after creating a skill. Same as ALL_TOOL_NAMES
# but with `create_artifact` removed. See spec
# docs/superpowers/specs/2026-07-28-skill-agent-no-create-artifact-design.md.
SKILL_AGENT_TOOL_NAMES = [t for t in ALL_TOOL_NAMES if t != "create_artifact"]
```

Then change line 191 from:
```python
"enabled_tools": _tools_in_registry(registry, ALL_TOOL_NAMES),
```
to:
```python
"enabled_tools": _tools_in_registry(registry, SKILL_AGENT_TOOL_NAMES),
```

#### 3.2.2 `backend/app/services/system_agents.py:200` — remove `"create_artifact"` from `boundaries.allowed`

Change:
```python
"allowed": ["create_skill", "update_skill", "search_skills", "list_tools", "create_artifact"],
```
to:
```python
"allowed": ["create_skill", "update_skill", "search_skills", "list_tools"],
```

#### 3.2.3 `backend/app/services/agent_prompts.py:647-682` — delete the "## Artifacts (`create_artifact`)" section

The user chose "silent removal" in the brainstorm — no replacement text. The 35-line section (header + 4 hard rules + 2 legitimate-use conditions + a closing line) is deleted entirely. The previous `### Bare-Request Handling` section above it (lines 534-583) ends with a list of dimensions; the next section after the deletion is `## Skill Categorization` (line 684).

Find the section starting at line 647:
```python
## Artifacts (`create_artifact`)

The `create_artifact` tool is available but is **NOT** a "demonstrate the
skill" button. Its sole legitimate use in this agent is **packaging the
final skill as a portable `.skill` file** so the user can hand it to
another agent or share it.

**Hard rules:**

- **Do NOT** call `create_artifact` to produce a sample, demo, example,
  preview, or trial output of the skill you just created. The skill is
  methodology (a `skill_md`), not a runnable artifact — there is nothing
  to "demo" by writing a file. If you find yourself about to call
  `create_artifact` to "show what the skill does", stop — that is the
  wrong move.
- **Do NOT** call `create_artifact` to generate a placeholder or stub
  file "so the user can try it". A 3 KB sample PDF is not a useful
  artifact; it adds noise to the conversation and clutters the user's
  artifact list.
- The user can run the skill themselves after creation by binding it
  to an agent and giving the agent real input. That is the correct
  "try it out" path, not artifact generation.

**When `create_artifact` IS appropriate:**

- The user explicitly asks to "package", "export", "bundle", or "save
  as a `.skill` file".
- The user explicitly asks for a sample, example, or demo artifact of
  the skill (e.g. "create a sample PDF showing what this skill would
  produce"). In that case, the user's request is the contract — do it,
  and do it well (real-looking content, not a stub).

If none of these conditions apply, do not call `create_artifact`.
Report success with the saved skill's metadata + the read-back result
and stop. The user will tell you if they want a packaged `.skill` or a
sample.

## Skill Categorization
```

Replace with just the `## Skill Categorization` header (so the previous section above it ends cleanly with a blank line):

```python
## Skill Categorization
```

**Note**: other `create_artifact` references in `agent_prompts.py` (lines 71-91, 847-859) are in `_DEFAULT_SKILLS_BLOCK` (appended to `GENERAL_ASSISTANT_SYSTEM_PROMPT`) and a different section that's not the Skill Agent's. Those references stay untouched — `create_artifact` is still available to the general assistant.

### 3.3 New test file

#### 3.3.1 `backend/tests/test_skill_agent_no_create_artifact.py`

Located at `backend/tests/test_skill_agent_no_create_artifact.py`. Pins the contract that the Skill Agent's toolset does not include `create_artifact`.

Tests:

1. **`test_skill_agent_tools_static_list_excludes_create_artifact`** — loads `agent_prompts.SKILL_AGENT_TOOLS` and asserts no tool has `name == "create_artifact"`. (Defensive: if the static list ever gets re-instated, this catches it.)

2. **`test_skill_agent_enabled_tools_excludes_create_artifact`** — imports the resolved toolset the way `get_tools("skill_agent", ...)` would, and asserts the resolved list does not contain a `create_artifact` tool. Uses the actual `get_tools` function with a fake `tool_config` and a minimal `agent_app` to exercise the full resolution path.

3. **`test_skill_agent_boundaries_allowed_excludes_create_artifact`** — parses `system_agents.py` as text and locates the Skill Agent's `manifest_json.boundaries.allowed` list. Asserts `"create_artifact"` is not in it. (AST-based so it doesn't depend on the agent's runtime creation logic.)

4. **`test_skill_agent_tool_names_constant_excludes_create_artifact`** — parses `system_agents.py` and asserts a constant named `SKILL_AGENT_TOOL_NAMES` exists at module level and does NOT include `"create_artifact"`. This is the load-bearing test: it pins the constant existence (catches the case where someone reverts to `ALL_TOOL_NAMES` at line 191).

5. **`test_skill_agent_prompt_does_not_mention_create_artifact`** — source-string scan of `SKILL_AGENT_SYSTEM_PROMPT` (the Skill Agent's prompt) and asserts the string `"create_artifact"` does not appear anywhere in it. Pins the "silent removal" decision.

6. **`test_skill_agent_prompt_no_artifact_section_header`** — source-string scan: assert no `## Artifacts` markdown section header exists in the Skill Agent prompt. (Defensive: if someone re-adds the section with a different title, this catches it.)

### 3.4 Out of scope (explicit)

- **`.skill` file packaging**. The user explicitly chose to make this flow unreachable from the Skill Agent. Users who want to package a skill can do so via the database directly, the `Tool` entity, or a future dedicated `package_skill` tool (out of scope for this turn).
- **`create_artifact` for other agents** (Agent Builder, general_assistant, automation_agent, etc.). The general assistant and automation agent legitimately need `create_artifact` for file deliverables. Their `boundaries.allowed` and `enabled_tools` stay unchanged.
- **Replacing the "Artifacts" prompt rule with a positive instruction**. The user picked "silent removal"; we don't add a replacement rule.

---

## 4. Error handling

| Failure mode | What happens |
|---|---|
| A user AgentApp record was created BEFORE this change and stored a `tool_config.enabled_tools` list that includes `create_artifact` | The runtime reads the persisted `tool_config` (line 1773 in `agent_prompts.py`), not the system default. Their tool config still includes `create_artifact`. **Acceptable**: only system agents use `system_agents.py` defaults; user agents use whatever's persisted in their row. No migration needed. |
| The model is told via a previous conversation that `create_artifact` exists | Future system-agent conversations re-load the prompt on every turn, so the stale mention has no effect. Existing conversation history is not re-prompted. |
| The model calls `create_artifact` via a tool alias or fallback | The permission check at `agent_tools.py:336-342` enforces `boundaries.allowed`. If `create_artifact` is removed from the list, the call returns `{"success": False, "error": "Permission denied: ..."}`. (Confirmed via search of permission-check code path.) |
| A test imports `SKILL_AGENT_TOOLS` and expects 4 tools including a sample/demo helper | None of the existing tests in `test_skill_agent_*` reference `create_artifact`. Verified via search. |

---

## 5. Testing

### 5.1 New test file (described in §3.3.1)

6 tests in `backend/tests/test_skill_agent_no_create_artifact.py`.

### 5.2 E2E smoke (no new tests, just re-run)

After the changes, all existing Skill Agent tests must still pass:

- `backend/tests/test_skill_agent_prompt_skill_creation_workflow.py` (12 tests)
- `backend/tests/test_skill_agent_loop_guard_config.py` (9 tests)
- `backend/tests/test_skill_agent_multiselect_options_block.py` (6 tests)
- `frontend/src/lib/translations.test.js` (4 tests)
- `frontend/src/lib/translations.skillAgentKeys.test.jsx` (3 tests)
- `frontend/src/pages/SkillAgent.emptyState.test.jsx`
- `frontend/src/pages/SkillAgent.applySuggestion.test.jsx`
- `frontend/src/pages/SkillAgent.currentSkillSync.test.jsx`
- `frontend/src/components/skillagent/*` (8 tests)
- `frontend/src/components/agent/AgentMarkdown.optionsBlock.multiselect.test.jsx` (9 tests)

### 5.3 Manual smoke (user action)

User opens `localhost:3000/skill-agent`, types "Code reviewer", confirms:
- 1 `search_skills` call
- 1 `:::options` block with 9 dimension-prefixed chips
- User clicks 2 chips, "Use these (2)" becomes active
- User clicks → input populated, presses Enter
- Agent creates the skill
- **No `create_artifact` call in the trace. No PPTX/PDF/DOCX in the file panel.**

---

## 6. Rollout

### 6.1 Rollout sequence

1. `backend/app/services/system_agents.py` — add `SKILL_AGENT_TOOL_NAMES` constant + change line 191 + change line 200.
2. `backend/app/services/agent_prompts.py` — delete the "## Artifacts" section (lines 647-682).
3. `backend/tests/test_skill_agent_no_create_artifact.py` — new file, 6 tests.
4. Run all Skill Agent tests (3 backend files + 1 new = 33 tests, 0 regressions).
5. Manual smoke (user does this in the browser).
6. Leave uncommitted per project policy.

### 6.2 Risk

- **Risk: persisted user AgentApp records with old tool configs.** A user who customized the Skill Agent's toolset BEFORE this change will have the old config in the DB. The new system default does NOT propagate to existing records. **Mitigation**: this is the same behavior as any other system-agent config change in this codebase (the system default is the seed; user overrides persist). Document in the spec and move on.
- **Risk: model hallucinates `create_artifact` and tries to call it.** The permission check at `agent_tools.py:336-342` rejects it. The test `test_skill_agent_boundaries_allowed_excludes_create_artifact` pins the permission contract.
- **Risk: the prompt gets very long if more sections are added.** The 35-line deletion is a small but real reduction. Negligible.
