"""Unit tests for the Agent Autonomy Contract — defense against agents
that push technical work (pip install, share schema, export CSV) onto the
user instead of solving the problem autonomously.

Covers 4 layers:
  1. Contract block `_AUTONOMY_CONTRACT_BLOCK` present in every builtin system prompt
  2. Forbidden autonomy-violating phrases absent from every prompt
  3. `_ALLOWED_IMPORTS` in code_execution_tool includes DB/data-science modules
  4. Public `assert_no_autonomy_violations(text)` helper for integration tests

These tests follow the existing unittest patterns in test_prompt_tools_normalization.py
and test_anti_hallucination_guardrails.py.
"""

import os
import sys
import unittest

# Make `app` importable
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


# ---------------------------------------------------------------------------
# Forbidden-phrase patterns (regex)
# ---------------------------------------------------------------------------
# These are patterns that, if they appear in a system prompt as a directive
# TO THE USER, indicate the agent is shirking autonomy. We use a broad set
# to catch future variations.
#
# IMPORTANT: The contract block itself mentions "pip install X" / "share the
# schema" as EXAMPLES of what NOT to do. The _strip_contract_block() helper
# removes the contract block before scanning, so those examples don't trigger
# false positives. The forbidden patterns are designed to catch the agent
# OUTPUTTING these phrases as directives to the user.
_FORBIDDEN_PATTERNS = [
    r"pip\s+install\s+[a-zA-Z0-9_\-]+",          # "pip install pymysql"
    r"please\s+share\s+the\s+(table|schema)",     # "please share the table structure"
    r"export\s+a\s+(csv|file|spreadsheet)",       # "export a CSV"
    r"share\s+the\s+(database\s+)?(schema|table\s+structure|column\s+names)",  # "share the column names"
    r"install\s+[a-z\-]+\s+(in|on)\s+(the|your)\s+(environment|machine|system|server)",  # "install X in the environment"
    r"run\s+(this|the)\s+(SQL\s+query|SQL|query|script)\s+(manually|yourself|for\s+you)",  # "run this SQL query manually"
    r"you\s+can\s+(install|run|execute)\s+this",   # "you can run this SQL manually"
]


# ---------------------------------------------------------------------------
# Autonomy Contract — the canonical block that must appear in every prompt
# ---------------------------------------------------------------------------
_AUTONOMY_CONTRACT_BLOCK = (
    "\n\n"
    "AUTONOMY CONTRACT (HARD RULE — you MUST follow this)\n"
    "- You are an autonomous worker. The user is NOT a sysadmin, DBA, or developer.\n"
    "- NEVER ask the user to install packages, share credentials, export CSVs, "
    "or run SQL manually. Those are YOUR job.\n"
    "- NEVER tell the user \"pip install X\", \"share the schema\", "
    "\"export the data\", or any equivalent.\n"
    "- If you hit a capability gap, solve it yourself in one of these ways "
    "(in priority order):\n"
    "  1. Install the needed dependency inside the sandbox (pip install in execute_code).\n"
    "  2. Use `ask_data_agent` if a database is connected.\n"
    "  3. Try alternative drivers or approaches.\n"
    "  4. As a LAST resort only: ask the user in PLAIN LANGUAGE "
    "(\"Can you paste a sample of the data, or upload a CSV?\") — "
    "never mention pip, apt, brew, npm, or any package manager.\n"
    "- Under NO circumstances should you emit a numbered list of "
    "technical setup tasks for the user to complete."
)


# ---------------------------------------------------------------------------
# Public helper for integration tests
# ---------------------------------------------------------------------------

def assert_no_autonomy_violations(text: str):
    """Assert that `text` contains no forbidden autonomy-violating phrases.

    Raises AssertionError with the offending pattern and match context
    if a violation is found. Idempotent — safe to call multiple times
    on the same text.

    The AUTONOMY CONTRACT section (if present) is stripped before scanning,
    because the contract itself contains examples of forbidden phrases
    (e.g. "NEVER tell the user 'pip install X'") as counter-examples.

    Usage in integration tests::

        from tests.test_agent_autonomy_contract import assert_no_autonomy_violations
        assert_no_autonomy_violations(assistant_response)
    """
    import re

    if not text or not isinstance(text, str):
        return  # empty/non-string is trivially clean

    # Strip the AUTONOMY CONTRACT section so examples-of-what-not-to-do
    # inside the contract block don't trigger false positives.
    text = _strip_contract_block(text)

    violations = []
    for pattern in _FORBIDDEN_PATTERNS:
        matches = list(re.finditer(pattern, text, re.IGNORECASE))
        for m in matches:
            start = max(0, m.start() - 30)
            end = min(len(text), m.end() + 30)
            context = text[start:end]
            violations.append(f"  Pattern: {pattern!r}\n  Match:   {m.group()!r}\n  Context: ...{context}...")

    if violations:
        msg = (
            f"Found {len(violations)} autonomy violation(s) in agent text:\n"
            + "\n".join(violations)
        )
        raise AssertionError(msg)


