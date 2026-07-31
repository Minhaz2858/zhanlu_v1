"""Regression: Skill Agent prompt must NOT use create_artifact to "demonstrate" skills.

User bug 2026-07-28: Clicking the "PDF summarizer" quick-start chip on the
Skill Agent empty state sent the bare text "PDF summarizer". The agent
created the skill correctly, but then called `create_artifact` to make a
3 KB sample PDF and tacked it onto the result. The user complained:
"user asked make skills but why is it providing one pdf file? it need to
create skills" — the artifact was unsolicited noise.

Root cause: SKILL_AGENT_SYSTEM_PROMPT in agent_prompts.py did not have
an explicit rule against using `create_artifact` to "demo" a skill. The
model inferred that creating a PDF summarizer entitled it to produce a
PDF.

Fix: a new "Artifacts (`create_artifact`)" section in the prompt with
hard rules. These tests pin the contract.
"""

import importlib

agent_prompts = importlib.import_module("app.services.agent_prompts")


def _prompt():
    return agent_prompts.SKILL_AGENT_SYSTEM_PROMPT


def test_skill_agent_prompt_has_artifacts_section():
    """The 'Artifacts' section must exist in the Skill Agent prompt."""
    prompt = _prompt()
    # Heading-style marker; we use "## Artifacts" (same convention as
    # "## Skill Testing" / "## Skill Categorization").
    assert "## Artifacts" in prompt, (
        "Skill Agent prompt must have an '## Artifacts' section explaining "
        "when `create_artifact` is appropriate. See agent_prompts.py."
    )


def test_skill_agent_prompt_forbids_sample_artifacts():
    """The 'Artifacts' section must explicitly forbid sample/demo artifacts."""
    prompt = _prompt().lower()
    # All four synonyms should be forbidden.
    for forbidden_kind in ("sample", "demo", "example", "preview", "trial"):
        assert (
            f"do not" in prompt and forbidden_kind in prompt
        ), (
            f"Skill Agent prompt should explicitly forbid generating "
            f"'{forbidden_kind}' artifacts after creating a skill. "
            f"See the new 'Artifacts' section in agent_prompts.py."
        )
    # The stub-pattern should also be called out (3 KB sample PDF was
    # exactly this anti-pattern in the original bug).
    assert "stub" in prompt or "placeholder" in prompt, (
        "Skill Agent prompt should call out that 'placeholder' / 'stub' "
        "artifacts are not useful and must not be created."
    )


def test_skill_agent_prompt_lists_legitimate_uses():
    """The 'Artifacts' section must name the two legitimate uses."""
    prompt = _prompt().lower()
    # Use 1: packaging the skill as a .skill file.
    assert ".skill" in prompt, (
        "Skill Agent prompt should mention `.skill` file packaging as a "
        "legitimate use of `create_artifact` (matches the existing line "
        "about skills being 'a self-contained .skill file the user can "
        "hand to another agent')."
    )
    # Use 2: explicit user request for a sample/demo artifact.
    assert "user explicitly asks" in prompt or (
        "user" in prompt and "explicitly" in prompt
    ), (
        "Skill Agent prompt should list 'user explicitly asks for a sample "
        "artifact' as a legitimate use of `create_artifact`."
    )


def test_skill_agent_prompt_redirects_to_binding():
    """The 'Artifacts' section must point the user at binding + running
    the skill as the correct 'try it out' path — not artifact generation."""
    prompt = _prompt().lower()
    assert "bind" in prompt and "agent" in prompt, (
        "Skill Agent prompt should tell the user that the correct 'try it "
        "out' path after creating a skill is to bind it to an agent and "
        "give the agent real input — not to call create_artifact."
    )


def test_skill_agent_runtime_config_still_enables_create_artifact():
    """create_artifact must remain in the Skill Agent's runtime toolset.

    The fix is a prompt-level rule, not a tool-removal. Removing
    `create_artifact` from the enabled_tools list would also break the
    legitimate .skill file packaging use case. Pin this against the
    same source of truth that test_skill_agent_expansion.py uses
    (`_build_system_agent_configs(registry=None)`).
    """
    from app.services.system_agents import _build_system_agent_configs

    configs = _build_system_agent_configs(registry=None)
    skill = next((c for c in configs if c.get("name") == "skill_agent"), None)
    assert skill is not None, "skill_agent missing from system agent configs"
    enabled = set((skill.get("tool_config") or {}).get("enabled_tools") or [])
    assert "create_artifact" in enabled, (
        "create_artifact must remain in skill_agent's enabled_tools — the "
        "fix for the sample-artifact bug is a prompt-level rule, not a "
        "tool-removal."
    )
