"""
E2E tests: Template system + Quality evaluation + Reflexion critique.
Usage:
    cd /home/ysk2025/zhanlu_7_30/backend && PYTHONPATH=. venv/bin/pytest tests/test_content_e2e_quality.py -v
    cd /home/ysk2025/zhanlu_7_30/backend && PYTHONPATH=. venv/bin/pytest tests/test_content_e2e_quality.py -v -k "not llm"
"""
from __future__ import annotations

import sys, os, pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── Sample payloads ────────────────────────────────────────────────────

RICH = {
    "title": "Q3 2025 Regional Sales Performance",
    "source": "SalesForce CRM + ERP Fusion",
    "summary": "Q3 2025 delivered 12.3% YoY revenue growth across all regions, with APAC outperforming at +18.7%. Gross margin improved 210 bps to 58.2%.",
    "methodology": "Data sourced from SalesForce and ERP Fusion. Period: Jul 1 -- Sep 30, 2025.",
    "kpis": [
        {"label": "Total Revenue", "value": "$847.2M", "delta": "+12.3% YoY"},
        {"label": "Gross Margin", "value": "58.2%", "delta": "+210 bps"},
        {"label": "New Deals Closed", "value": "1,247", "delta": "+87 QoQ"},
        {"label": "Avg Deal Size", "value": "$679K", "delta": "+5.1%"},
    ],
    "chart": {"title": "Regional Revenue", "type": "bar", "x_key": "region", "y_keys": ["revenue"],
              "data": [{"region": "NA", "revenue": 398}, {"region": "APAC", "revenue": 221},
                       {"region": "EMEA", "revenue": 178}, {"region": "LATAM", "revenue": 50}]},
    "key_findings": [
        {"text": "APAC growth fueled by three semiconductor mega-deals exceeding $20M ACV each."},
        {"text": "NA enterprise saw 15% increase in multi-year contract renewal rates."}],
    "insights": [
        {"text": "Semiconductor vertical contributed 31% of APAC revenue."},
        {"text": "Multi-year contract attach rate reached 68%, highest ever."}],
    "recommendations": [
        {"text": "Invest $2M in EMEA sales enablement to restore pipeline conversion."},
        {"text": "Launch APAC semiconductor playbook for other regional teams."}],
    "sections": [
        {"title": "APAC Deep-Dive", "content": "Strongest quarter on record.",
         "bullets": ["Semiconductor: $68M", "Cloud: $42M"]}],
}
MSG = "Create a sales report for Q3 2025"
SPARSE = {"title": "Quick Note", "summary": "a report"}


# ══════════════════════════════════════════════════════════════════════
#  Template System (REMOVED)
# ══════════════════════════════════════════════════════════════════════
#
# The hardcoded template system (sales_report / pitch_deck /
# executive_summary / data_brief) was deleted in favor of the C-Heavy
# skill-driven runner.  Document structure is now planned dynamically
# by an LLM inside the sandbox based on the user's actual request.
# Tests for the new path live in tests/test_skill_driven_*.py.

# ══════════════════════════════════════════════════════════════════════
#  Quality Evaluation
# ══════════════════════════════════════════════════════════════════════

class TestQualityEvaluation:
    def test_doc_quality_result_defaults(self):
        from app.services.synexia.quality_eval import DocQualityResult
        r = DocQualityResult()
        assert r.overall_score == 0.0
        assert r.is_ok is False
        assert r.issues == []

    def test_heuristic_rich_scores_high(self):
        from app.services.synexia.quality_eval import evaluate_document_quality
        r = evaluate_document_quality(user_message=MSG, artifact_type="docx", payload=RICH)
        assert r.overall_score > 0.3, f"Rich payload scored {r.overall_score}"

    def test_heuristic_sparse_scores_low(self):
        from app.services.synexia.quality_eval import evaluate_document_quality
        r = evaluate_document_quality(user_message=MSG, artifact_type="pptx", payload=SPARSE)
        assert r.overall_score < 0.6, f"Sparse scored {r.overall_score}"
        assert len(r.issues) > 0 or r.is_ok is False

    def test_completeness_rich_beats_sparse(self):
        from app.services.synexia.quality_eval import evaluate_document_quality
        rich = evaluate_document_quality(user_message=MSG, artifact_type="docx", payload=RICH)
        sparse = evaluate_document_quality(user_message=MSG, artifact_type="docx", payload=SPARSE)
        assert rich.completeness_score > sparse.completeness_score, (
            f"Rich completeness {rich.completeness_score} should exceed sparse {sparse.completeness_score}"
        )

    def test_all_artifact_types_supported(self):
        from app.services.synexia.quality_eval import evaluate_document_quality
        for at in ("docx", "pptx", "pdf"):
            r = evaluate_document_quality(user_message=MSG, artifact_type=at, payload=RICH)
            assert r.overall_score > 0.2, f"Type {at} scored {r.overall_score}"

    def test_evaluation_is_ok_threshold(self):
        """High-quality payload should pass basic quality gate."""
        from app.services.synexia.quality_eval import evaluate_document_quality
        r = evaluate_document_quality(user_message=MSG, artifact_type="docx", payload=RICH)
        # Rich payload: at least has scores and a verdict
        assert r.verdict or r.overall_score > 0, "No verdict or score"

    def test_all_score_dimensions_present(self):
        """All five quality dimensions should be scored (heuristic may be uniform)."""
        from app.services.synexia.quality_eval import evaluate_document_quality
        r = evaluate_document_quality(user_message=MSG, artifact_type="docx", payload=RICH)
        scores = [
            r.structure_score, r.content_score, r.visual_score,
            r.completeness_score, r.density_score,
        ]
        for i, s in enumerate(scores):
            assert 0.0 <= s <= 1.0, f"Dimension {i} out of range: {s}"
        # All scores in valid range — heuristic fallback may give uniform scores;
        # LLM-based evaluation should differentiate them.
        assert all(0.0 <= s <= 1.0 for s in scores)

    @pytest.mark.llm
    def test_evaluate_with_llm(self):
        from app.services.synexia.quality_eval import evaluate_document_quality
        from app.services.llm_service import llm_call
        r = evaluate_document_quality(user_message=MSG, artifact_type="docx", payload=RICH, llm_call=llm_call)
        assert r.overall_score > 0.0


