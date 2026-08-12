"""Tests for the Default Skills system.

Covers:

* ``default_skills.DEFAULT_SKILLS`` — manifest shape, 6 entries, required keys
* ``default_skills.DEFAULT_SKILL_NAMES`` — frozenset matches DEFAULT_SKILLS values
* ``default_skills.pick_default_skill()`` — override, format-hint, soft-intent,
  and fallback paths
* ``default_skills.detect_soft_intent()`` — regex heuristic for keyword-less
  requests
* ``default_skills.is_override_skill()`` / ``has_override()`` — custom vs
  default skill discrimination
* ``default_skills.get_default_skills_list()`` — API-friendly list output
* ``intent_router.detect_file_intent`` — html and dashboard detection
* ``user_signal.EXPORT_SIGNALS`` — export_html and export_dashboard presence
"""

import unittest
from unittest.mock import patch

from app.services.synexia.default_skills import (
    DEFAULT_SKILLS,
    DEFAULT_SKILL_NAMES,
    pick_default_skill,
    detect_soft_intent,
    is_override_skill,
    has_override,
    get_default_skills_list,
)
from app.services.synexia.intent_router import (
    detect_file_intent,
    EXPORT_SIGNAL_BY_FORMAT,
    user_signal_for_format,
)
from app.services.synexia.user_signal import (
    is_export_signal,
    EXPORT_SIGNALS,
)


# ---------------------------------------------------------------------------
# 1. Manifest shape
# ---------------------------------------------------------------------------

class TestDefaultSkillsManifest(unittest.TestCase):
    """DEFAULT_SKILLS must have exactly 5 entries with the required keys.

    P0 remap: ``md`` was dropped (no Claude markdown skill exists; markdown
    requests fall through to the LLM). ``html`` and ``dashboard`` both map
    to the Claude ``artifacts-builder`` skill (it builds stateful React
    apps, covering both web pages and dashboards). ``docx``/``pptx``/``pdf``
    resolve from ``document-skills/`` by frontmatter name.
    """

    def test_has_five_entries(self):
        self.assertEqual(len(DEFAULT_SKILLS), 5)

    def test_all_keys_present(self):
        expected_formats = {"docx", "pptx", "pdf", "html", "dashboard"}
        self.assertEqual(set(DEFAULT_SKILLS.keys()), expected_formats)

    def test_each_entry_has_required_keys(self):
        required = {"skill_name", "triggers", "format"}
        for fmt_key, entry in DEFAULT_SKILLS.items():
            self.assertEqual(
                set(entry.keys()),
                required,
                f"Entry for '{fmt_key}' missing or has extra keys",
            )
            self.assertIsInstance(entry["triggers"], list, f"triggers for '{fmt_key}' must be list")
            self.assertGreater(len(entry["triggers"]), 0, f"triggers for '{fmt_key}' must not be empty")

    def test_default_skill_names_frozenset(self):
        expected_names = {entry["skill_name"] for entry in DEFAULT_SKILLS.values()}
        self.assertEqual(set(DEFAULT_SKILL_NAMES), expected_names)
        # dashboard now maps to the live dashboard-generation skill, so the
        # unique set is {docx, pptx, pdf, artifacts-builder, dashboard-generation}.
        self.assertEqual(len(DEFAULT_SKILL_NAMES), 5)
        self.assertIn("artifacts-builder", DEFAULT_SKILL_NAMES)
        self.assertIn("dashboard-generation", DEFAULT_SKILL_NAMES)


# ---------------------------------------------------------------------------
# 2. Override precedence
# ---------------------------------------------------------------------------

