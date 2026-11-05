"""Capability fallback selection (Phase 2.4, 2026-10-31).

ROOT CAUSE ADDRESSED
--------------------
Corrective replans regenerated plans with the SAME executor set — a failed
specialist or tool was retried, never replaced by an authorized alternative.

This module SELECTS alternatives; it never executes them. Selection is:

- AUTHORITY-aware: only PUBLISHED agents / registered tools qualify — the
  publication gate is never bypassed (mission: "do not bypass publication or
  authorization").
- CAPABILITY-aware: keyword overlap between the failed objective and the
  candidate's name/description — unrelated agents are never treated as
  interchangeable.
- SAFETY-aware: destructive operations are never rerouted automatically
  (``fallback_permitted`` refuses them outright).

Pure selection core (``select_alternatives``) + a refusal predicate. The
caller supplies candidates (from the published AgentApp directory / tool
registry) and decides what to do with the result.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_WORD_RE = re.compile(r"[a-z0-9_]+")

# Generic words that carry no capability signal (matched against everything).
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "data", "agent", "analysis", "analyze",
    "report", "query", "task", "assistant", "general", "system", "tool",
})


@dataclass(frozen=True)
class AlternativeCapability:
    kind: str            # "specialist_agent" | "tool"
    name: str
    rationale: str       # authority + capability evidence (model-readable)
    source: str          # where the candidate was discovered

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "name": self.name,
            "rationale": self.rationale,
            "source": self.source,
        }


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# Phase 3.3: Chinese has no spaces — match overlapping character bigrams so
# keyword evidence works without dictionary segmentation. Bigrams of 2+ are
# checked as substrings against the candidate text during scoring.
def _tokens(text: str) -> set[str]:
    # Snake_case names ("sales_analyst") must split into capability words.
    lowered = (text or "").lower().replace("_", " ")
    out: set[str] = {
        t for t in _WORD_RE.findall(lowered)
        if t not in _STOPWORDS and len(t) > 2
    }
    # CJK: single characters are too ambiguous; use character bigrams.
    cjk_runs = _CJK_RE.findall(lowered)
    if cjk_runs:
        # Rebuild contiguous CJK runs for bigram extraction.
        runs = re.findall(r"[\u4e00-\u9fff]+", lowered)
        for run in runs:
            if len(run) >= 2:
                out.update(run[i:i + 2] for i in range(len(run) - 1))
            elif len(run) == 1:
                out.add(run)
    return out


def select_alternatives(
    candidates: list[dict],
    *,
    required_keywords: tuple[str, ...] | list[str],
    exclude: set[str] | None = None,
    limit: int = 3,
) -> list[dict]:
    """Select authorized alternative capabilities for a failed objective.

    Candidates: ``{name, description, published, kind?, source?}`` (extra keys
    ignored). Returns up to ``limit`` dicts with an authority rationale; an
    EMPTY list is the honest refusal — callers must report the limitation,
    never force an unrelated candidate.
    """
    if not candidates:
        return []
    required = _tokens(" ".join(required_keywords))
    if not required:
        return []
    excluded = {e.lower() for e in (exclude or set())}

    scored: list[tuple[float, dict]] = []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "").strip()
        if not name or name.lower() in excluded:
            continue
        # AUTHORITY: only published candidates qualify. Unpublished/unknown
        # publication status is a refusal, never a fallback.
        if c.get("published") is not True:
            continue
        text = f"{name} {c.get('description') or ''}"
        tokens = _tokens(text)
        overlap = required & tokens
        if not overlap:
            continue  # CAPABILITY: no keyword evidence → not interchangeable
        score = len(overlap) / len(required)
        scored.append((score, {
            "kind": str(c.get("kind") or "specialist_agent"),
            "name": name,
            "rationale": (
                f"Published agent; capability match on {', '.join(sorted(overlap))} "
                f"for the failed objective"
            ),
            "source": str(c.get("source") or "agent_directory"),
        }))
    scored.sort(key=lambda pair: -pair[0])
    return [dict(c) for _, c in scored[: max(1, int(limit))]]


def fallback_permitted(tool_name: str, arguments: dict | None = None) -> bool:
    """Destructive operations are NEVER rerouted through a fallback."""
    try:
        from app.services.permissions.approval import resolve_risk

        return resolve_risk(tool_name, arguments or {}).level not in ("destructive",)
    except Exception:  # noqa: BLE001 — fail open for reads, the caller still gates
        return True


def candidates_from_agent_directory(directory: list[Any]) -> list[dict]:
    """Map the existing agent directory listing (delegate_tool._list_available_agents
    shape: builtin defs + published AgentApps) into selection candidates."""
    out: list[dict] = []
    for item in directory or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        out.append({
            "name": name,
            "description": str(item.get("description") or ""),
            # The directory already advertises only published/delegable apps
            # (same gate list_available_agents uses) — keep that authority.
            "published": True,
            "kind": "specialist_agent",
            "source": str(item.get("source") or "agent_directory"),
        })
    return out
