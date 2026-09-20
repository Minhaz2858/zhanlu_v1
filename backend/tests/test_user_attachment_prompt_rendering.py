"""Regression: an uploaded file's extracted text must reach the LLM prompts.

Bug (fixed): the CONTEXT state folds each upload's extracted text into the
manifest as a ``user_attachments`` item, but both prompt builders read ONLY the
``conversation_context`` item. Result: the file was extracted, persisted to
``executions.context_manifest``, and never shown to any model — the agent
answered "I could not access the attached file" (and asked the user to paste the
data) while the extracted rows sat unread in the manifest.

Verified live before the fix: a turn with ZERO tool calls
(``executions.tool_calls IS NULL``) reported "unknown tool
``read_attached_file``" plus a table of six failed attempts that never happened.
"""

import pytest

from app.services.synexia.context_assembler import render_attachment_items

FILE_TEXT = (
    "--- test.csv ---\n"
    "product_code,units_sold,unit_price_usd\n"
    "ZX-4471,1240,318.55\n"
    "ZX-4493,2310,87.40"
)


def _manifest(*contents):
    return {
        "items": [
            {"type": "agent_config", "content": {"name": "general_assistant"}},
            *[
                {"type": "user_attachments", "content": c, "priority": "high"}
                for c in contents
            ],
            {"type": "conversation_context", "content": {"transcript": "hi"}},
        ]
    }


class TestRenderAttachmentItems:
    def test_renders_uploaded_file_text(self):
        assert "ZX-4471" in render_attachment_items(_manifest(FILE_TEXT))

    def test_empty_when_manifest_missing_or_shapeless(self):
        assert render_attachment_items(None) == ""
        assert render_attachment_items({}) == ""
        assert render_attachment_items({"items": []}) == ""
        assert render_attachment_items({"items": "not-a-list-of-dicts"}) == ""

    def test_ignores_non_attachment_items(self):
        """Only user_attachments carries upload text — not agent_config etc."""
        out = render_attachment_items(
            {"items": [{"type": "agent_config", "content": "SECRET"}]}
        )
        assert out == ""

    def test_joins_multiple_files_and_dedupes(self):
        out = render_attachment_items(_manifest(FILE_TEXT, "--- b.md ---\nbee"))
        assert "ZX-4471" in out and "bee" in out
        # The same block injected twice must not be duplicated into the prompt.
        assert render_attachment_items(_manifest(FILE_TEXT, FILE_TEXT)).count(
            "ZX-4471"
        ) == 1

    def test_tolerates_non_string_content(self):
        assert render_attachment_items(
            {"items": [{"type": "user_attachments", "content": {"a": 1}}]}
        ) == ""


class TestResponsePromptCarriesAttachments:
    """The answer prompt must contain the file text, not just the transcript."""

    def _prompt(self, manifest, observations=()):
        from types import SimpleNamespace

        from app.services.synexia.fsm import ExecutionRequest, SynexiaFSM

        fsm = SynexiaFSM.__new__(SynexiaFSM)  # bypass __init__ (needs a live DB)
        fsm.execution = SimpleNamespace(
            context_manifest=manifest,
            observations=list(observations),
            task_spec={},
        )
        return fsm._build_response_prompt(
            ExecutionRequest(
                conversation_id="c1",
                agent_name="general_assistant",
                user_message="total revenue in the attached file?",
            )
        )

    def test_file_text_is_in_the_answer_prompt(self):
        prompt = self._prompt(_manifest(FILE_TEXT))
        assert "ZX-4471" in prompt, "uploaded file never reached the answer prompt"
        assert "User attachments" in prompt

    def test_no_file_means_no_attachment_block(self):
        assert "User attachments" not in self._prompt({"items": []})

    def test_does_not_ask_for_a_failure_narrative_when_nothing_ran(self):
        """Zero observations + an attachment must not read as a failed run."""
        prompt = self._prompt(_manifest(FILE_TEXT))
        assert "explain what went wrong" not in prompt
        assert "No actions taken." not in prompt

    def test_keeps_error_clause_when_there_are_real_observations(self):
        prompt = self._prompt({"items": []}, observations=[object()])
        assert "If there were errors, explain what went wrong." in prompt

    @pytest.mark.parametrize(
        "sentinel", ["No actions taken.", ""]
    )
    def test_sentinel_grounding_is_treated_as_nothing_ran(self, sentinel, monkeypatch):
        """extract_grounding() returns the literal 'No actions taken.' on empty.

        A truthiness check on that string is always True, so the first version of
        this guard silently never fired and the fabricated narrative survived.
        Patched at the SOURCE module: fsm.py imports extract_grounding locally
        inside the prompt builder, so the name never lands on the fsm module.
        """
        import app.services.synexia.grounding_extractor as ge_mod

        monkeypatch.setattr(ge_mod, "extract_grounding", lambda *a, **k: sentinel)
        prompt = self._prompt(_manifest(FILE_TEXT))
        assert "No actions taken." not in prompt
        assert "ZX-4471" in prompt