class TestOverridePrecedence(unittest.TestCase):
    """When the user picks a custom skill, defaults must be skipped."""

    def test_override_returns_none(self):
        """pick_default_skill returns None when active_skill is non-null."""
        custom_skill = {"name": "custom-pdf-tool", "description": "Custom PDF tool"}
        result = pick_default_skill("make me a docx report", active_skill=custom_skill)
        self.assertIsNone(result)

    def test_override_returns_none_even_with_format(self):
        """Even explicit file format keywords don't break the override."""
        custom_skill = {"name": "my-frontend-skill", "trigger": "/design"}
        result = pick_default_skill("export to pptx", active_skill=custom_skill)
        self.assertIsNone(result)

    def test_no_override_picks_default(self):
        """When no skill is active, pick_default_skill returns a default."""
        result = pick_default_skill("make me a docx report", active_skill=None)
        self.assertIsNotNone(result)
        self.assertEqual(result["skill_name"], "docx")
        self.assertEqual(result["format"], "docx")

    def test_strong_custom_skill_beats_generic_report_default(self):
        """A strong weekly-report skill match must beat broad report→docx routing."""
        forced = {
            "skill_name": "weekly-report-generation",
            "triggers": [],
            "format": None,
            "forced": True,
            "score": 0.92,
            "source": "db",
        }
        with patch("app.services.synexia.default_skills.post_router_pick", return_value=forced):
            result = pick_default_skill("make a weekly sales report", active_skill=None)
        self.assertEqual(result["skill_name"], "weekly-report-generation")
        self.assertTrue(result["forced"])


# ---------------------------------------------------------------------------
# 3. Format-hint detection
# ---------------------------------------------------------------------------

class TestPickDefaultSkillWithFormatHint(unittest.TestCase):
    """Explicit format keywords in the user message should pick the right default."""

    def test_docx_explicit(self):
        result = pick_default_skill("create a docx memo")
        self.assertEqual(result["skill_name"], "docx")

    def test_pptx_explicit(self):
        result = pick_default_skill("build a pptx presentation")
        self.assertEqual(result["skill_name"], "pptx")

    def test_pdf_explicit(self):
        result = pick_default_skill("as a PDF please")
        self.assertEqual(result["skill_name"], "pdf")

    def test_html_explicit(self):
        result = pick_default_skill("make me an html page")
        self.assertEqual(result["skill_name"], "artifacts-builder")

    def test_dashboard_explicit(self):
        result = pick_default_skill("build a dashboard")
        self.assertEqual(result["skill_name"], "dashboard-generation")

    def test_md_is_no_longer_a_default(self):
        # md was dropped from DEFAULT_SKILLS — markdown requests fall
        # through to the LLM (which writes markdown directly, no skill).
        result = pick_default_skill("write me a .md readme")
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# 4. Soft-intent heuristic
# ---------------------------------------------------------------------------

class TestSoftIntentDetection(unittest.TestCase):
    """When no explicit format keyword exists, soft signals pick the right default."""

    def test_report_triggers_docx(self):
        self.assertEqual(detect_soft_intent("make a sales report"), "docx")
        self.assertEqual(detect_soft_intent("write a quarterly report for the board"), "docx")

    def test_deck_triggers_pptx(self):
        self.assertEqual(detect_soft_intent("create a pitch deck for investors"), "pptx")
        self.assertEqual(detect_soft_intent("I need slides for tomorrow"), "pptx")

    def test_dashboard_triggers_dashboard(self):
        self.assertEqual(detect_soft_intent("show me project KPIs"), "dashboard")
        self.assertEqual(detect_soft_intent("create a metrics dashboard"), "dashboard")
        self.assertEqual(detect_soft_intent("build a KPI chart"), "dashboard")

    def test_readme_no_longer_forces_md(self):
        # md soft-intent was removed alongside the md default — "readme"/
        # "docs" must not force-map to anything; the LLM picks freely.
        self.assertIsNone(detect_soft_intent("write a README for this project"))
        self.assertIsNone(detect_soft_intent("add documentation"))

    def test_webpage_triggers_html(self):
        self.assertEqual(detect_soft_intent("build a web page"), "html")
        self.assertEqual(detect_soft_intent("create an interactive page"), "html")

    def test_no_soft_signal_returns_none(self):
        self.assertIsNone(detect_soft_intent("hello, how are you?"))
        self.assertIsNone(detect_soft_intent(""))
        self.assertIsNone(detect_soft_intent(None))


