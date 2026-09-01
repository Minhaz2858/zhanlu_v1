"""Tests for _strip_sql_narration — SQL/plan narration leak fix (2026-08-21).

Production symptom: the model narrates the SQL it intends to run ("For the
full month of July 2026 … SELECT … FROM erp_t_sal_outstockentry …") and
emits an empty "JSON Report Card" section. That narration leaked into the
final bubble. This strip removes, deterministically:

1. Fenced ```sql …``` blocks (and plain fences whose content starts with
   SELECT/WITH).
2. Bare-SQL paragraphs (paragraph starting with SELECT/WITH and containing
   FROM).
3. "JSON Report Card"-style markdown sections (heading + json fence/empty
   body) — never legitimate user-facing content.

Real answers that merely mention a table name or include a small inline
snippet must survive.
"""

import pytest

from app.routers.agents import _strip_sql_narration


def test_fenced_sql_block_removed():
    text = (
        "Here is the plan.\n\n"
        "```sql\nSELECT FNAME, FQTY FROM erp_t_sal_outstockentry WHERE FDATE >= '2026-07-01';\n```\n\n"
        "The rest of the answer."
    )
    out = _strip_sql_narration(text)
    assert "SELECT" not in out
    assert "erp_t_sal_outstockentry" not in out
    assert "Here is the plan." in out
    assert "The rest of the answer." in out


def test_plain_fence_starting_with_select_removed():
    text = "Intro\n\n```\nSELECT FMATERIALID FROM aipdp_data_warehouse_prod LIMIT 80\n```\n\nOutro"
    out = _strip_sql_narration(text)
    assert "FMATERIALID" not in out
    assert "Intro" in out and "Outro" in out


def test_bare_sql_paragraph_removed():
    text = (
        "Summary: revenue grew.\n\n"
        "SELECT s.FMATERIALID, SUM(s.FAMOUNT) FROM erp_t_sal_outstockentry s GROUP BY s.FMATERIALID;\n\n"
        "Conclusion follows."
    )
    out = _strip_sql_narration(text)
    assert "SELECT" not in out
    assert "Summary: revenue grew." in out
    assert "Conclusion follows." in out


def test_json_report_card_section_removed():
    text = (
        "## Executive Summary\n\nRevenue was ¥3.5M.\n\n"
        "## JSON Report Card\n\n```json\n{\"title\": \"x\", \"kpis\": []}\n```\n\n"
        "## Key Observations\n\nConcentration risk."
    )
    out = _strip_sql_narration(text)
    assert "JSON Report Card" not in out
    assert "Executive Summary" in out
    assert "Key Observations" in out
    assert "¥3.5M" in out


def test_empty_json_report_card_section_removed():
    text = "## Analysis\n\nGood numbers.\n\n## JSON Report Card\n\n\n## Next\n\nBye."
    out = _strip_sql_narration(text)
    assert "JSON Report Card" not in out
    assert "Analysis" in out and "Next" in out


def test_legit_answer_survives():
    text = (
        "## July 2026 Sales Report\n\n"
        "Total revenue was ¥3.50M across 41 products. The top product "
        "(103350) contributed 78.9% of revenue.\n\n"
        "- Concentration risk is high.\n"
        "- Margin ratio is 91.6 vs 138.5.\n"
    )
    out = _strip_sql_narration(text)
    assert out.strip() == text.strip()


def test_inline_code_with_short_sql_identifier_survives():
    # A short inline mention (not a fence, not a bare-SQL paragraph) stays.
    text = "I queried `erp_t_sal_outstockentry` for July; results below."
    out = _strip_sql_narration(text)
    assert "erp_t_sal_outstockentry" in out


def test_empty_and_none_input():
    assert _strip_sql_narration("") == ""
    assert _strip_sql_narration("   ") == "   "


def test_degenerate_no_headings():
    # json section strip must not eat the whole doc when no headings exist.
    text = "Just prose, no structure at all."
    assert _strip_sql_narration(text) == text
