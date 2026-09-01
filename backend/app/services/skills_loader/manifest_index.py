"""Skill manifest index: scan ``backend/skills/*/`` once, cache, serve.

The skill loader already supports on-demand SKILL.md bodies.  This
module adds the *discovery* layer the planner needs:

* At process start, walk ``backend/skills/`` (and optionally
  ``backend/system_skills/``) and read each skill's ``manifest.yaml``.
* Build an in-process list of ``(name, one-line description)`` that the
  planner injects into every PLAN prompt.  Total payload is ~50 KB for
  the current 30+ skills.
* Expose ``load_skill_body(name)`` for the lazy body fetch (the
  existing ``load_skill_body_tool`` already does this on the tool
  side; this module is the catalog the planner uses to know *what* to
  ask for).

A manifest is a tiny YAML file:

    name: docx
    description: Create Word documents from markdown.
    version: 1.0
    tags: [file, document]
    inputs:
      - name: markdown
        type: string
    outputs:
      - name: file_url
        type: url

Anything missing a ``name`` or ``description`` is skipped — those two
fields are the planner's only contract.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)


@dataclass
class SkillManifest:
    name: str
    description: str
    version: str = "0.0"
    tags: list[str] = field(default_factory=list)
    inputs: list[dict] = field(default_factory=list)
    outputs: list[dict] = field(default_factory=list)
    path: str = ""  # absolute path to the skill directory

    def one_line(self) -> str:
        """Return ``name — description`` for the planner prompt."""
        desc = (self.description or "").strip().replace("\n", " ")
        if len(desc) > 120:
            desc = desc[:117] + "…"
        return f"{self.name} — {desc}"


class ManifestIndex:
    """Thread-safe in-process index of skill manifests."""

    def __init__(self, skills_dirs: Optional[list[str]] = None) -> None:
        self._lock = threading.Lock()
        self._index: dict[str, SkillManifest] = {}
        self._loaded = False
        # Default to scanning the canonical skill locations.  Callers can
        # override for tests.
        if skills_dirs is None:
            self._skills_dirs = self._default_dirs()
        else:
            self._skills_dirs = skills_dirs

    @staticmethod
    def _default_dirs() -> list[str]:
        here = Path(__file__).resolve()
        # backend/app/services/skills_loader/manifest_index.py
        # → backend/skills and backend/system_skills
        backend_root = here.parents[3]
        return [
            str(backend_root / "skills"),
            str(backend_root / "system_skills"),
        ]

    def ensure_loaded(self) -> None:
        """Load on first use; idempotent."""
        with self._lock:
            if self._loaded:
                return
            for d in self._skills_dirs:
                if not d or not os.path.isdir(d):
                    continue
                self._scan_dir(d)
            self._loaded = True
            logger.info(
                "manifest_index: loaded %d skills from %s",
                len(self._index), self._skills_dirs,
            )

    def _scan_dir(self, root: str) -> None:
        try:
            for entry in sorted(os.listdir(root)):
                full = os.path.join(root, entry)
                if not os.path.isdir(full):
                    continue
                manifest_path = os.path.join(full, "manifest.yaml")
                if not os.path.isfile(manifest_path):
                    continue
                try:
                    manifest = self._parse_manifest(manifest_path, full)
                except Exception as exc:
                    logger.warning(
                        "manifest_index: failed to parse %s (%s)", manifest_path, exc,
                    )
                    continue
                if manifest is None:
                    continue
                self._index[manifest.name] = manifest
        except Exception as exc:
            logger.warning("manifest_index: cannot scan %s (%s)", root, exc)

    @staticmethod
    def _parse_manifest(path: str, base: str) -> Optional[SkillManifest]:
        """Parse a single manifest.yaml.

        Tries PyYAML first; falls back to a minimal key-value parser
        for the subset of YAML we use (so we don't hard-depend on
        PyYAML in environments where it's not installed).
        """
        text = Path(path).read_text(encoding="utf-8")
        data: dict
        try:
            import yaml  # type: ignore

            data = yaml.safe_load(text) or {}
        except Exception:
            data = _minimal_yaml(text)
        if not isinstance(data, dict):
            return None
        name = (data.get("name") or os.path.basename(base)).strip()
        description = (data.get("description") or "").strip()
        if not name or not description:
            return None
        return SkillManifest(
            name=name,
            description=description,
            version=str(data.get("version", "0.0")),
            tags=list(data.get("tags", []) or []),
            inputs=list(data.get("inputs", []) or []),
            outputs=list(data.get("outputs", []) or []),
            path=base,
        )

    def get(self, name: str) -> Optional[SkillManifest]:
        self.ensure_loaded()
        return self._index.get(name)

    def all(self) -> list[SkillManifest]:
        self.ensure_loaded()
        return list(self._index.values())

    def as_plan_prompt(self, *, max_skills: int = 200) -> str:
        """Render the catalog for the PLAN prompt.

        Format::

            Available skills (load on demand via load_skill_body):
              - docx — Create Word documents from markdown.
              - pptx — Create PowerPoint decks from a slide spec.
              ...

        The output is bounded by ``max_skills`` so a runaway install
        can't blow out the prompt budget.
        """
        self.ensure_loaded()
        items = self._index.values()
        # Sort by name for deterministic prompts.
        ordered = sorted(items, key=lambda m: m.name)[:max_skills]
        lines = ["Available skills (load on demand via load_skill_body):"]
        for m in ordered:
            lines.append(f"  - {m.one_line()}")
        return "\n".join(lines)

    def search(self, query: str) -> list[SkillManifest]:
        """Naive keyword search; used by the find-skills tool."""
        self.ensure_loaded()
        q = (query or "").lower().strip()
        if not q:
            return []
        results: list[tuple[int, SkillManifest]] = []
        for m in self._index.values():
            score = 0
            hay = (m.name + " " + m.description + " " + " ".join(m.tags)).lower()
            for token in q.split():
                if token in hay:
                    score += 1
            if score > 0:
                results.append((score, m))
        results.sort(key=lambda pair: (-pair[0], pair[1].name))
        return [m for _, m in results]


def _minimal_yaml(text: str) -> dict:
    """Fallback parser: extract top-level ``key: value`` pairs.

    Enough for the manifest format we ship.  Avoids a hard PyYAML
    dependency for environments where the planner runs but YAML isn't
    installed.
    """
    out: dict = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        if line.startswith(" ") or line.startswith("\t"):
            # Nested mapping; skip in fallback mode.
            continue
        k, _, v = line.partition(":")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if v.startswith("[") and v.endswith("]"):
            v = [x.strip().strip('"').strip("'") for x in v[1:-1].split(",") if x.strip()]
        out[k] = v
    return out


# Module-level singleton so callers don't have to thread the index
# through every planner invocation.
_DEFAULT_INDEX: Optional[ManifestIndex] = None
_DEFAULT_INDEX_LOCK = threading.Lock()


def get_manifest_index() -> ManifestIndex:
    global _DEFAULT_INDEX
    with _DEFAULT_INDEX_LOCK:
        if _DEFAULT_INDEX is None:
            _DEFAULT_INDEX = ManifestIndex()
        return _DEFAULT_INDEX


__all__ = [
    "SkillManifest",
    "ManifestIndex",
    "get_manifest_index",
]
