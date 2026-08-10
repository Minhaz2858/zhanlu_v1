"""Tests for the recipe store (experience layer Phase A).

Recipes record which tool sequence produced a good answer per intent class.
Stored in the same JSON-file-per-agent pattern as the existing learning graph.

Backward compatibility: the existing learning JSON files must still load and
round-trip without a recipes key.
"""

import sys
import os
import json

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

import pytest

from app.services.learning_graph import (
    LearningGraph,
    Recipe,
    load_learning_graph,
    save_learning_graph,
    record_recipe,
    adjust_recipe_feedback,
    get_recipe_prompt,
    load_recipes,
    record_learning,
    get_learning_prompt,
)


@pytest.fixture
def tmp_store(tmp_path):
    return str(tmp_path)


class TestRecipeDataclass:
    def test_recipe_defaults(self):
        r = Recipe(intent_class="price_report", tool_sequence=["ask_pricing", "ask_report"])
        assert r.success_count == 0
        assert r.failure_count == 0
        assert r.confidence == 0.0
        assert r.avg_iterations == 0.0
        assert r.last_used == ""

    def test_recipe_to_dict_roundtrip(self):
        r = Recipe(
            intent_class="price_report",
            tool_sequence=["ask_pricing", "ask_report"],
            success_count=5,
            failure_count=1,
            confidence=0.8,
            avg_iterations=3.2,
            last_used="2026-08-10T00:00:00+00:00",
        )
        d = r.to_dict()
        r2 = Recipe.from_dict(d)
        assert r2 == r


class TestRecordRecipe:
    def test_new_recipe_created(self, tmp_store):
        record_recipe(
            "agent-1", "price_report", ["ask_pricing", "ask_report"],
            success=True, iterations=3, storage_dir=tmp_store,
        )
        recipes = load_recipes("agent-1", storage_dir=tmp_store)
        assert len(recipes) == 1
        r = recipes[0]
        assert r.intent_class == "price_report"
        assert r.tool_sequence == ["ask_pricing", "ask_report"]
        assert r.success_count == 1
        assert r.confidence > 0.0

    def test_same_recipe_reinforced(self, tmp_store):
        record_recipe("agent-1", "price_report", ["ask_pricing"], success=True, storage_dir=tmp_store)
        record_recipe("agent-1", "price_report", ["ask_pricing"], success=True, storage_dir=tmp_store)
        recipes = load_recipes("agent-1", storage_dir=tmp_store)
        assert len(recipes) == 1
        assert recipes[0].success_count == 2
        assert recipes[0].confidence > 0.5

    def test_failure_penalizes(self, tmp_store):
        record_recipe("agent-1", "price_report", ["ask_pricing"], success=True, storage_dir=tmp_store)
        record_recipe("agent-1", "price_report", ["ask_pricing"], success=False, storage_dir=tmp_store)
        recipes = load_recipes("agent-1", storage_dir=tmp_store)
        assert recipes[0].success_count == 1
        assert recipes[0].failure_count == 1

    def test_different_sequences_are_distinct(self, tmp_store):
        record_recipe("agent-1", "price_report", ["ask_pricing"], success=True, storage_dir=tmp_store)
        record_recipe("agent-1", "price_report", ["ask_perception", "ask_pricing"], success=True, storage_dir=tmp_store)
        recipes = load_recipes("agent-1", storage_dir=tmp_store)
        assert len(recipes) == 2

    def test_avg_iterations_update(self, tmp_store):
        record_recipe("agent-1", "price_report", ["ask_pricing"], success=True, iterations=2, storage_dir=tmp_store)
        record_recipe("agent-1", "price_report", ["ask_pricing"], success=True, iterations=4, storage_dir=tmp_store)
        recipes = load_recipes("agent-1", storage_dir=tmp_store)
        assert recipes[0].avg_iterations == 3.0

    def test_isolated_per_agent(self, tmp_store):
        record_recipe("agent-1", "price_report", ["ask_pricing"], success=True, storage_dir=tmp_store)
        assert load_recipes("agent-2", storage_dir=tmp_store) == []


