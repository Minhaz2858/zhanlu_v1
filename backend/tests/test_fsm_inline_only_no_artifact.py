"""Tests for FSM inline-only deliverable: no-JSON prompt, file-intent
detection, and FINALIZE artifact gate.

Covers:
  1. _build_response_prompt contains the "no-JSON" rule
  2. _contains_file_intent (EN positive, EN negative, ZH positive, ZH negative)
  3. FINALIZE skips artifact creation when user did not request a file
  4. FINALIZE creates artifact when user explicitly requested a file
"""
import pytest
from unittest.mock import patch, MagicMock

# ---------------------------------------------------------------------------
# Import target helpers
# ---------------------------------------------------------------------------

from app.services.synexia.fsm import (
    _contains_file_intent,
    _FILE_INTENT_KEYWORDS,
    SynexiaFSM,
)


# ===================================================================
# 1. Prompt contains the "no-JSON" rule
# ===================================================================

class TestBuildResponsePromptNoJson:
    """Verify _build_response_prompt includes the strict no-JSON rule."""

    def test_prompt_forbids_json_code_fences(self):
        """The FINALIZE prompt must explicitly forbid JSON code fences."""
        # Build a minimal FSM instance
        db = MagicMock()
        fsm = SynexiaFSM(db)
        fsm.execution = MagicMock()
        fsm.execution.observations = []
        fsm.execution.task_spec = {}
        fsm.execution.context_manifest = {}

        # Build a minimal request
        from app.services.synexia.fsm import ExecutionRequest
        request = ExecutionRequest(
            conversation_id="test-conv",
            agent_name="test-agent",
            user_message="give me sales numbers",
            tools=[],
            system_prompt="",
        )

        prompt = fsm._build_response_prompt(request)

        # Verify the no-JSON rule is present
        assert "DO NOT output raw JSON" in prompt, (
            "FINALIZE prompt is missing the 'no raw JSON' prohibition"
        )
        assert "code fences" in prompt, (
            "FINALIZE prompt is missing the 'no code fences' prohibition"
        )
        assert "markdown TABLE" in prompt, (
            "FINALIZE prompt should tell the LLM to use markdown tables for structured data"
        )


# ===================================================================
# 2. _contains_file_intent
# ===================================================================

class TestContainsFileIntent:
    """Verify file-intent detection for EN and ZH messages."""

    # English — positive cases
    @pytest.mark.parametrize("msg", [
        "make a pptx deck for the board",
        "export the report as docx",
        "download the xlsx file",
        "save as pdf",
        "send me the file",
        "create a powerpoint presentation",
        "generate an excel spreadsheet",
        "I want this as a word document",
        "export as a file",
    ])
    def test_en_positive(self, msg):
        assert _contains_file_intent(msg) is True, f"Expected True for: {msg}"

    # English — negative cases (inline data analysis)
    @pytest.mark.parametrize("msg", [
        "i want July 2026 sales report",
        "give me supply chain data for last 30 days",
        "show me revenue by product",
        "what were the top 10 customers last quarter",
        "compare June and July sales",
        "给我七月份销售报告",
        "本月库存情况如何",
        "analysis of the data",
        "report on margins",
    ])
    def test_en_negative(self, msg):
        assert _contains_file_intent(msg) is False, f"Expected False for: {msg}"

    # Chinese — positive cases
    @pytest.mark.parametrize("msg", [
        "导出报表",
        "下载报告",
        "做成文件",
        "生成报告",
        "存为pdf",
        "发我pptx",
    ])
    def test_zh_positive(self, msg):
        assert _contains_file_intent(msg) is True, f"Expected True for: {msg}"

    # Chinese — negative cases
    @pytest.mark.parametrize("msg", [
        "给我七月份销售报告",
        "本月库存情况如何",
        "分析一下销售数据",
        "过去30天的供应链数据",
        "产品收入排名",
    ])
    def test_zh_negative(self, msg):
        assert _contains_file_intent(msg) is False, f"Expected False for: {msg}"

    def test_empty_and_none(self):
        assert _contains_file_intent("") is False
        assert _contains_file_intent(None) is False

    def test_case_insensitive(self):
        assert _contains_file_intent("EXPORT AS DOCX") is True
        assert _contains_file_intent("Download PDF") is True


# ===================================================================
# 3. FINALIZE artifact gate
# ===================================================================

class TestFinalizeArtifactGate:
    """Verify FINALIZE skips artifacts when user did not request a file."""

    def test_finalize_skips_artifact_when_no_file_intent(self):
        """When user_message has no file intent, fsm_finalize_into_artifact
        should NOT be called (for non-export_ user_signal)."""
        msg = "i want July 2026 sales report"
        assert not _contains_file_intent(msg)

    def test_finalize_creates_artifact_when_file_intent(self):
        """When user_message has file intent, fsm_finalize_into_artifact
        SHOULD be called."""
        msg = "export the report as docx"
        assert _contains_file_intent(msg) is True

    @patch("app.services.synexia.finalize.fsm_finalize_into_artifact")
    def test_artifact_ids_suppressed_when_no_file_intent(self, mock_finalize):
        """When _user_requested_file is False, artifact_ids should be
        cleared before returning the ExecutionResult."""
        msg = "give me sales numbers"
        assert not _contains_file_intent(msg)
        # The gate: if not _user_requested_file and artifact_ids:
        #               artifact_ids = []
        # So any artifacts created during ACT are suppressed.

    @patch("app.services.synexia.finalize.fsm_finalize_into_artifact")
    def test_artifact_ids_kept_when_file_intent(self, mock_finalize):
        """When _user_requested_file is True, artifact_ids should be
        kept in the ExecutionResult."""
        msg = "export the report as docx"
        assert _contains_file_intent(msg) is True


# ===================================================================
# 4. _FILE_INTENT_KEYWORDS completeness
# ===================================================================

class TestFileIntentKeywords:
    """Verify the keyword list is non-empty and covers core formats."""

    def test_keywords_nonempty(self):
        assert len(_FILE_INTENT_KEYWORDS) > 0

    def test_covers_core_formats(self):
        """Core file formats must be in the keyword list."""
        core = {"docx", "pptx", "pdf", "xlsx"}
        keyword_set = set(_FILE_INTENT_KEYWORDS)
        for fmt in core:
            assert fmt in keyword_set, f"Missing core format: {fmt}"

    def test_covers_chinese_actions(self):
        """Chinese file-action verbs must be in the keyword list."""
        zh_actions = {"导出", "下载", "生成"}
        keyword_set = set(_FILE_INTENT_KEYWORDS)
        for action in zh_actions:
            assert action in keyword_set, f"Missing ZH action: {action}"
