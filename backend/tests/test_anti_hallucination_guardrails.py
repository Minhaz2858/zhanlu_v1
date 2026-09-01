"""Anti-hallucination guardrail tests — defense-in-depth for agents with
bound data sources.

Tests 5 layers of guardrail:
  1. Runtime hallucination guardrail (detects no-tool-call + data question + bound KBs → retry)
  2. tool_choice forcing on first iteration for data questions
  3. Prominent anti-hallucination directive prepended to system prompt
  4. Startup-time prompt normalization for existing agents
  5. Data-question keyword heuristic

These tests follow the existing unittest patterns in test_prompt_tools_normalization.py
and data_source_runtime/_e2e.py.
"""

import os
import sys
import unittest
from unittest.mock import AsyncMock, patch, MagicMock

# Make `app` importable
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


# ---------------------------------------------------------------------------
# Fix 5: Data-question keyword heuristic
# ---------------------------------------------------------------------------

class TestDataQuestionHeuristic(unittest.TestCase):
    """Tests for _is_data_question() — the keyword heuristic that determines
    whether a user message is asking about data."""

    def test_matches_top_customers(self):
        """'who are my top 5 customers?' should be detected as a data question."""
        from app.routers.agents import _is_data_question
        self.assertTrue(_is_data_question("who are my top 5 customers?"))

    def test_matches_sales_report(self):
        from app.routers.agents import _is_data_question
        self.assertTrue(_is_data_question("generate a sales report for Q3"))

    def test_matches_monthly_revenue(self):
        from app.routers.agents import _is_data_question
        self.assertTrue(_is_data_question("show me monthly revenue trends"))

    def test_matches_how_many_orders(self):
        from app.routers.agents import _is_data_question
        self.assertTrue(_is_data_question("how many orders were placed last week?"))

    def test_matches_revenue_query(self):
        from app.routers.agents import _is_data_question
        self.assertTrue(_is_data_question("what is the total revenue?"))

    def test_matches_database_query(self):
        from app.routers.agents import _is_data_question
        self.assertTrue(_is_data_question("query the database for active users"))

    def test_matches_statistics(self):
        from app.routers.agents import _is_data_question
        self.assertTrue(_is_data_question("show me statistics on user engagement"))

    def test_matches_profit_margin(self):
        from app.routers.agents import _is_data_question
        self.assertTrue(_is_data_question("what's the profit margin this quarter?"))

    def test_does_not_match_hello(self):
        from app.routers.agents import _is_data_question
        self.assertFalse(_is_data_question("hello"))

    def test_does_not_match_capabilities(self):
        from app.routers.agents import _is_data_question
        self.assertFalse(_is_data_question("what can you do?"))

    def test_does_not_match_create_agent(self):
        from app.routers.agents import _is_data_question
        # "customer" is in the keyword list, but "create an agent" is a
        # non-data operation that should be excluded.
        self.assertFalse(_is_data_question("create an agent for customer support"))

    def test_does_not_match_create_skill(self):
        from app.routers.agents import _is_data_question
        self.assertFalse(_is_data_question("create a new skill for data analysis"))

    def test_does_not_match_empty(self):
        from app.routers.agents import _is_data_question
        self.assertFalse(_is_data_question(""))

    def test_does_not_match_none(self):
        from app.routers.agents import _is_data_question
        self.assertFalse(_is_data_question(None))

    def test_case_insensitive(self):
        from app.routers.agents import _is_data_question
        self.assertTrue(_is_data_question("SHOW ME THE REVENUE"))
        self.assertTrue(_is_data_question("Top Customers"))


# ---------------------------------------------------------------------------
# Fix 1: Runtime hallucination guardrail
# ---------------------------------------------------------------------------

