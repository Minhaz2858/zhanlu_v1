"""Regression: Skill Agent prompt must not call `get_skill` and must
ask clarifying questions for bare requests.

Two related bugs observed in 2026-07-28 sessions:

(1) `get_skill` is NOT a registered tool. The SKILL_AGENT_SYSTEM_PROMPT
    had a "Skill Testing" step that told the agent to "Read back the
    saved skill via `get_skill`". The agent called `get_skill` and got
    a hard error: `"Unknown tool: get_skill"`. The user saw the red
    error in the trace even though the skill had actually been created
    successfully one step earlier. The right verification is to inspect
    the `create_skill` response (which already contains the saved
    fields) — no separate re-read needed.

(2) The agent created skills from bare labels (e.g. "Weekly report",
    "PDF summarizer", "Code reviewer" — the chip-click flow) without
    asking any clarifying questions. The user said: "user asked to
    create new skills why is it searching old skills, or it need to
    ask fallback question to user to make the perfect skills for the
    user". The prompt said "Gather the necessary information through
    conversation" but didn't say HOW — the agent interpreted it as
    "extract from context" and created generic skills.

Fixes in agent_prompts.py:
- The "Skill Testing" section no longer tells the agent to call
  `get_skill`. It explicitly says "do NOT call a `get_skill` tool".
- A new "### Bare-Request Handling" subsection under "Creating Skills"
  says to ask 2-3 targeted clarifying questions (via AskUserQuestion)
  for bare requests, and lists the most-leverage dimensions.

These tests pin both contracts.
"""

import importlib
import re

agent_prompts = importlib.import_module("app.services.agent_prompts")


def _prompt():
    return agent_prompts.SKILL_AGENT_SYSTEM_PROMPT


# --------------------------------------------------------------------
# (1) No `get_skill` call
# --------------------------------------------------------------------

def test_skill_agent_prompt_does_not_instruct_calling_get_skill():
    """The prompt must not tell the agent to call a `get_skill` tool.

    The agent has been observed to follow this instruction literally
    and produce `"Unknown tool: get_skill"` failures after every
    successful `create_skill`. The fix removes the instruction.
    """
    prompt = _prompt()
    # We tolerate a mention of `get_skill` if it is framed as a NEGATIVE
    # ("do NOT call", "there is no such tool"), but a bare positive
    # instruction is the bug. We check for the original bad phrase.
    bad = re.search(
        r"Read back the saved skill via `?get_skill`?",
        prompt,
    )
    assert not bad, (
        "Skill Agent prompt still instructs the agent to "
        "`Read back the saved skill via get_skill`. The `get_skill` "
        "tool does not exist in the Skill Agent's toolset — the "
        "`create_skill` response already contains the saved fields. "
        "Remove the instruction in the 'Skill Testing' section of "
        "agent_prompts.py."
    )


def test_skill_agent_prompt_explicitly_negates_get_skill():
    """The prompt must explicitly tell the agent NOT to call `get_skill`."""
    prompt = _prompt().lower()
    # Either an explicit "do not call" or a "there is no such tool" or
    # an "unknown tool" warning is acceptable. At minimum the prompt
    # must warn the agent away from the failure mode.
    has_warning = (
        ("do not" in prompt and "get_skill" in prompt)
        or ("no such tool" in prompt and "get_skill" in prompt)
        or ("unknown tool" in prompt and "get_skill" in prompt)
    )
    assert has_warning, (
        "Skill Agent prompt should explicitly warn the agent away "
        "from calling `get_skill` (e.g. 'do NOT call a `get_skill` "
        "tool: there is no such tool'). See the 'Skill Testing' "
        "section in agent_prompts.py."
    )


def test_skill_agent_prompt_redirects_to_create_skill_response():
    """The prompt must point the agent at the `create_skill` response
    as the source of saved fields."""
    prompt = _prompt().lower()
    # The new instruction should reference both create_skill (or
    # update_skill) AND the response as the verification source.
    assert "create_skill" in prompt or "update_skill" in prompt, (
        "Skill Agent prompt should reference create_skill/update_skill "
        "as the source of saved fields for the post-create dry-run."
    )
    assert "response" in prompt, (
        "Skill Agent prompt should tell the agent to inspect the "
        "create_skill response directly (the response already contains "
        "the saved fields)."
    )


# --------------------------------------------------------------------
# (2) Clarifying questions for bare requests
# --------------------------------------------------------------------

def test_skill_agent_prompt_has_bare_request_handling_section():
    """The 'Creating Skills' workflow must include bare-request handling."""
    prompt = _prompt()
    # We accept either a heading-style marker (preferred) or a
    # sentence that explicitly names the bare-request case. The
    # fix uses an H3 heading `### Bare-Request Handling`.
    assert "Bare-Request Handling" in prompt or "bare request" in prompt.lower(), (
        "Skill Agent prompt must have a 'Bare-Request Handling' "
        "section (or equivalent) under 'Creating Skills' so the agent "
        "knows to ask clarifying questions for vague requests."
    )


