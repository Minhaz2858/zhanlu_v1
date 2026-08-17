"""Persistence for in-flight ``SkillDraft`` state.

The creation orchestrator maintains state across conversation turns. Each
conversation maps to at most one active ``SkillDraft``. Storage is hybrid:

- Primary: an in-process registry keyed by ``conversation_id`` (fast, no DB
  round-trip, suitable for the single-process backend deployment).
- Optional: JSON persistence to ``~/.zhanlu/skills/.drafts/<conversation_id>.json``
  so a draft survives a backend restart.

The store is intentionally small and defensive — lookups never raise, and a
missing/corrupt draft simply yields ``None`` (the orchestrator then starts a
fresh draft).
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.services.skill_sync import USER_SKILLS_DIR

logger = logging.getLogger(__name__)

# Valid status values for a draft's lifecycle.
DRAFT_STATUSES = (
    "collecting",  # gathering requirements
    "proposing",   # presenting folder layout / plan
    "drafting",    # generating file contents
    "review",      # user reviewing the draft
    "ready",       # ready to save
    "saved",       # persisted to filesystem + DB
)


@dataclass
class SkillDraft:
    name: str = ""
    description: str = ""
    skill_md: str = ""
    references: dict[str, str] = field(default_factory=dict)  # filename -> md content
    assets: dict[str, str] = field(default_factory=dict)  # rel_path -> source path/url/base64
    manifest: dict[str, Any] = field(default_factory=dict)
    status: str = "collecting"
    conversation_id: str = ""
    turn_count: int = 0
    category: str = "custom"
    author: str = "user"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SkillDraft":
        allowed = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in (data or {}).items() if k in allowed}
        draft = cls(**kwargs)
        if draft.status not in DRAFT_STATUSES:
            draft.status = "collecting"
        return draft


class SkillDraftStore:
    """Thread-safe registry + filesystem persistence for SkillDraft objects."""

    _drafts_dir = USER_SKILLS_DIR / ".drafts"

    def __init__(self):
        self._lock = threading.RLock()
        self._memory: dict[str, SkillDraft] = {}

    # ── persistence helpers ────────────────────────────────────────────

    def _draft_path(self, conversation_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in conversation_id)
        return self._drafts_dir / f"{safe}.json"

    def _persist(self, draft: SkillDraft) -> None:
        try:
            self._drafts_dir.mkdir(parents=True, exist_ok=True)
            path = self._draft_path(draft.conversation_id)
            path.write_text(
                json.dumps(draft.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Failed to persist skill draft: %s", exc)

    # ── public API ─────────────────────────────────────────────────────

    def get(self, conversation_id: str) -> SkillDraft | None:
        with self._lock:
            draft = self._memory.get(conversation_id)
            if draft is not None:
                return draft
        # Try filesystem (e.g. after a restart).
        return self._load_from_disk(conversation_id)

    def _load_from_disk(self, conversation_id: str) -> SkillDraft | None:
        path = self._draft_path(conversation_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            draft = SkillDraft.from_dict(data)
            with self._lock:
                self._memory[conversation_id] = draft
            return draft
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Failed to load skill draft for %s: %s", conversation_id, exc)
            return None

    def put(self, draft: SkillDraft) -> SkillDraft:
        with self._lock:
            self._memory[draft.conversation_id] = draft
        self._persist(draft)
        return draft

    def delete(self, conversation_id: str) -> None:
        with self._lock:
            self._memory.pop(conversation_id, None)
        try:
            path = self._draft_path(conversation_id)
            if path.exists():
                path.unlink()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Failed to delete skill draft for %s: %s", conversation_id, exc)


# Module-level singleton used by the orchestrator. Imported by routers so the
# same store is shared across requests.
draft_store = SkillDraftStore()
