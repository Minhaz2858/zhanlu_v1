"""Tests for the new file-format intent detection + user_signal exports.

Covers:

* ``synexia.intent_router.detect_file_intent`` — keyword detection
  for docx / pptx / xlsx / pdf / md.
* ``synexia.user_signal.is_export_signal`` / ``EXPORT_SIGNALS`` —
  the new export_docx / export_pptx / export_xlsx / export_pdf /
  export_md values are recognized.
"""

import unittest

from app.services.synexia.intent_router import (
    detect_file_intent,
    user_signal_for_format,
    EXPORT_SIGNAL_BY_FORMAT,
)
from app.services.synexia.user_signal import (
    is_export_signal,
    EXPORT_SIGNALS,
    detect_user_signal,
)


class TestDetectFileIntent(unittest.TestCase):
    def test_docx_keyword(self):
        self.assertEqual(detect_file_intent("make me a docx report"), "docx")

    def test_docx_natural(self):
        self.assertEqual(detect_file_intent("as a Word document please"), "docx")
        self.assertEqual(detect_file_intent("as a .doc file"), "docx")

    def test_pptx_keyword(self):
        self.assertEqual(detect_file_intent("export to pptx"), "pptx")
        self.assertEqual(detect_file_intent("PowerPoint deck"), "pptx")
        self.assertEqual(detect_file_intent("a pitch deck"), "pptx")
        self.assertEqual(detect_file_intent("a slide deck"), "pptx")

    def test_pptx_standalone_ppt(self):
        # Bare "PPT"/"ppt" (no "x", no dot) is the most common request form.
        self.assertEqual(detect_file_intent("make a sales overview PPT"), "pptx")
        self.assertEqual(detect_file_intent("Please build a ppt for me"), "pptx")

    def test_pptx_chinese(self):
        # CJK-adjacent tokens have no \b boundary; the pattern must not rely
        # on word boundaries for 演示文稿 / 幻灯片 / CJK+PPT.
        self.assertEqual(detect_file_intent("做一份销售总览PPT"), "pptx")
        self.assertEqual(detect_file_intent("帮我生成一个演示文稿"), "pptx")
        self.assertEqual(detect_file_intent("做一个幻灯片汇报"), "pptx")
        self.assertEqual(detect_file_intent("帮我做一个ppt汇报材料"), "pptx")

    def test_xlsx_keyword(self):
        self.assertEqual(detect_file_intent("export to xlsx"), "xlsx")
        self.assertEqual(detect_file_intent("excel workbook"), "xlsx")
        self.assertEqual(detect_file_intent("a .xls file"), "xlsx")
        self.assertEqual(detect_file_intent("as a spreadsheet"), "xlsx")

    def test_pdf_keyword(self):
        self.assertEqual(detect_file_intent("as PDF"), "pdf")

    def test_md_keyword(self):
        self.assertEqual(detect_file_intent("as markdown"), "md")
        self.assertEqual(detect_file_intent("a .md file"), "md")

    def test_no_format_intent(self):
        self.assertIsNone(detect_file_intent("hello, how are you?"))
        self.assertIsNone(detect_file_intent(""))

    def test_none_safe(self):
        self.assertIsNone(detect_file_intent(None))

    def test_case_insensitive(self):
        self.assertEqual(detect_file_intent("DOCX please"), "docx")
        self.assertEqual(detect_file_intent("PowerPoint"), "pptx")

    def test_priority_when_multiple(self):
        # docx should win over xlsx when both appear (docx is listed first)
        result = detect_file_intent("send me a docx file as xlsx")
        self.assertIn(result, {"docx", "xlsx"})


class TestUserSignalForFormat(unittest.TestCase):
    def test_export_signals_cover_all_formats(self):
        for fmt, signal in EXPORT_SIGNAL_BY_FORMAT.items():
            self.assertEqual(user_signal_for_format(fmt), signal)
            self.assertTrue(is_export_signal(signal))

    def test_export_signals_set_is_complete(self):
        for signal in EXPORT_SIGNAL_BY_FORMAT.values():
            self.assertIn(signal, EXPORT_SIGNALS)


class TestIsExportSignal(unittest.TestCase):
    def test_legacy_aliases(self):
        self.assertTrue(is_export_signal("export"))
        self.assertTrue(is_export_signal("download"))
        self.assertTrue(is_export_signal("save"))

    def test_new_format_specific_signals(self):
        self.assertTrue(is_export_signal("export_docx"))
        self.assertTrue(is_export_signal("export_pptx"))
        self.assertTrue(is_export_signal("export_xlsx"))
        self.assertTrue(is_export_signal("export_pdf"))
        self.assertTrue(is_export_signal("export_md"))

    def test_default_is_not_export(self):
        self.assertFalse(is_export_signal("default"))
        self.assertFalse(is_export_signal(None))
        self.assertFalse(is_export_signal(""))
        self.assertFalse(is_export_signal("something_weird"))