def _strip_contract_block(text: str) -> str:
    """Remove the AUTONOMY CONTRACT section from prompt text.

    The contract block starts with 'AUTONOMY CONTRACT' and continues until
    the next non-indented section header or double-newline-terminated end.
    Splits on the contract marker and returns only the parts before/after it.
    """
    import re

    # Pattern: "AUTONOMY CONTRACT" through a blank line before the next
    # section that starts with a word (like "NO HALLUCINATION", "OPERATING",
    # "TOOLS", etc.) or EOF.
    pattern = r'\n\nAUTONOMY CONTRACT \(HARD RULE.*?(?=\n\n(?:[A-Z][A-Z\s]+\n|$|OPERATING|TOOLS|RESPONSE|INTENT|FILE|HANDLING|Same|When|Always|Never|For|Use))'
    return re.sub(pattern, '', text, flags=re.DOTALL)


# ---------------------------------------------------------------------------
# Layer 1: Contract block presence in every builtin prompt
# ---------------------------------------------------------------------------

class TestAutonomyContractPresent(unittest.TestCase):
    """Every system prompt (builtin + 5-layer user-agent assembly) must
    contain the autonomy contract block."""

    @classmethod
    def setUpClass(cls):
        """Import the canonical contract block from the source module."""
        from app.services.agent_prompts import _AUTONOMY_CONTRACT_BLOCK
        cls.contract_block = _AUTONOMY_CONTRACT_BLOCK

    def data_agent_contract_is_embedded(prompt_text, label="prompt"):
        """The DATA_AGENT_PROMPT contract uses 'calling agent' not 'user'."""
        if "AUTONOMY CONTRACT (HARD RULE" in prompt_text:
            # Check for data-agent-specific wording
            if "NEVER ask the calling agent" in prompt_text:
                return True
            if "NEVER ask the user" in prompt_text:
                return True
        return False

    def _assert_contract_present(self, prompt_text, label="prompt"):
        """Assert that `prompt_text` contains the autonomy contract block."""
        self.assertIn("AUTONOMY CONTRACT (HARD RULE", prompt_text,
                      f"{label}: missing AUTONOMY CONTRACT header")
        # Check for either "user" or "calling agent" variant
        has_user = "NEVER ask the user to install" in prompt_text
        has_caller = "NEVER ask the calling agent to install" in prompt_text
        self.assertTrue(has_user or has_caller,
                        f"{label}: missing 'NEVER ask the user/calling agent to install' clause")
        # "NEVER tell" appears in both variants
        has_tell_user = "NEVER tell the user" in prompt_text
        has_tell_caller = "NEVER tell the caller" in prompt_text
        self.assertTrue(has_tell_user or has_tell_caller,
                        f"{label}: missing 'NEVER tell the user/caller' clause")
        self.assertIn("If you hit a capability gap", prompt_text,
                      f"{label}: missing 'If you hit a capability gap' section")

    def test_generic_agent_prompt_has_contract(self):
        from app.services.agent_prompts import GENERIC_AGENT_SYSTEM_PROMPT
        self._assert_contract_present(GENERIC_AGENT_SYSTEM_PROMPT, "GENERIC")

    def test_general_assistant_prompt_has_contract(self):
        from app.services.agent_prompts import GENERAL_ASSISTANT_SYSTEM_PROMPT
        self._assert_contract_present(GENERAL_ASSISTANT_SYSTEM_PROMPT, "GENERAL_ASSISTANT")

    def test_power_user_prompt_has_contract(self):
        from app.services.agent_prompts import POWER_USER_SYSTEM_PROMPT
        self._assert_contract_present(POWER_USER_SYSTEM_PROMPT, "POWER_USER")

    def test_data_agent_prompt_has_contract(self):
        from app.services.agent_definitions import DATA_AGENT_PROMPT
        self._assert_contract_present(DATA_AGENT_PROMPT, "DATA_AGENT")

    def test_assemble_user_agent_prompt_includes_contract(self):
        """The 5-layer user-agent assembly must append the contract."""
        from app.services.agent_prompts import assemble_user_agent_prompt

        class FakeAgent:
            name = "Sales Agent"
            description = "Sales data analyst"
            prompt_identity = "You are a sales analyst."
            prompt_boundary = "Read-only data access."
            prompt_reasoning = "Analyze then report."
            prompt_tools = "Use ask_data_agent for DB queries."
            prompt_output = "Lead with the answer."
            capabilities = ["data_analysis"]

        prompt = assemble_user_agent_prompt(FakeAgent())
        self._assert_contract_present(prompt, "assemble_user_agent_prompt()")


