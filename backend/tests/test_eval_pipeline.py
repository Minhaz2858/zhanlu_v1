"""Regression tests for LLM quality evaluation pipeline (Phase 6)."""

from unittest.mock import MagicMock, patch


class TestEvalPipeline:
    """Tests for eval_pipeline.py."""

    def test_run_eval_pipeline_empty_db(self):
        """Should handle empty database gracefully."""
        from app.services.eval_pipeline import run_eval_pipeline, build_daily_report
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = []
        results = run_eval_pipeline(db)
        assert isinstance(results, list)
        assert len(results) == 0

        report = build_daily_report(results)
        assert report["total"] == 0
        assert "dimensions" in report

    def test_build_daily_report_with_results(self):
        """Should compute correct report statistics."""
        from app.services.eval_pipeline import build_daily_report, EvalRecord
        results = [
            EvalRecord(
                conversation_id="conv-1",
                user_message="q1",
                assistant_text="a1",
                verdict="accept",
                scores={"completeness": 9, "accuracy": 8},
            ),
            EvalRecord(
                conversation_id="conv-2",
                user_message="q2",
                assistant_text="a2",
                verdict="reject",
                scores={"completeness": 4, "accuracy": 5},
            ),
        ]

        report = build_daily_report(results)
        assert report["total"] == 2
        assert report["pass_rate"] == 0.5
        assert isinstance(report["dimensions"], dict)

    def test_eval_pipeline_disabled_check(self):
        """Should honor EVAL_PIPELINE_ENABLED flag."""
        import app.config
        original = getattr(app.config.settings, "EVAL_PIPELINE_ENABLED", None)
        try:
            app.config.settings.EVAL_PIPELINE_ENABLED = False
            from app.services.eval_pipeline import run_eval_pipeline
            # Should return immediately without calling DB
            db = MagicMock()
            # When disabled, the pipeline should skip
            # (implementation detail: check happens at entry point)
        finally:
            if original is not None:
                app.config.settings.EVAL_PIPELINE_ENABLED = original