class TestHallucinationGuardrail(unittest.TestCase):
    """Tests for _check_hallucination_guardrail() — the function that decides
    whether to retry when the LLM emits no tool call on a data question.

    Returns _GuardrailResult(action, message) where action is:
      "none"     → guardrail did not trigger
      "nudge"    → inject message and retry
      "fallback" → replace hallucinated content with message and break
    """

    def test_guardrail_triggers_when_bound_kbs_and_data_question(self):
        """When bound KBs exist, question is data-related, no tool call made,
        the guardrail should return a nudge."""
        from app.routers.agents import _check_hallucination_guardrail
        result = _check_hallucination_guardrail(
            user_message="who are my top 5 customers?",
            data_ctx_extras={"bound_kb_ids": ["kb-1", "kb-2"]},
            tool_calls_made=[],
            iteration=0,
            guardrail_retries=0,
        )
        self.assertEqual(result.action, "nudge")
        self.assertIn("ask_data_agent", result.message)

    def test_guardrail_no_trigger_without_bound_kbs(self):
        """When no KBs are bound, guardrail should not trigger."""
        from app.routers.agents import _check_hallucination_guardrail
        result = _check_hallucination_guardrail(
            user_message="who are my top 5 customers?",
            data_ctx_extras={},
            tool_calls_made=[],
            iteration=0,
            guardrail_retries=0,
        )
        self.assertEqual(result.action, "none")

    def test_guardrail_no_trigger_when_ask_data_agent_called(self):
        """When ask_data_agent was already called, guardrail should not trigger
        (the agent did the right thing)."""
        from app.routers.agents import _check_hallucination_guardrail
        result = _check_hallucination_guardrail(
            user_message="who are my top 5 customers?",
            data_ctx_extras={"bound_kb_ids": ["kb-1"]},
            tool_calls_made=[{"name": "ask_data_agent"}],
            iteration=1,
            guardrail_retries=0,
        )
        self.assertEqual(result.action, "none")

    def test_guardrail_no_trigger_on_non_data_question(self):
        """When the question is not data-related, guardrail should not trigger."""
        from app.routers.agents import _check_hallucination_guardrail
        result = _check_hallucination_guardrail(
            user_message="hello, how are you?",
            data_ctx_extras={"bound_kb_ids": ["kb-1"]},
            tool_calls_made=[],
            iteration=0,
            guardrail_retries=0,
        )
        self.assertEqual(result.action, "none")

    def test_guardrail_fallback_when_retries_exhausted(self):
        """When guardrail_retries >= 2 (max), guardrail should return a
        safe fallback instead of a nudge to retry."""
        from app.routers.agents import _check_hallucination_guardrail
        result = _check_hallucination_guardrail(
            user_message="who are my top 5 customers?",
            data_ctx_extras={"bound_kb_ids": ["kb-1"]},
            tool_calls_made=[],
            iteration=0,
            guardrail_retries=2,
        )
        self.assertEqual(result.action, "fallback")
        # The fallback should NOT contain retry instructions
        self.assertNotIn("MUST call", result.message)

    def test_guardrail_triggers_on_later_iteration_without_ask_data_agent(self):
        """Guardrail triggers on any iteration where the LLM emits no tool call
        and ask_data_agent was never called — not just iteration 0."""
        from app.routers.agents import _check_hallucination_guardrail
        result = _check_hallucination_guardrail(
            user_message="who are my top 5 customers?",
            data_ctx_extras={"bound_kb_ids": ["kb-1"]},
            tool_calls_made=[],
            iteration=3,
            guardrail_retries=0,
        )
        self.assertEqual(result.action, "nudge")

    def test_guardrail_triggers_when_other_tool_called(self):
        """If the LLM called a non-ask_data_agent tool (e.g. web_search) but
        not ask_data_agent, the guardrail should still trigger because the
        agent hasn't queried the DATABASE yet."""
        from app.routers.agents import _check_hallucination_guardrail
        result = _check_hallucination_guardrail(
            user_message="who are my top 5 customers?",
            data_ctx_extras={"bound_kb_ids": ["kb-1"]},
            tool_calls_made=[{"name": "web_search"}],
            iteration=1,
            guardrail_retries=0,
        )
        # web_search was called but not ask_data_agent — should still trigger
        self.assertEqual(result.action, "nudge")

    # -- Per-model bypass flag (weak local model opt-out) -------------------

    def test_bypass_flag_skips_guardrail(self):
        """When bypass_guardrail=True, the guardrail must return action="none"
        even for a data question with bound KBs and no tool call."""
        from app.routers.agents import _check_hallucination_guardrail
        result = _check_hallucination_guardrail(
            user_message="who are my top 5 customers?",
            data_ctx_extras={"bound_kb_ids": ["kb-1"]},
            tool_calls_made=[],
            iteration=0,
            guardrail_retries=0,
            model_id="qwen3.5-27b-tools",
            bypass_guardrail=True,
        )
        self.assertEqual(result.action, "none")
        self.assertEqual(result.message, "")

    def test_bypass_flag_default_false_keeps_guardrail(self):
        """When bypass_guardrail is omitted (default False), the guardrail
        behaves exactly as before (nudge on data question + bound KBs)."""
        from app.routers.agents import _check_hallucination_guardrail
        result = _check_hallucination_guardrail(
            user_message="who are my top 5 customers?",
            data_ctx_extras={"bound_kb_ids": ["kb-1"]},
            tool_calls_made=[],
            iteration=0,
            guardrail_retries=0,
        )
        self.assertEqual(result.action, "nudge")

    def test_bypass_flag_explicit_false_keeps_guardrail(self):
        """bypass_guardrail=False explicitly should behave identically to
        the default (guardrail still triggers)."""
        from app.routers.agents import _check_hallucination_guardrail
        result = _check_hallucination_guardrail(
            user_message="who are my top 5 customers?",
            data_ctx_extras={"bound_kb_ids": ["kb-1"]},
            tool_calls_made=[],
            iteration=0,
            guardrail_retries=0,
            model_id="deepseek-chat",
            bypass_guardrail=False,
        )
        self.assertEqual(result.action, "nudge")

    def test_bypass_flag_skips_even_when_retries_exhausted(self):
        """bypass takes precedence over the fallback branch: even when
        guardrail_retries >= max, bypass_guardrail=True returns action="none"."""
        from app.routers.agents import _check_hallucination_guardrail
        result = _check_hallucination_guardrail(
            user_message="who are my top 5 customers?",
            data_ctx_extras={"bound_kb_ids": ["kb-1"]},
            tool_calls_made=[],
            iteration=0,
            guardrail_retries=2,
            model_id="llama3.2",
            bypass_guardrail=True,
        )
        self.assertEqual(result.action, "none")