# ---------------------------------------------------------------------------
# Layer 2: Forbidden phrases absent from every builtin prompt
# ---------------------------------------------------------------------------

class TestForbiddenPhrasesAbsent(unittest.TestCase):
    """No system prompt (builtin or user-agent assembled) may contain
    autonomy-violating phrases (pip install, share schema, export CSV)."""

    @classmethod
    def setUpClass(cls):
        from app.services.agent_prompts import (
            GENERIC_AGENT_SYSTEM_PROMPT,
            GENERAL_ASSISTANT_SYSTEM_PROMPT,
            POWER_USER_SYSTEM_PROMPT,
            AGENT_BUILDER_SYSTEM_PROMPT,
            SKILL_AGENT_SYSTEM_PROMPT,
            AUTOMATION_AGENT_SYSTEM_PROMPT,
        )
        from app.services.agent_definitions import (
            DATA_AGENT_PROMPT,
            GENERAL_PURPOSE_PROMPT,
            EXPLORE_PROMPT,
            PLAN_PROMPT,
            WORKER_PROMPT,
            VERIFICATION_PROMPT,
        )
        cls._all_prompts = {
            "GENERIC": GENERIC_AGENT_SYSTEM_PROMPT,
            "GENERAL_ASSISTANT": GENERAL_ASSISTANT_SYSTEM_PROMPT,
            "POWER_USER": POWER_USER_SYSTEM_PROMPT,
            "AGENT_BUILDER": AGENT_BUILDER_SYSTEM_PROMPT,
            "SKILL_AGENT": SKILL_AGENT_SYSTEM_PROMPT,
            "AUTOMATION_AGENT": AUTOMATION_AGENT_SYSTEM_PROMPT,
            "DATA_AGENT": DATA_AGENT_PROMPT,
            "GENERAL_PURPOSE": GENERAL_PURPOSE_PROMPT,
            "EXPLORE": EXPLORE_PROMPT,
            "PLAN": PLAN_PROMPT,
            "WORKER": WORKER_PROMPT,
            "VERIFICATION": VERIFICATION_PROMPT,
        }

    def _check_no_forbidden_phrases(self, prompt_text):
        """Run assert_no_autonomy_violations and wrap the error with a useful label."""
        if not prompt_text:
            return
        assert_no_autonomy_violations(prompt_text)

    def test_generic_no_forbidden(self):
        self._check_no_forbidden_phrases(self._all_prompts["GENERIC"])

    def test_general_assistant_no_forbidden(self):
        self._check_no_forbidden_phrases(self._all_prompts["GENERAL_ASSISTANT"])

    def test_power_user_no_forbidden(self):
        self._check_no_forbidden_phrases(self._all_prompts["POWER_USER"])

    def test_agent_builder_no_forbidden(self):
        self._check_no_forbidden_phrases(self._all_prompts["AGENT_BUILDER"])

    def test_skill_agent_no_forbidden(self):
        self._check_no_forbidden_phrases(self._all_prompts["SKILL_AGENT"])

    def test_automation_agent_no_forbidden(self):
        self._check_no_forbidden_phrases(self._all_prompts["AUTOMATION_AGENT"])

    def test_data_agent_no_forbidden(self):
        self._check_no_forbidden_phrases(self._all_prompts["DATA_AGENT"])

    def test_general_purpose_no_forbidden(self):
        self._check_no_forbidden_phrases(self._all_prompts["GENERAL_PURPOSE"])

    def test_explore_no_forbidden(self):
        self._check_no_forbidden_phrases(self._all_prompts["EXPLORE"])

    def test_plan_no_forbidden(self):
        self._check_no_forbidden_phrases(self._all_prompts["PLAN"])

    def test_worker_no_forbidden(self):
        self._check_no_forbidden_phrases(self._all_prompts["WORKER"])

    def test_verification_no_forbidden(self):
        self._check_no_forbidden_phrases(self._all_prompts["VERIFICATION"])

    def test_user_agent_assembly_no_forbidden(self):
        """The assembled 5-layer user prompt must not introduce forbidden phrases."""
        from app.services.agent_prompts import assemble_user_agent_prompt

        class FakeAgent:
            name = "Test Agent"
            description = "Test"
            prompt_identity = "You are a test agent."
            prompt_boundary = "Test boundary."
            prompt_reasoning = "Test reasoning."
            prompt_tools = "Test tools."
            prompt_output = "Test output."
            capabilities = ["test"]

        prompt = assemble_user_agent_prompt(FakeAgent())
        self._check_no_forbidden_phrases(prompt)