# ---------------------------------------------------------------------------
# 5. Fallback to docx
# ---------------------------------------------------------------------------

class TestFallbackToDocx(unittest.TestCase):
    """Ambiguous requests: soft-intent still maps to docx; truly generic
    requests return None so the LLM picks from the catalog."""

    def test_ambiguous_request_falls_back_to_docx(self):
        """'make a sales report' has no explicit format → fallback via soft-intent to docx."""
        result = pick_default_skill("make a sales report")
        # "sales report" contains "report" → soft intent maps to docx
        self.assertEqual(result["skill_name"], "docx")

    def test_totally_ambiguous_returns_none(self):
        """Totally ambiguous requests return None → LLM picks from catalog."""
        result = pick_default_skill("do something for me")
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# 6. intent_router HTML + Dashboard
# ---------------------------------------------------------------------------

class TestIntentRouterNewFormats(unittest.TestCase):
    """detect_file_intent must recognize 'html' and 'dashboard'."""

    def test_html_web_page(self):
        self.assertEqual(detect_file_intent("make a web page"), "html")

    def test_html_dot_html(self):
        self.assertEqual(detect_file_intent("create an .html file"), "html")

    def test_dashboard_keyword(self):
        self.assertEqual(detect_file_intent("build a dashboard please"), "dashboard")

    def test_kpi_dashboard(self):
        self.assertEqual(detect_file_intent("kpi dashboard for Q2"), "dashboard")

    def test_export_signals_cover_html_and_dashboard(self):
        self.assertIn("export_html", EXPORT_SIGNALS)
        self.assertIn("export_dashboard", EXPORT_SIGNALS)
        self.assertTrue(is_export_signal("export_html"))
        self.assertTrue(is_export_signal("export_dashboard"))

    def test_user_signal_for_format_new(self):
        self.assertEqual(user_signal_for_format("html"), "export_html")
        self.assertEqual(user_signal_for_format("dashboard"), "export_dashboard")


# ---------------------------------------------------------------------------
# 7. is_override_skill / has_override
# ---------------------------------------------------------------------------

class TestIsOverrideSkill(unittest.TestCase):
    """is_override_skill discriminates custom skills from default skills."""

    def test_custom_skill_is_override(self):
        custom = {"name": "my-custom-skill", "trigger": "/custom"}
        self.assertTrue(is_override_skill(custom))
        self.assertTrue(has_override(custom))

    def test_default_skill_is_not_override(self):
        default = {"name": "docx", "trigger": "/docx"}
        self.assertFalse(is_override_skill(default))
        self.assertFalse(has_override(default))

    def test_none_is_not_override(self):
        self.assertFalse(is_override_skill(None))
        self.assertFalse(has_override(None))

    def test_empty_dict_is_not_override(self):
        self.assertFalse(is_override_skill({}))
        self.assertFalse(is_override_skill({"name": ""}))

    def test_default_skill_name_is_not_override(self):
        for name in DEFAULT_SKILL_NAMES:
            self.assertFalse(is_override_skill({"name": name}), f"'{name}' should not be override")


# ---------------------------------------------------------------------------
# 8. get_default_skills_list
# ---------------------------------------------------------------------------

class TestGetDefaultSkillsList(unittest.TestCase):
    """get_default_skills_list returns the correct API-friendly list."""

    def test_returns_five_items(self):
        skills_list = get_default_skills_list()
        self.assertEqual(len(skills_list), 5)

    def test_each_item_has_expected_keys(self):
        skills_list = get_default_skills_list()
        for item in skills_list:
            self.assertIn("skill_name", item)
            self.assertIn("triggers", item)
            self.assertIn("format", item)
            self.assertIsInstance(item["triggers"], list)

    def test_all_format_keys_present(self):
        skills_list = get_default_skills_list()
        formats = {item["format"] for item in skills_list}
        self.assertEqual(formats, {"docx", "pptx", "pdf", "html", "dashboard"})


if __name__ == "__main__":
    unittest.main()
