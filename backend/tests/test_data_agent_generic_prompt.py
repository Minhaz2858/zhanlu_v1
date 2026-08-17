"""Regression tests for the Data Agent generic schema-discovery fix.

The Data Agent must work across ANY database, so its prompt must:
  1. Mandate schema-first behaviour (call `describe_schema` before any SQL).
  2. Mandate a query-failure fallback (re-check schema + re-query when a query
     returns zero rows or NULL in expected columns, instead of reporting
     "date coverage: None" as a final answer).
  3. Allow self-correction even after a data-returning query when the data is
     unusable (all-NULL / zero rows).
  4. Contain ZERO hardcoded table or column names.

The main agent's `_SCHEMA_AWARE_PROTOCOL_BLOCK` (injected into
ecisco_bi_assistant) must likewise contain zero hardcoded column names
(e.g. FPRODUCEDATE / FUPDATETIME).

Follows the existing unittest patterns in test_agent_autonomy_contract.py.
"""

import os
import sys
import unittest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


# Hardcoded domain-specific table/column names that MUST NOT appear in the
# generic Data Agent prompt. This list is intentionally concrete — any of these
# appearing would mean the agent is being nudged toward one specific database.
_HARDCODED_SCHEMA_MARKERS = [
    # Tables
    "erp_v_stk_inventory",
    "erp_t_stk_instock",
    "erp_t_sal_outstock",
    "erp_product_sales_details",
    # Columns
    "FUPDATETIME",
    "FPRODUCEDATE",
    "FDATE",
    "IN_DATE",
    "OUT_DATE",
    "FMATERIALID",
    "FNAME",
    "material_name",
]


class TestDataAgentGenericPrompt(unittest.TestCase):
    """DATA_AGENT_PROMPT must enforce schema discovery and contain no
    hardcoded table/column names."""

    @classmethod
    def setUpClass(cls):
        from app.services.agent_definitions import DATA_AGENT_PROMPT
        cls.prompt = DATA_AGENT_PROMPT

    def test_schema_first_hard_rule_present(self):
        self.assertIn("SCHEMA-FIRST HARD RULE", self.prompt)
        self.assertIn("Before writing ANY SQL, call `describe_schema`", self.prompt)
        self.assertIn("NEVER assume or invent table or column names", self.prompt)
        self.assertIn("using ONLY the column names the schema actually returned", self.prompt)

    def test_query_failure_fallback_present(self):
        self.assertIn("QUERY FAILURE FALLBACK", self.prompt)
        self.assertIn("Call `describe_schema` on the table again", self.prompt)
        self.assertIn("Re-execute immediately", self.prompt)
        # The agent must NOT report null coverage as a final answer.
        self.assertIn('Do NOT report "date coverage: None" or "0 rows"', self.prompt)

    def test_stop_after_data_rule_has_self_correction_exception(self):
        # The original CRITICAL stop-after-data rule is preserved...
        self.assertIn("stop calling tools and write your prose", self.prompt)
        # ...but now has a self-correction exception for unusable data.
        self.assertIn("EXCEPTION", self.prompt)
        self.assertIn("BAD DATA SELF-CORRECTION", self.prompt)
        self.assertIn("re-execute once", self.prompt)

    def test_no_hardcoded_table_or_column_names(self):
        lowered = self.prompt.lower()
        for marker in _HARDCODED_SCHEMA_MARKERS:
            self.assertNotIn(marker.lower(), lowered,
                             f"DATA_AGENT_PROMPT contains hardcoded schema marker {marker!r}")

class TestSchemaAwareProtocolBlockGeneric(unittest.TestCase):
    """The main agent's schema-aware protocol block must use generic
    placeholder examples, not real column names."""

    def test_no_hardcoded_column_names_in_protocol_block(self):
        from app.services.agent_prompts import _SCHEMA_AWARE_PROTOCOL_BLOCK
        for marker in ("FPRODUCEDATE", "FUPDATETIME", "FDATE", "IN_DATE", "OUT_DATE"):
            self.assertNotIn(marker, _SCHEMA_AWARE_PROTOCOL_BLOCK,
                             f"_SCHEMA_AWARE_PROTOCOL_BLOCK contains hardcoded column {marker!r}")

    def test_protocol_block_still_enforces_auto_correction(self):
        from app.services.agent_prompts import _SCHEMA_AWARE_PROTOCOL_BLOCK
        # The rule logic survives: auto-select alternative column + proceed.
        self.assertIn("DATA QUALITY REPORTER", _SCHEMA_AWARE_PROTOCOL_BLOCK)
        self.assertIn("Automatically select the best alternative column", _SCHEMA_AWARE_PROTOCOL_BLOCK)
        self.assertIn("NEVER stop execution to ask permission", _SCHEMA_AWARE_PROTOCOL_BLOCK)
        self.assertIn("NEVER HALLUCINATE COLUMNS", _SCHEMA_AWARE_PROTOCOL_BLOCK)


if __name__ == "__main__":
    unittest.main()
