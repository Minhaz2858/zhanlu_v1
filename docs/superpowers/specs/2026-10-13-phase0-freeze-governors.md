# Option C — Phase 0: Keyword-Governor Freeze (policy)

**Date:** 2026-10-13 · **Status:** In effect from this commit

## The rule

**No new keyword/regex governor may be added to the turn path.** New
behavior is added as a tool the orchestrator (or router) chooses — never
as another `if "word" in message` branch between pipelines.

## Why

Every regression class we fixed in the 2026-10 sessions traces to one
pattern: a regex guessing intent from the user's text and overriding a
real outcome —

- glossary shortcut hijacking an image question → answered a definition
- file-intent regex suppressing a deliberately planned artifact
- sandbox strip deleting a planned deliverable
- bounce-back markers flagging correct answers for revision
- `<100 chars` fallback replacing a correct concise answer
- fake unsupported slots from temporal/entity words

~150 such sites exist today. Each new one interferes with the existing
149 in unpredictable ways. The goal-based principle: decisions come from
the plan and the model, never from word lists.

## What this freezes

- `PlanTrigger` sources in `agents.py` (data-bound/data-tool/glossary
  overrides) — no new sources, no new keyword vocabularies
- Intent classifiers in `services/synexia/*` — bug fixes may correct
  existing patterns (e.g. the multilingual vocabulary work), but no new
  intent-gating by keywords
- Response rewriting / suppression gates — the honesty contract (say
  what happened) replaces any canned replacement

## Escape hatch

A keyword match may remain ONLY when it is (a) inside a capability (below
the loop), (b) fail-open, and (c) documented here. Examples already
legitimate: tool_security read-only enforcement, guardrail loop caps.

## The path off the freeze

Phase 1 (LLM turn router, `services/turn_router.py`, flag
`V3_LLM_ROUTER_MODE` off→dark→on) replaces the routing decision; Phase 2
boxes the FSM as `query_database`; Phase 3 deletes the surviving
governors with their dark-comparison data as the safety evidence.
