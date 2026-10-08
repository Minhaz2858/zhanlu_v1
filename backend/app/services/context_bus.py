"""Shared context bus — the single context model for both agent brains.

IBM orchestration fix (context-management step): the FSM path and the ReAct
main loop previously ran on separate context models (``ContextManifest`` vs
raw ``llm_messages`` + compaction), which caused the context-loss bug class.
The ContextBus is the convergence point: a per-conversation, section-keyed
store that both paths publish to and retrieve from, keeping the FSM's
deterministic skeleton untouched.

Design:
- Sections: ``system`` (agent config, tools, skills), ``transcript``,
  ``artifacts``, ``tool_digest`` (rolling per-turn tool-result digests),
  ``task_state`` (plan/gate/verify records), ``memory``, ``compaction``.
- Retrieval: MiniLM cosine via the shared document embedder (same model as
  ``dynamic_tools``), with a deterministic lexical fallback so the bus never
  fails open-closed.
- ``to_fsm_manifest()`` shapes output for ``synexia.context_assembler``
  consumers; ``format_for_react()`` renders a compact system-prompt block.
"""
from __future__ import annotations

import logging
import math
import re
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_SECTION_ORDER = (
    "system",
    "task_state",
    "compaction",
    "memory",
    "artifacts",
    "tool_digest",
    "transcript",
)

_MAX_SECTION_CHARS = 2000
_MAX_RETRIEVE_CHARS = 3500


@dataclass
class ContextSection:
    """One typed context entry on the bus."""

    key: str
    text: str
    meta: dict = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)


class ContextBus:
    """Per-conversation shared context store (in-memory, thread-safe)."""

    def __init__(self, conversation_id: str):
        self.conversation_id = conversation_id
        self._sections: dict[str, ContextSection] = {}
        self._lock = threading.Lock()

    # ── publish / read ──

    def publish(self, key: str, text: str, meta: dict | None = None) -> None:
        if not key or not isinstance(text, str):
            return
        text = text[:_MAX_SECTION_CHARS]
        with self._lock:
            self._sections[key] = ContextSection(
                key=key, text=text, meta=dict(meta or {}),
                updated_at=time.time(),
            )

    def get(self, key: str) -> ContextSection | None:
        with self._lock:
            section = self._sections.get(key)
            return ContextSection(
                key=section.key, text=section.text,
                meta=dict(section.meta), updated_at=section.updated_at,
            ) if section else None

    def keys(self) -> list[str]:
        with self._lock:
            return list(self._sections.keys())

    # ── tool-result digests ──

    def digest_tool_result(self, tool_name: str, result: dict | None,
                           max_chars: int = 1200) -> None:
        """Rolling digest of a substantive tool result this turn.

        Stored under ``tool_digest:<tool_name>``; the ``tool_digest`` parent
        section aggregates names so retrieval/prompt rendering can find them.
        """
        if not tool_name:
            return
        key = f"tool_digest:{tool_name}"
        text = _summarize_result(result, max_chars)
        with self._lock:
            names = [k.split(":", 1)[1] for k in self._sections
                     if k.startswith("tool_digest:") and k != key]
            meta = {"tools": sorted(set(names + [tool_name]))}
            self._sections[key] = ContextSection(
                key=key, text=text, meta=meta, updated_at=time.time(),
            )
            self._sections["tool_digest"] = ContextSection(
                key="tool_digest",
                text="Tools executed this turn: " + ", ".join(meta["tools"]),
                meta={"tools": meta["tools"]},
                updated_at=time.time(),
            )

    # ── retrieval ──

    def retrieve(self, query: str, k: int = 5) -> list[ContextSection]:
        """Top-k sections by semantic similarity, lexical fallback."""
        with self._lock:
            sections = list(self._sections.values())
        if not sections or not query:
            return []
        scored = self._semantic_scores(query, sections)
        if scored is None:
            scored = _lexical_scores(query, sections)
        ranked = sorted(zip(scored, sections), key=lambda t: -t[0])
        out: list[ContextSection] = []
        total = 0
        for score, section in ranked:
            if len(out) >= k:
                break
            if score <= 0.0:
                continue
            out.append(section)
            total += len(section.text)
            if total >= _MAX_RETRIEVE_CHARS:
                break
        return out

    @staticmethod
    def _semantic_scores(query: str, sections: list[ContextSection]) -> list[float] | None:
        try:
            from app.services.dynamic_tools import _embed_one_or_none

            qv = _embed_one_or_none(query)
            if not qv:
                return None
            texts = [f"{s.key}: {s.text}" for s in sections]
            from app.services.dynamic_tools import _embed_or_none

            dvs = _embed_or_none(texts)
            if not dvs or len(dvs) != len(texts):
                return None
            return [_cosine(qv, dv) for dv in dvs]
        except Exception as exc:  # noqa: BLE001 — retrieval must never raise
            logger.debug("context_bus: semantic retrieval failed: %s", exc)
            return None

    # ── consumers ──

    def to_fsm_manifest(self) -> dict:
        """Shape bus content for ``synexia.context_assembler.ContextManifest``
        consumers. Keys mirror assemble_context()'s contract."""
        items: list[dict] = []
        available_tools: list[str] = []
        available_skills: list[str] = []
        with self._lock:
            sections = dict(self._sections)
        for key, section in sections.items():
            items.append({
                "type": key,
                "content": section.text,
                "meta": dict(section.meta),
                "updated_at": section.updated_at,
            })
        system = sections.get("system")
        if system:
            available_tools = list(system.meta.get("tools") or [])
            available_skills = list(system.meta.get("skills") or [])
        token_estimate = sum(len(s.text) for s in sections.values()) // 4
        return {
            "items": items,
            "token_estimate": token_estimate,
            "available_tools": available_tools,
            "available_skills": available_skills,
            "user_attachments": list((sections.get("system").meta or {}).get("attachments") or []),
        }

    def format_for_react(self, skip: set[str] | None = None) -> str:
        """Compact block for ReAct system-prompt injection.

        ``skip`` drops sections already covered by another block (e.g. the
        followup-context block covers ``transcript``/``artifacts``).
        """
        skip = skip or set()
        with self._lock:
            sections = dict(self._sections)
        lines: list[str] = []
        for key in _SECTION_ORDER:
            if key in skip:
                continue
            section = sections.get(key)
            if not section:
                continue
            header = key.replace("tool_digest:", "tool_result:") .replace("_", " ").upper()
            lines.append(f"[{header}]\n{section.text}")
        # per-tool digests not covered by the aggregate section
        for key, section in sections.items():
            if key.startswith("tool_digest:"):
                name = key.split(":", 1)[1]
                lines.append(f"[TOOL RESULT: {name}]\n{section.text}")
        return "\n\n".join(lines).strip()