# ---------------------------------------------------------------------------
# Fix 2: tool_choice forcing
# ---------------------------------------------------------------------------

class TestToolChoiceForcing(unittest.TestCase):
    """Tests for _compute_tool_choice() — determines when to force
    ask_data_agent via tool_choice."""

    def test_force_on_first_iteration_data_question_bound_kbs(self):
        """On iteration 0, with a data question and bound KBs,
        tool_choice should force ask_data_agent (when the tool is
        granted — server-side enforcement is gated on tool presence)."""
        from app.routers.agents import _compute_tool_choice
        result = _compute_tool_choice(
            user_message="top 5 customers",
            data_ctx_extras={"bound_kb_ids": ["kb-1"]},
            iteration=0,
            tool_names=["ask_data_agent", "web_search"],
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["type"], "function")
        self.assertEqual(result["function"]["name"], "ask_data_agent")

    def test_auto_on_non_data_question(self):
        """For non-data questions, tool_choice should be auto (None)."""
        from app.routers.agents import _compute_tool_choice
        result = _compute_tool_choice(
            user_message="hello",
            data_ctx_extras={"bound_kb_ids": ["kb-1"]},
            iteration=0,
            tool_names=["ask_data_agent"],
        )
        self.assertIsNone(result)

    def test_auto_without_bound_kbs(self):
        """Without bound KBs, tool_choice should be auto (None)."""
        from app.routers.agents import _compute_tool_choice
        result = _compute_tool_choice(
            user_message="top 5 customers",
            data_ctx_extras={},
            iteration=0,
            tool_names=["ask_data_agent"],
        )
        self.assertIsNone(result)

    def test_auto_on_subsequent_iterations(self):
        """On iteration > 0, tool_choice should be auto (None) —
        the LLM has already had its chance to call the tool."""
        from app.routers.agents import _compute_tool_choice
        result = _compute_tool_choice(
            user_message="top 5 customers",
            data_ctx_extras={"bound_kb_ids": ["kb-1"]},
            iteration=1,
            tool_names=["ask_data_agent"],
        )
        self.assertIsNone(result)

    def test_auto_on_create_agent_question(self):
        """Even with bound KBs, 'create an agent' should NOT force
        ask_data_agent — it's not a data question."""
        from app.routers.agents import _compute_tool_choice
        result = _compute_tool_choice(
            user_message="create an agent for customer support",
            data_ctx_extras={"bound_kb_ids": ["kb-1"]},
            iteration=0,
            tool_names=["ask_data_agent"],
        )
        self.assertIsNone(result)

    def test_force_create_artifact_on_doc_intent(self):
        """When the message asks for a pptx/docx and create_artifact is
        in the tool list, server forces create_artifact on iteration 0."""
        from app.routers.agents import _compute_tool_choice
        result = _compute_tool_choice(
            user_message="Make me a PowerPoint on Q3 sales",
            data_ctx_extras={},
            iteration=0,
            tool_names=["create_artifact", "web_search"],
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["function"]["name"], "create_artifact")

    def test_force_web_extract_on_url(self):
        """When the message contains a URL and web_extract is in the
        tool list, server forces web_extract on iteration 0."""
        from app.routers.agents import _compute_tool_choice
        result = _compute_tool_choice(
            user_message="summarize https://example.com",
            data_ctx_extras={},
            iteration=0,
            tool_names=["web_extract", "web_search"],
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["function"]["name"], "web_extract")

    def test_force_web_search_on_time_sensitive(self):
        """When the message is time-sensitive and web_search is in the
        tool list, server forces web_search on iteration 0."""
        from app.routers.agents import _compute_tool_choice
        result = _compute_tool_choice(
            user_message="latest news on AI",
            data_ctx_extras={},
            iteration=0,
            tool_names=["web_search", "create_artifact"],
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["function"]["name"], "web_search")

    def test_no_force_when_tool_absent(self):
        """If the matching tool is not in the agent's tool list, the
        server must NOT force a missing tool (would break the LLM
        contract)."""
        from app.routers.agents import _compute_tool_choice
        result = _compute_tool_choice(
            user_message="latest news on AI",
            data_ctx_extras={},
            iteration=0,
            tool_names=["create_artifact"],
        )
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Fix 3: Prominent anti-hallucination prompt directive
# ---------------------------------------------------------------------------

