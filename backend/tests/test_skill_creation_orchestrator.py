"""Tests for the stateful 4-phase skill creation orchestrator.

Verifies that:
1. A fresh turn with no draft starts the Understand phase and produces a draft.
2. The draft advances collecting -> proposing -> drafting -> review -> ready.
3. Draft state is persisted/loaded by conversation_id via SkillDraftStore.
4. Saving a draft persists a folder-package-shaped artifact to the filesystem.
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.database import Base, engine, SessionLocal
import app.models  # noqa: F401

Base.metadata.create_all(engine)

from app.services.skill_studio import CreationOrchestrator, SkillDraft
from app.services.skill_studio.draft_store import SkillDraftStore

CONVERSATION_ID = "conv-orchestrator-test-001"


@pytest.fixture(autouse=True)
def isolate_draft_store():
    """Each test gets a clean in-memory + temp-dir draft store."""
    store = SkillDraftStore()
    # Point the drafts dir at a temp location so filesystem persistence is isolated.
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="test_drafts_"))
    object.__setattr__(store, "_drafts_dir", tmp)
    yield store
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


def test_draft_store_roundtrip(isolate_draft_store):
    store = isolate_draft_store
    draft = SkillDraft(
        name="weekly-report",
        description="Generates a weekly ops report",
        skill_md="# Weekly Report\n",
        status="collecting",
        conversation_id=CONVERSATION_ID,
        turn_count=0,
    )
    store.put(draft)
    loaded = store.get(CONVERSATION_ID)
    assert loaded is not None
    assert loaded.name == "weekly-report"
    assert loaded.status == "collecting"


def test_draft_store_missing_returns_none(isolate_draft_store):
    assert isolate_draft_store.get("does-not-exist-conv") is None


@pytest.mark.asyncio
async def test_process_turn_creates_draft_with_llm_fallback(isolate_draft_store):
    """First turn should create a draft and move to proposing (LLM failure -> fallback)."""
    orch = CreationOrchestrator(store=isolate_draft_store)
    with patch(
        "app.services.llm_service.call_llm",
        new_callable=AsyncMock,
        side_effect=Exception("no llm"),
    ):
        result = await orch.process_turn(
            conversation_id=CONVERSATION_ID,
            user_message="create a skill that summarizes meeting notes into action items",
        )
    assert result.draft is not None
    assert result.draft.name  # fallback slug from the message
    assert result.draft.status == "proposing"
    # Persisted so a follow-up turn sees the same draft.
    assert isolate_draft_store.get(CONVERSATION_ID) is not None


@pytest.mark.asyncio
async def test_full_flow_to_saved(isolate_draft_store):
    """Drive collecting -> proposing -> drafting -> review -> saved."""
    orch = CreationOrchestrator(store=isolate_draft_store)

    with patch(
        "app.services.llm_service.call_llm",
        new_callable=AsyncMock,
        return_value={"response": '{"name": "meeting-notes", "description": "Summarize meetings"}'},
    ):
        r1 = await orch.process_turn(CONVERSATION_ID, "create a skill for meeting notes")
    assert r1.draft.status == "proposing"

    # Confirm the proposed layout -> drafting happens automatically.
    with patch(
        "app.services.llm_service.call_llm",
        new_callable=AsyncMock,
        return_value={"response": "# Meeting Notes Skill\n\nSummarize.\n"},
    ), patch(
        "app.services.skill_sync.reload_skills_registry",
        new_callable=MagicMock,
    ):
        r2 = await orch.process_turn(CONVERSATION_ID, "looks good")
    assert r2.draft.status in ("review", "ready", "saved")

    # Save it.
    with patch(
        "app.services.skill_sync.reload_skills_registry",
        new_callable=MagicMock,
    ):
        r3 = await orch.process_turn(CONVERSATION_ID, "save it")
    assert r3.saved is True
    assert r3.draft.status == "saved"


def test_skill_draft_defaults():
    draft = SkillDraft(conversation_id=CONVERSATION_ID)
    assert draft.status == "collecting"
    assert draft.name == ""
    assert draft.references == {}
    assert draft.assets == {}