# ── registry ──

_BUSSES: dict[str, ContextBus] = {}
_REGISTRY_LOCK = threading.Lock()


def get_context_bus(conversation_id: str) -> ContextBus:
    """Create-or-get the bus for a conversation (one object shared by both
    agent paths within the turn)."""
    with _REGISTRY_LOCK:
        bus = _BUSSES.get(conversation_id)
        if bus is None:
            bus = ContextBus(conversation_id)
            _BUSSES[conversation_id] = bus
        return bus


def drop_context_bus(conversation_id: str) -> None:
    """End-of-turn cleanup (called from both terminal paths)."""
    with _REGISTRY_LOCK:
        _BUSSES.pop(conversation_id, None)


# ── helpers ──

def _cosine(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    da = math.sqrt(sum(x * x for x in a))
    db = math.sqrt(sum(y * y for y in b))
    if da == 0.0 or db == 0.0:
        return 0.0
    return num / (da * db)


_WORD_RE = re.compile(r"[a-z0-9_]{2,}")


def _lexical_scores(query: str, sections: list[ContextSection]) -> list[float]:
    q_words = set(_WORD_RE.findall(query.lower()))
    scores: list[float] = []
    for s in sections:
        hay = set(_WORD_RE.findall(f"{s.key} {s.text}".lower()))
        if not hay:
            scores.append(0.0)
            continue
        inter = len(q_words & hay)
        scores.append(inter / max(1, len(q_words)) if q_words else 0.0)
    return scores


def _summarize_result(result: dict | None, max_chars: int) -> str:
    if result is None:
        return "(no result)"
    try:
        if isinstance(result, dict):
            parts: list[str] = []
            for key in ("summary", "answer", "final_text", "error", "status"):
                val = result.get(key)
                if isinstance(val, str) and val.strip():
                    parts.append(f"{key}: {val.strip()}")
            rows = result.get("rows")
            if isinstance(rows, list):
                parts.append(f"rows: {len(rows)}")
                if rows[:2]:
                    parts.append(f"sample: {str(rows[:2])[:400]}")
            for key, val in result.items():
                if key in ("summary", "answer", "final_text", "error", "status", "rows"):
                    continue
                text = str(val)
                if text and len(text) < 300:
                    parts.append(f"{key}: {text}")
            return "\n".join(parts)[:max_chars] or str(result)[:max_chars]
        return str(result)[:max_chars]
    except Exception as exc:  # noqa: BLE001
        logger.debug("context_bus: digest summarize failed: %s", exc)
        return str(result)[:max_chars]
