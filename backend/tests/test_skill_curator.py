"""Tests for skill curator."""
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.skill_curator import (
    SkillInfo,
    OverlapPair,
    CurationReport,
    _tokenize,
    _jaccard_similarity,
    find_overlapping_skills,
    find_stale_skills,
    run_skill_curation,
    OVERLAP_THRESHOLD,
)


def test_tokenize():
    """Tokenize splits on word boundaries, skips short tokens."""
    tokens = _tokenize("Hello world, this is a test of the tokenizer")
    assert "hello" in tokens
    assert "world" in tokens
    assert "tokenizer" in tokens
    assert "is" not in tokens  # too short (2 chars)
    assert "a" not in tokens  # too short


def test_jaccard_similarity_identical():
    """Identical sets have similarity 1.0."""
    s = {"a", "b", "c"}
    assert _jaccard_similarity(s, s) == 1.0


def test_jaccard_similarity_disjoint():
    """Disjoint sets have similarity 0.0."""
    assert _jaccard_similarity({"a", "b"}, {"c", "d"}) == 0.0


def test_jaccard_similarity_partial():
    """Partial overlap gives intermediate score."""
    score = _jaccard_similarity({"a", "b", "c"}, {"b", "c", "d"})
    assert 0.0 < score < 1.0
    assert abs(score - 2/4) < 0.01  # 2 shared, 4 total


def test_find_overlapping_skills():
    """Finds skills with high token overlap."""
    # Create mock tools with overlapping content
    tools = []
    for i, (name, content) in enumerate([
        ("python-test-runner", "Run python tests with pytest. This skill helps you run pytest test suites and interpret test results for python projects."),
        ("pytest-helper", "Run python tests with pytest. This skill helps you run pytest test suites and interpret test results for python projects."),
        ("rust-builder", "Build rust projects with cargo. This skill compiles rust code using cargo build."),
    ]):
        t = MagicMock()
        t.id = f"tool-{i}"
        t.name = name
        t.description = content[:50]
        t.content = content
        t.tool_type = "skill"
        t.is_deleted = False
        t.category = "testing"
        t.last_used_at = datetime.utcnow()
        t.usage_count = 5
        t.source = "user"
        tools.append(t)

    db = MagicMock()
    db.query.return_value.all.return_value = tools

    pairs = find_overlapping_skills(db, threshold=0.5)
    # The two pytest skills should overlap significantly
    assert len(pairs) >= 1
    pair = pairs[0]
    assert "pytest" in pair.skill_a.name.lower() or "pytest" in pair.skill_b.name.lower()
    assert pair.overlap_score >= 0.5


def test_find_stale_skills():
    """Finds skills that are unused or stale."""
    tools = []
    for i, (name, usage, last_used) in enumerate([
        ("active-skill", 10, datetime.utcnow()),
        ("unused-skill", 0, None),
        ("stale-skill", 1, datetime.utcnow() - timedelta(days=90)),
    ]):
        t = MagicMock()
        t.id = f"tool-{i}"
        t.name = name
        t.description = f"Description for {name} " * 10  # ensure > MIN_CONTENT_LENGTH
        t.content = f"Content for {name} " * 10
        t.tool_type = "skill"
        t.is_deleted = False
        t.category = "general"
        t.last_used_at = last_used
        t.usage_count = usage
        t.source = "user"
        t.updated_date = datetime.utcnow()
        tools.append(t)

    db = MagicMock()
    db.query.return_value.all.return_value = tools

    stale = find_stale_skills(db, stale_days=60)
    stale_names = {s.name for s in stale}
    assert "unused-skill" in stale_names
    assert "stale-skill" in stale_names
    assert "active-skill" not in stale_names


def test_curation_report_to_dict():
    """CurationReport serializes correctly."""
    report = CurationReport(
        total_skills=10, overlapping_pairs=2, stale_skills=3,
        merge_suggestions=[{"skill_a": "a", "skill_b": "b"}],
        archive_suggestions=[{"name": "old-skill"}],
    )
    d = report.to_dict()
    assert d["total_skills"] == 10
    assert d["overlapping_pairs"] == 2
    assert len(d["merge_suggestions"]) == 1
    assert len(d["archive_suggestions"]) == 1


def test_run_skill_curation():
    """Full curation run produces a report."""
    tools = []
    for i, name in enumerate(["skill-a", "skill-b", "skill-c"]):
        t = MagicMock()
        t.id = f"tool-{i}"
        t.name = name
        t.description = f"Description for {name} " * 10
        t.content = f"Unique content for {name} with different words " * 5
        t.tool_type = "skill"
        t.is_deleted = False
        t.category = "general"
        t.last_used_at = datetime.utcnow() - timedelta(days=90)
        t.usage_count = 0
        t.source = "user"
        t.updated_date = datetime.utcnow()
        tools.append(t)

    db = MagicMock()
    db.query.return_value.all.return_value = tools

    report = run_skill_curation(db, stale_days=60)
    assert report.total_skills == 3
    assert report.stale_skills == 3  # all unused and old
