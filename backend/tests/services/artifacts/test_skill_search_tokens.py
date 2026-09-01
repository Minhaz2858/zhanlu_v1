"""Tests for tokenized skill search (_search_tokens + SkillsRegistry.search).

The old whole-string substring search returned NOTHING for full user
sentences ("analyze competitors and why they rank higher") because the
sentence as a single substring never appears in a skill name/description.
Tokenized search fixes the harness gap: the agent can now discover skills
from natural-language requests, not just single keywords.
"""
import pytest

from app.services.skills_loader import _search_tokens


def test_tokens_strip_stopwords_and_short() -> None:
    toks = _search_tokens("analyze competitors and why they rank higher")
    # stopwords (and/why) removed; the meaningful tokens remain.
    assert "competitors" in toks
    assert "and" not in toks
    assert "why" not in toks
    # "rank" and "higher" survive as signal.
    assert "rank" in toks
    # "analyze" is a domain term, NOT a stopword — removing it would make
    # "data analysis" queries match nothing.
    assert "analyze" in toks


def test_tokens_empty_for_noise_only() -> None:
    assert _search_tokens("") == []
    assert _search_tokens("the a an of to for") == []


def test_tokens_cjk_bigrams() -> None:
    toks = _search_tokens("写一份行业研究报告")
    # Whole string + sliding 2-char bigrams for CJK matching.
    assert "行业" in toks
    assert "研究" in toks
    assert "报告" in toks


def test_tokens_dedupe() -> None:
    toks = _search_tokens("market market analysis analysis")
    assert toks.count("market") == 1
    # "analysis" is a domain term and survives (deduped).
    assert toks.count("analysis") == 1


def test_search_finds_skill_from_full_sentence() -> None:
    """The harness fix: a natural-language sentence discovers the skill."""
    from app.services.skills_loader import get_skills_registry

    reg = get_skills_registry()
    results = reg.search("analyze competitors and why they rank higher", limit=5)
    names = [s.name for s in results]
    assert "competitor-analysis" in names, f"competitor-analysis not in {names}"


def test_search_finds_research_from_sentence() -> None:
    from app.services.skills_loader import get_skills_registry

    reg = get_skills_registry()
    results = reg.search("write an equity research report on this stock", limit=5)
    names = [s.name for s in results]
    assert "equity-research-report" in names, f"equity-research-report not in {names}"


def test_search_short_keyword_still_works() -> None:
    """Legacy single-keyword behavior must not regress."""
    from app.services.skills_loader import get_skills_registry

    reg = get_skills_registry()
    results = reg.search("pptx", limit=5)
    names = [s.name for s in results]
    assert any("ppt" in n for n in names), f"no pptx skill in {names}"


def test_search_cjk_sentence() -> None:
    from app.services.skills_loader import get_skills_registry

    reg = get_skills_registry()
    results = reg.search("帮我写一份行业研究报告", limit=5)
    names = [s.name for s in results]
    # stock-research-report / vc-industry-research carry 行业/研究 triggers.
    assert any("research" in n or "stock" in n or "brief" in n for n in names), f"no research skill in {names}"