class TestPromptDirectivePrepend(unittest.TestCase):
    """Tests that the CRITICAL RULE is prepended to the system prompt
    (not appended at the end)."""

    def test_critical_rule_at_top_of_prompt(self):
        """When prepare_data_source_runtime augments the prompt, the
        CRITICAL RULE should appear at the very top (before base content)."""
        from app.services.data_source_runtime import prepare_data_source_runtime
        from app.models.agent_app import AgentApp
        from app.models.knowledge_base import KnowledgeBase
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app.database import Base
        import tempfile

        fd, meta_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        engine = create_engine(f"sqlite:///{meta_path}")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()

        kb = KnowledgeBase(
            id="kb_test",
            name="Test DB",
            source_kind="database",
            db_type="sqlite",
            status="active",
        )
        db.add(kb)
        db.commit()

        agent = AgentApp(
            name="Test Agent",
            knowledge_bases=["kb_test"],
        )
        db.add(agent)
        db.commit()

        base_prompt = "You are a helpful assistant."
        tools, prompt, extras = prepare_data_source_runtime(
            db, agent, base_tools=[], base_system_prompt=base_prompt,
        )

        # The CRITICAL RULE should appear BEFORE the base prompt content
        critical_idx = prompt.find("CRITICAL RULE")
        base_idx = prompt.find("You are a helpful assistant.")
        self.assertGreater(critical_idx, -1, "CRITICAL RULE not found in prompt")
        self.assertGreater(base_idx, -1, "Base prompt not found")
        self.assertLess(critical_idx, base_idx,
                        "CRITICAL RULE should appear before base prompt content")

        # Cleanup
        db.close()
        engine.dispose()
        os.unlink(meta_path)

    def test_critical_rule_mentions_ask_data_agent(self):
        """The CRITICAL RULE should mention ask_data_agent by name."""
        from app.services.data_source_runtime import prepare_data_source_runtime
        from app.models.agent_app import AgentApp
        from app.models.knowledge_base import KnowledgeBase
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app.database import Base
        import tempfile

        fd, meta_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        engine = create_engine(f"sqlite:///{meta_path}")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()

        kb = KnowledgeBase(
            id="kb_test2",
            name="Test DB",
            source_kind="database",
            db_type="sqlite",
            status="active",
        )
        db.add(kb)
        db.commit()

        agent = AgentApp(
            name="Test Agent",
            knowledge_bases=["kb_test2"],
        )
        db.add(agent)
        db.commit()

        tools, prompt, extras = prepare_data_source_runtime(
            db, agent, base_tools=[], base_system_prompt="BASE",
        )

        # Check ask_data_agent appears in the CRITICAL RULE section (top)
        critical_section = prompt[:500]  # first 500 chars
        self.assertIn("ask_data_agent", critical_section)
        self.assertIn("CRITICAL", critical_section[:100].upper())

        db.close()
        engine.dispose()
        os.unlink(meta_path)