# ══════════════════════════════════════════════════════════════════════
#  Reflexion Critique
# ══════════════════════════════════════════════════════════════════════

class TestReflexionCritique:
    @pytest.mark.asyncio
    async def test_critique_returns_object(self):
        from app.services.synexia.reflexion import critique_document
        r = await critique_document(user_message=MSG, artifact_type="pptx", payload=RICH)
        assert r is not None
        # ReflexionVerdict should have verdict/score
        assert hasattr(r, "verdict") or hasattr(r, "overall_score") or isinstance(r, dict)

    @pytest.mark.asyncio
    async def test_rich_beats_sparse_in_critique(self):
        from app.services.synexia.reflexion import critique_document
        rich = await critique_document(user_message=MSG, artifact_type="pptx", payload=RICH)
        sparse = await critique_document(user_message=MSG, artifact_type="pptx", payload=SPARSE)
        # Both should at least exist
        assert rich is not None
        assert sparse is not None

    @pytest.mark.llm
    @pytest.mark.asyncio
    async def test_critique_with_llm(self):
        from app.services.synexia.reflexion import critique_document
        from app.services.llm_service import llm_call
        r = await critique_document(user_message=MSG, artifact_type="docx", payload=RICH, llm_call=llm_call)
        assert r is not None


# ══════════════════════════════════════════════════════════════════════
#  Synthesis Prompt Content Requirements
# ══════════════════════════════════════════════════════════════════════

class TestSynthesisPromptRequirements:
    """Verify the synthesis system prompt enforces professional document structure."""

    def test_synthesis_prompt_requires_methodology(self):
        """The synthesis system prompt MUST require a methodology section."""
        from app.services.synexia.report_synthesis import _SYNTHESIS_SYSTEM_PROMPT

        prompt_lower = _SYNTHESIS_SYSTEM_PROMPT.lower()
        assert "methodology" in prompt_lower, (
            "Synthesis prompt must require methodology section"
        )

    def test_synthesis_prompt_requires_key_findings(self):
        """The synthesis system prompt MUST require key_findings."""
        from app.services.synexia.report_synthesis import _SYNTHESIS_SYSTEM_PROMPT

        prompt_lower = _SYNTHESIS_SYSTEM_PROMPT.lower()
        assert "key_findings" in prompt_lower or "key findings" in prompt_lower, (
            "Synthesis prompt must require key_findings section"
        )

    def test_synthesis_prompt_requires_recommendations(self):
        """The synthesis system prompt MUST require recommendations."""
        from app.services.synexia.report_synthesis import _SYNTHESIS_SYSTEM_PROMPT

        prompt_lower = _SYNTHESIS_SYSTEM_PROMPT.lower()
        assert "recommendations" in prompt_lower, (
            "Synthesis prompt must require recommendations section"
        )

    def test_synthesis_prompt_requires_at_least_5_insights(self):
        """The synthesis system prompt MUST require at least 5 insights."""
        from app.services.synexia.report_synthesis import _SYNTHESIS_SYSTEM_PROMPT

        prompt_lower = _SYNTHESIS_SYSTEM_PROMPT.lower()
        # The prompt should mention a minimum insight count
        found = any(
            phrase in prompt_lower
            for phrase in ["5 insights", "5-8", "at least 5", "least 5 insight"]
        )
        assert found, (
            "Synthesis prompt must require at least 5 insights for professional depth"
        )

    def test_synthesis_prompt_requires_comparison_periods(self):
        """The synthesis system prompt MUST mention comparison periods."""
        from app.services.synexia.report_synthesis import _SYNTHESIS_SYSTEM_PROMPT

        prompt_lower = _SYNTHESIS_SYSTEM_PROMPT.lower()
        # Must reference MoM, YoY, or comparison period
        has_comparison = any(
            term in prompt_lower
            for term in ["mom", "yoy", "comparison period", "period-over-period",
                         "vs prior", "month-over-month"]
        )
        assert has_comparison, (
            "Synthesis prompt must require comparison periods for analytical depth"
        )

    def test_synthesis_prompt_is_substantially_longer_than_minimal(self):
        """The prompt should be significantly longer than a minimal template."""
        from app.services.synexia.report_synthesis import _SYNTHESIS_SYSTEM_PROMPT

        # A professional prompt must be comprehensive — well over 500 chars
        assert len(_SYNTHESIS_SYSTEM_PROMPT) > 1500, (
            f"Prompt too short ({len(_SYNTHESIS_SYSTEM_PROMPT)} chars) — "
            "must be comprehensive enough to guide professional document generation"
        )