# ---------------------------------------------------------------------------
# Layer 3: ALLOWED_IMPORTS includes DB and data-science modules
# ---------------------------------------------------------------------------

class TestAllowedImportsExpanded(unittest.TestCase):
    """The execute_code sandbox ALLOWED_IMPORTS must include the DB drivers
    and data-science modules needed for autonomous data analysis."""

    _REQUIRED_IMPORTS = {
        "pymysql",
        "mysql",
        "sqlalchemy",
        "pandas",
        "numpy",
    }

    def test_required_db_imports_are_allowed(self):
        from app.services.tool_handlers.code_execution_tool import _ALLOWED_IMPORTS
        for module in self._REQUIRED_IMPORTS:
            self.assertIn(module, _ALLOWED_IMPORTS,
                          f"{module!r} must be in _ALLOWED_IMPORTS for autonomous DB access")

    def test_stdlib_modules_still_allowed(self):
        """Existing stdlib imports must remain after expansion."""
        from app.services.tool_handlers.code_execution_tool import _ALLOWED_IMPORTS
        stdlib_expected = {"math", "json", "re", "collections", "itertools",
                           "functools", "datetime", "decimal", "statistics", "string"}
        for module in stdlib_expected:
            self.assertIn(module, _ALLOWED_IMPORTS,
                          f"stdlib {module!r} must remain in _ALLOWED_IMPORTS")

    def test_dangerous_modules_still_blocked(self):
        """Security-critical modules must NOT be in the whitelist."""
        from app.services.tool_handlers.code_execution_tool import _ALLOWED_IMPORTS
        blocked = {"os", "sys", "subprocess", "shutil", "socket", "requests",
                   "http", "urllib", "ftplib", "telnetlib", "smtplib", "poplib",
                   "ctypes", "multiprocessing", "signal", "pty"}
        for module in blocked:
            self.assertNotIn(module, _ALLOWED_IMPORTS,
                             f"dangerous module {module!r} must NOT be in _ALLOWED_IMPORTS")


# ---------------------------------------------------------------------------
# Layer 4: Public helper smoke test
# ---------------------------------------------------------------------------

class TestAssertNoAutonomyViolationsHelper(unittest.TestCase):
    """Smoke tests for the public assert_no_autonomy_violations() helper."""

    def test_clean_text_passes(self):
        """Clean text should not raise."""
        assert_no_autonomy_violations("Here is your sales report. Revenue is up 12%.")

    def test_pip_install_detected(self):
        """'pip install pymysql' in agent text is a violation."""
        with self.assertRaises(AssertionError) as ctx:
            assert_no_autonomy_violations(
                "I can't query the database. Please run: pip install pymysql"
            )
        self.assertIn("autonomy violation", str(ctx.exception))

    def test_share_schema_detected(self):
        """'please share the schema' is a violation."""
        with self.assertRaises(AssertionError):
            assert_no_autonomy_violations(
                "Option 2: Please share the table structure and column names."
            )

    def test_export_csv_detected(self):
        """'export a CSV' is a violation."""
        with self.assertRaises(AssertionError):
            assert_no_autonomy_violations(
                "Option 3: Export a CSV of the data and I'll analyze it."
            )

    def test_run_sql_manually_detected(self):
        """'run this SQL query manually' is a violation."""
        with self.assertRaises(AssertionError):
            assert_no_autonomy_violations(
                "Please run this SQL query manually in your MySQL client."
            )

    def test_install_in_environment_detected(self):
        """'install X in the environment' is a violation."""
        with self.assertRaises(AssertionError):
            assert_no_autonomy_violations(
                "You need to install pymysql in the environment first."
            )

    def test_empty_text_passes(self):
        """Empty or None text is trivially clean."""
        assert_no_autonomy_violations("")
        assert_no_autonomy_violations(None)

    def test_legitimate_mention_ok(self):
        """Text that mentions CSV in a non-directive way should not raise
        (e.g. 'The data is available as CSV download')."""
        # "export a CSV" pattern should NOT match "available as CSV" —
        # the pattern is: export\s+a\s+(csv|file|spreadsheet)
        assert_no_autonomy_violations(
            "The results are available for download as a CSV file."
        )


if __name__ == "__main__":
    unittest.main()
