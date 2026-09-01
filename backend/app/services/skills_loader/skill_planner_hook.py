"""Skill planner hook.

Wires the manifest index into the SynexiaFSM PLAN state so the planner
sees a one-line catalog of every available skill and may emit a
``load_skill`` plan node to fetch the full body on demand.

Contract:

* ``SkillPlannerHook.build_plan_prompt_extra()`` returns the string the
  planner should append to its PLAN prompt.  Currently a 30-line
  catalog; bounded by ``max_skills`` so a runaway install can't blow
  the budget.
* ``SkillPlannerHook.materialize_node(node)`` consumes a plan node of
  shape ``{"type": "load_skill", "skill": "docx"}`` and returns the
  full SKILL.md body.  The planner emits this node when a plan
  references a skill by name.

The hook is a *pure* layer over the manifest index: it doesn't call the
LLM, doesn't write to the DB, and doesn't surface side effects.  Tests
can construct an isolated hook with a custom ``ManifestIndex``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

from app.services.skills_loader.manifest_index import (
    ManifestIndex,
    SkillManifest,
    get_manifest_index,
)

logger = logging.getLogger(__name__)

_MAX_SKILLS_IN_PLAN = 200
_MAX_BODY_CHARS = 50_000  # cap so a runaway skill can't bloat context

# Skill subfolders that ship bulk connector/automation docs. They stay
# SEARCHABLE (unified_search / post_router_pick can still force-match them
# on demand) but are excluded from the planner prompt injection so the
# 800+ connector docs don't drown out the curated business skills
# (progressive disclosure: small catalog in prompt, full catalog on demand).
_PLANNER_EXCLUDE_SUBSTRINGS: tuple[str, ...] = ("composio-skills",)


def _is_planner_curated(skill: Any) -> bool:
    """True if a skill should appear in the planner catalog injection.

    Excludes bulk connector-doc collections (kept searchable on demand).
    """
    path = (getattr(skill, "file_path", "") or "").replace("\\", "/")
    return not any(sub in path for sub in _PLANNER_EXCLUDE_SUBSTRINGS)


@dataclass
class LoadSkillResult:
    name: str
    body: str
    version: str
    description: str


class SkillPlannerHook:
    """Inject skill awareness into the SynexiaFSM planner."""

    def __init__(self, index: Optional[ManifestIndex] = None, *, max_skills: int = _MAX_SKILLS_IN_PLAN) -> None:
        self.index = index or get_manifest_index()
        self.max_skills = max_skills

    # ── PLAN prompt augmentation ────────────────────────────────────────
    def build_plan_prompt_extra(self) -> str:
        """Return the catalog block to append to the PLAN prompt.

        Prefers ``SkillsRegistry.list_skills()`` for complete coverage —
        it scans both ``manifest.yaml`` packages and legacy ``SKILL.md``
        frontmatter, whereas ``ManifestIndex`` only covers
        ``manifest.yaml``.  Falls back to ``ManifestIndex`` if the
        registry is unavailable (e.g. not yet loaded).
        """
        # Primary: SkillsRegistry (richest source — manifest.yaml + SKILL.md)
        try:
            from app.services.skills_loader import get_skills_registry
            skills = get_skills_registry().list_skills()
            if skills:
                # Progressive disclosure: inject only the curated business
                # skills in the planner prompt. Bulk connector-doc
                # collections (e.g. composio-skills) stay searchable on
                # demand via unified_search / post_router_pick.
                skills = [s for s in skills if _is_planner_curated(s)]
                ordered = sorted(skills, key=lambda s: s.name)[: self.max_skills]
                lines = [
                    "Available skills (invoke as node_type=\"skill\" with the "
                    "skill name, or use the Skill tool to load methodology):"
                ]
                for s in ordered:
                    desc = (s.description or s.summary or "").strip().replace("\n", " ")
                    if len(desc) > 120:
                        desc = desc[:117] + "…"
                    lines.append(f"  - {s.name} — {desc}")
                return "\n".join(lines)
        except Exception as exc:
            logger.debug("SkillsRegistry unavailable for planner catalog: %s", exc)

        # Fallback: ManifestIndex (manifest.yaml-only)
        return self.index.as_plan_prompt(max_skills=self.max_skills)

    # ── Node materialization ────────────────────────────────────────────
    def materialize_node(self, node: dict[str, Any]) -> Optional[LoadSkillResult]:
        """Consume a ``load_skill`` plan node.

        Returns the body + metadata, or ``None`` when the skill is
        unknown / the body is missing.  The caller is expected to log
        and skip — never raise — so a bad plan never blocks the FSM.
        """
        if not isinstance(node, dict):
            return None
        if node.get("type") != "load_skill":
            return None
        name = (node.get("skill") or node.get("name") or "").strip()
        if not name:
            return None

        # Primary: SkillsRegistry (covers manifest.yaml + legacy SKILL.md).
        # ``SkillMetadata.body`` is already loaded from disk, so no extra
        # file read is needed here.
        try:
            from app.services.skills_loader import get_skills_registry
            skill = get_skills_registry().get(name)
            if skill is not None and skill.body:
                body = skill.body
                if len(body) > _MAX_BODY_CHARS:
                    body = body[:_MAX_BODY_CHARS] + "\n\n[…body truncated…]"
                return LoadSkillResult(
                    name=skill.name,
                    body=body,
                    version=skill.version,
                    description=skill.description,
                )
        except Exception as exc:
            logger.debug("SkillsRegistry lookup for %r failed: %s", name, exc)

        # Fallback: ManifestIndex → read body from disk
        manifest = self.index.get(name)
        if manifest is None:
            logger.info("skill_planner_hook: skill %r not in manifest", name)
            return None
        body = self._read_body(manifest)
        if body is None:
            logger.info("skill_planner_hook: skill %r has no SKILL.md body", name)
            return None
        return LoadSkillResult(
            name=manifest.name,
            body=body,
            version=manifest.version,
            description=manifest.description,
        )

    @staticmethod
    def _read_body(manifest: SkillManifest) -> Optional[str]:
        candidates = [
            os.path.join(manifest.path, "SKILL.md"),
            os.path.join(manifest.path, "skill.md"),
            os.path.join(manifest.path, "README.md"),
        ]
        for path in candidates:
            if not os.path.isfile(path):
                continue
            try:
                text = open(path, encoding="utf-8", errors="replace").read()
            except Exception as exc:
                logger.warning(
                    "skill_planner_hook: failed to read %s (%s)", path, exc,
                )
                continue
            if len(text) > _MAX_BODY_CHARS:
                text = text[:_MAX_BODY_CHARS] + "\n\n[…body truncated…]"
            return text
        return None


def get_skill_planner_hook() -> SkillPlannerHook:
    return SkillPlannerHook()


__all__ = [
    "LoadSkillResult",
    "SkillPlannerHook",
    "get_skill_planner_hook",
]
