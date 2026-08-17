"""Tests for DeckPlan persistence into ArtifactVersion.source_json.

Verifies the plan dict stored by the orchestrator round-trips through
DeckPlan.model_validate (the PHASE 2 edit-tool contract).  Mirrors exactly
what sandbox_tool._run_sandbox_skill / artifact_tool.create_artifact persist.
"""

from __future__ import annotations

from app.services.synexia.contracts import DeckPlan, SlidePlan


def _plan_dict() -> dict:
    plan = DeckPlan(
        title="Persisted Deck",
        deck_type="data_report",
        slides=[
            SlidePlan(layout="cover", title="Persisted Deck", narrative_role="hook",
                      headline_style="assertion"),
            SlidePlan(layout="bullets", title="Revenue grew 8% QoQ",
                      bullets=["Detail one.", "Detail two."], max_bullets=5,
                      max_words_per_bullet=12, headline_style="assertion"),
        ],
    )
    return plan.model_dump(mode="json")


def test_source_json_deck_plan_roundtrip():
    deck_plan = _plan_dict()
    source_json = {"deck_plan": deck_plan}
    # This is what PHASE 2 edit tools will do.
    restored = DeckPlan.model_validate(source_json["deck_plan"])
    assert restored.title == "Persisted Deck"
    assert len(restored.slides) == 2
    assert restored.slides[1].headline_style == "assertion"
    assert restored.slides[1].max_bullets == 5


def test_source_json_survives_json_serialize():
    import json

    source_json = {"deck_plan": _plan_dict()}
    blob = json.dumps(source_json)
    back = json.loads(blob)
    restored = DeckPlan.model_validate(back["deck_plan"])
    assert restored.deck_type == "data_report"
    assert restored.slides[0].layout == "cover"