def test_skill_agent_prompt_directs_clarifying_questions_before_create():
    """For bare requests, the agent must ask 2-3 questions BEFORE creating."""
    prompt = _prompt().lower()
    # Look for explicit "ask ... before" guidance or "clarifying
    # question" wording near "bare".
    asks_before_create = (
        ("ask" in prompt and "before" in prompt and "creat" in prompt)
        or "clarifying question" in prompt
    )
    assert asks_before_create, (
        "Skill Agent prompt should direct the agent to ask clarifying "
        "questions BEFORE creating a skill when the request is bare."
    )


def test_skill_agent_prompt_names_leverage_dimensions():
    """The bare-request guidance must list concrete dimensions to ask about."""
    prompt = _prompt().lower()
    # At minimum, audience + output format should be mentioned so the
    # agent knows what to ask. Cadence and data source are also useful.
    must_have = ["audience", "output"]
    for dim in must_have:
        assert dim in prompt, (
            f"Skill Agent prompt should mention '{dim}' as a "
            f"clarifying-question dimension in the Bare-Request "
            f"Handling section."
        )


def test_skill_agent_prompt_chip_click_flow_explicitly_addressed():
    """The prompt must call out the chip-click flow as a common
    source of bare requests (so the agent doesn't treat the chips as
    complete specs)."""
    prompt = _prompt()
    # The fix references the "Try one of these" chips explicitly.
    assert "Try one of these" in prompt or "quick-start chip" in prompt or "quick-start" in prompt, (
        "Skill Agent prompt should name the 'Try one of these' chip "
        "flow (or 'quick-start chip') as a common source of bare "
        "requests, so the agent doesn't treat the chip text as a "
        "complete spec."
    )


def test_skill_agent_prompt_tells_agent_to_ask_at_most_3_questions():
    """The prompt must bound the question count so the agent doesn't
    interrogate the user."""
    prompt = _prompt().lower()
    # Look for "2-3" or "2 or 3" or "at most" wording near "ask".
    bounded = (
        "2-3" in prompt
        or "two or three" in prompt
        or ("at most" in prompt and "ask" in prompt)
        or "do not ask all" in prompt
    )
    assert bounded, (
        "Skill Agent prompt should tell the agent to ask 2-3 (not "
        "all six) clarifying questions for bare requests. See the "
        "Bare-Request Handling section in agent_prompts.py."
    )


# --------------------------------------------------------------------
# (3) search_skills budget + no-stop phrasing (2026-07-28)
# --------------------------------------------------------------------