class TestRecipePrompt:
    def test_prompt_empty_when_no_recipes(self, tmp_store):
        assert get_recipe_prompt("agent-1", storage_dir=tmp_store) == ""

    def test_prompt_contains_recipe(self, tmp_store):
        record_recipe("agent-1", "price_report", ["ask_pricing", "ask_report"], success=True, storage_dir=tmp_store)
        record_recipe("agent-1", "price_report", ["ask_pricing", "ask_report"], success=True, storage_dir=tmp_store)
        prompt = get_recipe_prompt("agent-1", storage_dir=tmp_store)
        assert "price_report" in prompt
        assert "ask_pricing" in prompt
        assert "ask_report" in prompt

    def test_low_confidence_recipe_excluded_from_prompt(self, tmp_store):
        # 1 success + 1 failure -> confidence low -> not shown
        record_recipe("agent-1", "price_report", ["ask_pricing"], success=True, storage_dir=tmp_store)
        record_recipe("agent-1", "price_report", ["ask_pricing"], success=False, storage_dir=tmp_store)
        assert get_recipe_prompt("agent-1", storage_dir=tmp_store) == ""


class TestAdjustRecipeFeedback:
    def test_thumbs_up_reinforces_best_recipe(self, tmp_store):
        record_recipe("agent-1", "price_report", ["ask_pricing"], success=True, storage_dir=tmp_store)
        record_recipe("agent-1", "price_report", ["ask_pricing"], success=True, storage_dir=tmp_store)
        before = load_recipes("agent-1", storage_dir=tmp_store)[0]
        adjust_recipe_feedback("agent-1", "price_report", 1, storage_dir=tmp_store)
        after = load_recipes("agent-1", storage_dir=tmp_store)[0]
        assert after.success_count == before.success_count + 1
        assert after.failure_count == before.failure_count
        assert after.confidence > before.confidence

    def test_thumbs_down_penalizes(self, tmp_store):
        record_recipe("agent-1", "market_analysis", ["ask_perception"], success=True, storage_dir=tmp_store)
        before = load_recipes("agent-1", storage_dir=tmp_store)[0]
        adjust_recipe_feedback("agent-1", "market_analysis", -1, storage_dir=tmp_store)
        after = load_recipes("agent-1", storage_dir=tmp_store)[0]
        assert after.failure_count == before.failure_count + 1
        assert after.confidence < before.confidence

    def test_noop_when_no_recipe_for_intent(self, tmp_store):
        record_recipe("agent-1", "price_report", ["ask_pricing"], success=True, storage_dir=tmp_store)
        adjust_recipe_feedback("agent-1", "forecast_question", 1, storage_dir=tmp_store)
        recipes = load_recipes("agent-1", storage_dir=tmp_store)
        assert len(recipes) == 1  # unchanged
        assert recipes[0].intent_class == "price_report"

    def test_zero_rating_is_noop(self, tmp_store):
        record_recipe("agent-1", "price_report", ["ask_pricing"], success=True, storage_dir=tmp_store)
        before = load_recipes("agent-1", storage_dir=tmp_store)[0]
        adjust_recipe_feedback("agent-1", "price_report", 0, storage_dir=tmp_store)
        after = load_recipes("agent-1", storage_dir=tmp_store)[0]
        assert after.success_count == before.success_count


class TestBackwardCompat:
    def test_existing_learning_file_still_loads(self, tmp_store):
        # Legacy file without recipes key
        path = os.path.join(tmp_store, "agent-1.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"agent_app_id": "agent-1", "learnings": []}, f)
        graph = load_learning_graph("agent-1", storage_dir=tmp_store)
        assert graph.agent_app_id == "agent-1"

    def test_learning_and_recipes_coexist(self, tmp_store):
        # Two records push learning confidence above the 0.4 prompt threshold
        record_learning("agent-1", "use pricing tool", "success", storage_dir=tmp_store)
        record_learning("agent-1", "use pricing tool", "success", storage_dir=tmp_store)
        record_recipe("agent-1", "price_report", ["ask_pricing"], success=True, storage_dir=tmp_store)
        # Both prompts work
        assert get_learning_prompt("agent-1", storage_dir=tmp_store) != ""
        assert get_recipe_prompt("agent-1", storage_dir=tmp_store) != ""
        # Learning graph still intact
        graph = load_learning_graph("agent-1", storage_dir=tmp_store)
        assert len(graph.learnings) == 1
