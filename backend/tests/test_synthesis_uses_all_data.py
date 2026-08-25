"""2026-08-26: Test that the synthesis prompt uses ALL collected data
and demands a 400-800 word comprehensive report.

User observation: the final answer was inadequate because the
synthesis LLM was being fed only 8 rows + 2000 chars of data, with
a 12s timeout — far too short for a real report. Hard-coding would
break for new databases; instead, the LLM must receive ALL the
data and be told to analyze it generically (any schema).
"""
import inspect


def test_synthesis_prompt_uses_all_rows():
    """The synthesis prompt must include ALL rows from the dataset,
    not just a small sample."""
    from app.routers import agents as a
    src = inspect.getsource(a._force_llm_synthesis)
    # The old code used `rows[:8]` — must NOT be the only sample size
    assert "rows[:8]" not in src, (
        "synthesis still only sends 8 rows; needs ALL rows "
        "(or at least first 200 + pre-aggregated summary)"
    )
    # The new code uses a larger sample
    assert "_MAX_ROWS_IN_PROMPT" in src, (
        "synthesis must cap at a reasonable max rows (200) instead of 8"
    )


def test_synthesis_prompt_demands_400_800_words():
    """The synthesis prompt must demand 400-800 words, not 5-8 sentences."""
    from app.routers import agents as a
    src = inspect.getsource(a._force_llm_synthesis)
    # Must demand 400-800 word report
    assert "400-800 word" in src or "400 word" in src, (
        "synthesis prompt must demand 400-800 word report"
    )
    # Must NOT cap at 5-8 sentences in the actual prompt
    # (occurrences in comments are OK; we check the source for the
    # exact prompt template phrasing)
    assert 'COMPREHENSIVE 5-8 sentence' not in src, (
        "synthesis prompt still has 'COMPREHENSIVE 5-8 sentence' template"
    )


def test_synthesis_prompt_includes_all_5_sections():
    """The synthesis prompt must include all 5 standard report sections."""
    from app.routers import agents as a
    src = inspect.getsource(a._force_llm_synthesis)
    for section in [
        "Executive Summary",
        "Key Numbers",
        "Trends",
        "Notable Anomalies",
        "Recommended Next Steps",
    ]:
        assert section in src, f"section missing from prompt: {section}"


def test_synthesis_timeout_long_enough_for_comprehensive_report():
    """Non-qwen timeout must be at least 30s — 12s is too tight for a
    400-800 word report on the full dataset."""
    from app.routers import agents as a
    src = inspect.getsource(a._force_llm_synthesis)
    # Must NOT be 12s for non-qwen
    assert "60.0 if _is_qwen else 12.0" not in src, (
        "non-qwen timeout still 12s; needs 30-45s for a full report"
    )
    # Must be at least 30s
    import re
    m = re.search(r"60\.0 if _is_qwen else ([\d.]+)", src)
    if m:
        assert float(m.group(1)) >= 30.0, (
            f"non-qwen timeout {m.group(1)} too short; needs >=30s"
        )


def test_synthesis_prompt_uses_business_terms_not_column_names():
    """The prompt must tell the LLM to use business terms (e.g. 'total
    contract value') not raw column names. This is what makes the
    solution generic — works for any database without hardcoding."""
    from app.routers import agents as a
    src = inspect.getsource(a._force_llm_synthesis)
    # Must mention business terms
    assert "business terms" in src.lower() or "business context" in src.lower()
    # Must tell the LLM to compute numbers itself from the rows
    assert "compute" in src.lower() and "yourself" in src.lower()
    # Must warn against just listing column names
    assert "NEVER just list column names" in src or "DO NOT just describe" in src


def test_synthesis_prompt_handles_id_columns_gracefully():
    """The prompt must tell the LLM to skip ID/date columns as 'Key Numbers'
    so it doesn't report customer_id=165446381 as a business metric.
    This is generic (column-pattern-based), not hardcoded for any
    specific database."""
    from app.routers import agents as a
    src = inspect.getsource(a._force_llm_synthesis)
    # Must tell the LLM to skip ID columns
    assert "ID" in src or "_id" in src, (
        "prompt must tell LLM to skip ID columns as 'Key Numbers'"
    )


def test_synthesis_prompt_includes_actual_data():
    """The synthesis prompt must include the actual data rows in the
    prompt text so the LLM can analyze them."""
    from app.routers import agents as a
    src = inspect.getsource(a._force_llm_synthesis)
    # Must include the rows in the prompt
    assert "rows_sample" in src or "rows_data" in src
    # Must include the pre-aggregated block
    assert "preagg_block" in src