def test_skill_agent_prompt_caps_search_skills_at_once():
    """Skill Agent must cap `search_skills` at one call per turn (2026-07-28).

    Regression test: agent was observed looping on `search_skills` 3x with
    the same args before giving up with "I'll stop the skill creation
    there". The fix lives in `## Skill Discovery` of the agent prompt and
    must include an "at most ONCE per turn" rule.
    """
    prompt = _prompt()
    # Locate the Skill Discovery section.
    m = re.search(
        r"##\s*Skill Discovery(.*?)(?=^##\s|\Z)",
        prompt,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert m, "Skill Agent prompt is missing a '## Skill Discovery' section"
    section = m.group(1)

    # The rule must be present, phrased in language the model can follow
    # (multiple acceptable wordings to avoid brittleness).
    assert re.search(
        r"at most (once|1 time|1)( every| per| in a)?\s*turn",
        section,
        flags=re.IGNORECASE,
    ), (
        "Skill Agent's '## Skill Discovery' section must cap search_skills "
        "at one call per turn. Found section text:\n" + section[:600]
    )

    # And the same-result rule: if the previous search returned the same
    # result, do not search again. This is what blocks the 3x identical
    # loop in practice.
    assert re.search(
        r"(same (result|keywords|search)|do\s+not\s+search\s+again|don[''`]?t\s+search\s+again|already searched)",
        section,
        flags=re.IGNORECASE,
    ), (
        "Skill Agent's '## Skill Discovery' section must say 'do not search "
        "again' (or equivalent) when the previous search returned the same "
        "result. Found section text:\n" + section[:600]
    )


def test_skill_agent_prompt_forbids_stop_phrasing():
    """Skill Agent must never say 'I'll stop' or 'stopping the skill creation'.

    Regression test: after the user clicked the directive chip
    "Create a new skill: a report generation tool", the agent replied
    "I'll stop the skill creation there" without ever calling
    `create_skill`. That is the exact bad pattern to forbid.

    The prompt may legitimately mention the forbidden phrases inside the
    rule text (e.g. `Never say "I'll stop"…`) — what we are pinning is
    that (a) the rule is present, and (b) the prompt does not instruct
    the model to actually USE those phrases.
    """
    prompt = _prompt()
    # Positive check: the prompt must include an explicit no-stop-phrasing
    # rule. Look for the rule patterns: "no-stop", "Never say", "Never
    # announce", "do not ... give up", etc.
    has_rule = re.search(
        r"no[- ]stop|no-?stop|never say|never announce|do not.*give up|don[''`]?t.*give up",
        prompt,
        flags=re.IGNORECASE,
    )
    assert has_rule, (
        "Skill Agent prompt must include an explicit no-stop-phrasing rule "
        "(e.g. 'Never say you will stop...' or 'do not announce that you are "
        "giving up on the request')."
    )

    # Negative check: the prompt must not positively instruct the agent
    # to use any of these stop-giving-up phrasings. We tolerate a
    # NEGATIVE mention (in the rule) but flag a POSITIVE one.
    # We detect "positive instruction" as the phrase being followed by a
    # verb like "and" / "then" / "so" / "while" or appearing in a quote
    # with no negation context. Simpler heuristic: look for the phrase
    # NOT preceded by words like 'never', 'don', 'do not', 'not', 'no '.
    forbidden_phrases = [
        "I'll stop",
        "I will stop",
        "stopping the skill creation",
        "stopping here",
        "giving up on the request",
    ]
    for phrase in forbidden_phrases:
        # Find all occurrences and check that each one is in a negation
        # context (preceded by 'never', 'don', 'do not', 'not', 'won't',
        # 'wouldn't', 'can't', 'shouldn't', etc.).
        for m in re.finditer(re.escape(phrase), prompt, flags=re.IGNORECASE):
            # Look at the 60 chars before the match for a negation.
            start = max(0, m.start() - 60)
            prefix = prompt[start:m.start()].lower()
            in_negation = bool(re.search(
                r"\b(never|do(?:es)?n[''`]?t|didn[''`]?t|won[''`]?t|"
                r"wouldn[''`]?t|shouldn[''`]?t|cann[''`]?t|"
                r"mustn[''`]?t|can|do\s+not|does\s+not|did\s+not|"
                r"no|not)\b",
                prefix,
            ))
            assert in_negation, (
                f"Skill Agent prompt has a POSITIVE (non-negated) "
                f"occurrence of forbidden stop-phrasing {phrase!r} at "
                f"position {m.start()}. The agent must NEVER be told to "
                f"use this phrasing — only warned against it. "
                f"Surrounding context: ...{prompt[max(0,m.start()-30):m.end()+30]}..."
            )


def test_skill_agent_prompt_uses_options_block_not_askuserquestion_tool():
    """Skill Agent must use `:::options` markdown block, not AskUserQuestion tool.

    Regression test: 2026-07-28, the prompt said to use the
    `AskUserQuestion` tool, but that tool is NOT registered in the
    Skill Agent's runtime toolset (only `create_skill`, `update_skill`,
    `list_tools`, `search_skills` are). Asking for an unregistered tool
    causes the LLM to spin in place. The fix is to use the `:::options`
    markdown block (rendered as chips by the chat UI) instead.

    The prompt may legitimately mention "AskUserQuestion" inside the
    negation rule — what we are pinning is that the prompt does not
    instruct the agent to USE the tool (positive instruction), only to
    AVOID it.
    """
    prompt = _prompt()
    # The fix: prompt must mention :::options as the way to ask clarifying
    # questions in the Bare-Request Handling section.
    assert ":::options" in prompt, (
        "Skill Agent prompt must reference the ':::options' markdown block "
        "as the way to present clarifying-question chips to the user. "
        "(The 'AskUserQuestion' tool is not registered in the runtime "
        "toolset, so referencing it causes the LLM to spin in place.)"
    )
    # Negative check: the prompt must not POSITIVELY instruct the agent
    # to use the AskUserQuestion tool. We tolerate a NEGATIVE mention
    # (e.g. "do NOT use an `AskUserQuestion` tool") but flag a positive
    # one (e.g. "use the `AskUserQuestion` tool").
    for m in re.finditer(r"AskUserQuestion", prompt):
        start = max(0, m.start() - 40)
        prefix = prompt[start:m.start()].lower()
        in_negation = bool(re.search(
            r"\b(never|do\s+not|don[''`]?t|no|not)\b", prefix
        ))
        assert in_negation, (
            f"Skill Agent prompt has a POSITIVE (non-negated) reference "
            f"to 'AskUserQuestion' at position {m.start()}. The "
            f"AskUserQuestion tool is not in the Skill Agent's toolset "
            f"— only warn the agent against it, do not instruct it to "
            f"use the tool. Surrounding context: "
            f"...{prompt[max(0,m.start()-30):m.end()+30]}..."
        )
