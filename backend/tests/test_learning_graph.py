"""Tests for the learning graph."""
import json
import os
import sys
import tempfile
from pathlib import Path

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.learning_graph import (
    Learning,
    LearningGraph,
    load_learning_graph,
    save_learning_graph,
    record_learning,
    get_learning_prompt,
)


def test_learning_creation():
    """Learning dataclass stores all fields."""
    l = Learning(
        technique="used pytest -v",
        outcome="success",
        context="debugging test failures",
        confidence=0.5,
        timestamp="2026-01-01T00:00:00",
        tool="execute_code",
    )
    d = l.to_dict()
    assert d["technique"] == "used pytest -v"
    assert d["outcome"] == "success"
    assert d["confidence"] == 0.5


def test_add_new_learning():
    """Adding a new learning creates an entry."""
    graph = LearningGraph(agent_app_id="test-agent")
    graph.add_learning("technique A", "success", context="problem X")
    assert len(graph.learnings) == 1
    assert graph.learnings[0].technique == "technique A"
    assert graph.learnings[0].confidence > 0


def test_reinforce_existing_learning():
    """Adding the same technique+outcome increments confidence."""
    graph = LearningGraph(agent_app_id="test-agent")
    graph.add_learning("technique A", "success")
    initial_confidence = graph.learnings[0].confidence
    graph.add_learning("technique A", "success")
    assert len(graph.learnings) == 1  # no duplicate
    assert graph.learnings[0].confidence > initial_confidence


def test_different_outcome_creates_new():
    """Same technique with different outcome creates a new entry."""
    graph = LearningGraph(agent_app_id="test-agent")
    graph.add_learning("technique A", "success")
    graph.add_learning("technique A", "failure")
    assert len(graph.learnings) == 2


def test_get_successful_techniques():
    """get_successful_techniques filters by outcome and confidence."""
    graph = LearningGraph(agent_app_id="test-agent")
    graph.add_learning("good technique", "success", confidence_boost=0.5)
    graph.add_learning("bad technique", "failure", confidence_boost=0.5)
    graph.add_learning("weak technique", "success", confidence_boost=0.1)  # below threshold

    successes = graph.get_successful_techniques(min_confidence=0.4)
    assert len(successes) == 1
    assert successes[0].technique == "good technique"


def test_get_warnings():
    """get_warnings returns high-confidence failures."""
    graph = LearningGraph(agent_app_id="test-agent")
    graph.add_learning("failed approach", "failure", confidence_boost=0.5)
    graph.add_learning("success", "success", confidence_boost=0.5)

    warnings = graph.get_warnings(min_confidence=0.4)
    assert len(warnings) == 1
    assert warnings[0].outcome == "failure"


def test_to_prompt_text_empty():
    """Empty graph returns empty string."""
    graph = LearningGraph(agent_app_id="test-agent")
    assert graph.to_prompt_text() == ""


def test_to_prompt_text_with_learnings():
    """Graph with learnings produces prompt text."""
    graph = LearningGraph(agent_app_id="test-agent")
    graph.add_learning("use pytest -v", "success", context="debugging", confidence_boost=0.5)
    graph.add_learning("avoid rm -rf", "failure", context="destructive", confidence_boost=0.5)

    text = graph.to_prompt_text()
    assert "pytest -v" in text
    assert "rm -rf" in text
    assert "Learned from past sessions" in text


def test_save_and_load():
    """Learning graph can be saved and loaded from disk."""
    with tempfile.TemporaryDirectory() as tmpdir:
        graph = LearningGraph(agent_app_id="test-agent")
        graph.add_learning("technique A", "success", confidence_boost=0.5)
        save_learning_graph(graph, storage_dir=tmpdir)

        loaded = load_learning_graph("test-agent", storage_dir=tmpdir)
        assert len(loaded.learnings) == 1
        assert loaded.learnings[0].technique == "technique A"


def test_load_nonexistent():
    """Loading a non-existent graph returns empty."""
    with tempfile.TemporaryDirectory() as tmpdir:
        graph = load_learning_graph("nonexistent", storage_dir=tmpdir)
        assert len(graph.learnings) == 0
        assert graph.agent_app_id == "nonexistent"


def test_record_learning_persists():
    """record_learning saves to disk."""
    with tempfile.TemporaryDirectory() as tmpdir:
        record_learning("test-agent", "technique X", "success",
                        context="test", tool="read_file", storage_dir=tmpdir)

        graph = load_learning_graph("test-agent", storage_dir=tmpdir)
        assert len(graph.learnings) == 1
        assert graph.learnings[0].technique == "technique X"
        assert graph.learnings[0].tool == "read_file"


def test_get_learning_prompt():
    """get_learning_prompt returns formatted text."""
    with tempfile.TemporaryDirectory() as tmpdir:
        record_learning("test-agent", "use pytest", "success",
                        confidence_boost=0.5, storage_dir=tmpdir)
        prompt = get_learning_prompt("test-agent", storage_dir=tmpdir)
        assert "pytest" in prompt
        assert "Learned from past sessions" in prompt


def test_max_learnings_prune():
    """Graph prunes to max when too many learnings."""
    graph = LearningGraph(agent_app_id="test-agent")
    for i in range(60):
        graph.add_learning(f"technique_{i}", "success", confidence_boost=0.1)
    assert len(graph.learnings) <= 50