# ---------------------------------------------------------------------------
# Fix 4: Startup-time prompt normalization
# ---------------------------------------------------------------------------

class TestStartupPromptNormalization(unittest.TestCase):
    """Tests that normalize_all_agent_prompts() updates stale prompt_tools
    for agents with bound KBs at startup."""

    def test_normalizes_stale_prompt_tools(self):
        """An agent with bound KBs but stale prompt_tools (no ask_data_agent
        mention) should get normalized to include the ask_data_agent block."""
        from app.services.agent_tools import (
            _normalize_prompt_tools_for_bound_kbs,
            _DB_TOOLS_BLOCK_MARKER_START,
        )
        # Simulate a stale prompt that references display name
        stale = "Tool selection: use Database Query for data."
        result = _normalize_prompt_tools_for_bound_kbs(stale, ["kb-1"])
        self.assertIn("ask_data_agent", result)
        self.assertIn(_DB_TOOLS_BLOCK_MARKER_START, result)

    def test_does_not_modify_already_normalized(self):
        """An agent whose prompt_tools already mentions ask_data_agent
        should not be modified (idempotent)."""
        from app.services.agent_tools import _normalize_prompt_tools_for_bound_kbs
        already_good = "Use `ask_data_agent` for all database queries."
        result = _normalize_prompt_tools_for_bound_kbs(already_good, ["kb-1"])
        self.assertEqual(result, already_good)

    def test_startup_function_exists(self):
        """The normalize_all_agent_prompts function should exist in
        agent_tools.py for startup-time normalization."""
        from app.services.agent_tools import normalize_all_agent_prompts
        self.assertTrue(callable(normalize_all_agent_prompts))


# ---------------------------------------------------------------------------
# Integration: Guardrail retry flow
# ---------------------------------------------------------------------------

class TestGuardrailRetryFlow(unittest.TestCase):
    """Integration test: when the LLM hallucinates (no tool call on a data
    question with bound KBs), the guardrail should trigger a retry, and
    the retry should include a nudge message in the conversation."""

    def test_guardrail_retry_adds_nudge_message(self):
        """The nudge message returned by the guardrail should contain
        explicit instructions to call ask_data_agent."""
        from app.routers.agents import _check_hallucination_guardrail
        result = _check_hallucination_guardrail(
            user_message="show me sales data",
            data_ctx_extras={"bound_kb_ids": ["kb-1"]},
            tool_calls_made=[],
            iteration=0,
            guardrail_retries=0,
        )
        self.assertEqual(result.action, "nudge")
        # The nudge should instruct calling ask_data_agent
        self.assertIn("ask_data_agent", result.message)
        # Should mention not fabricating
        self.assertTrue(
            "fabricat" in result.message.lower() or "invent" in result.message.lower() or
            "do not" in result.message.lower() or "must" in result.message.lower(),
            f"Nudge should warn against fabrication: {result.message}"
        )

    def test_safe_fallback_after_retries(self):
        """After max retries, the guardrail should return a safe fallback
        message that does NOT contain fabricated data."""
        from app.routers.agents import _check_hallucination_guardrail
        result = _check_hallucination_guardrail(
            user_message="show me sales data",
            data_ctx_extras={"bound_kb_ids": ["kb-1"]},
            tool_calls_made=[],
            iteration=0,
            guardrail_retries=2,
        )
        self.assertEqual(result.action, "fallback")
        # Fallback should be user-facing safe message
        self.assertNotIn("$", result.message)  # No fake currency
        self.assertNotIn("Acme", result.message)  # No fake company names


if __name__ == "__main__":
    unittest.main()
