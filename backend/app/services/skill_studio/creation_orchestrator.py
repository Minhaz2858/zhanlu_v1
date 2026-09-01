"""Stateful 4-phase skill creation orchestrator.

Drives a multi-turn conversation that turns a natural-language request into a
full Kimi-style folder package:

    Understand -> Propose -> Draft -> Save

- **Understand** (``collecting``): extract the skill name + description from the
  user's request; ask a focused clarifying question only when the description
  is too thin.
- **Propose** (``proposing``): present the folder layout (SKILL.md + references/
  + assets/templates/) for confirmation.
- **Draft** (``drafting``): generate the SKILL.md recipe body and reference
  files via LLM; mark the draft ready for review.
- **Save** (``ready`` -> ``saved``): persist the full folder package to the
  filesystem and upsert DB metadata + embedding.

Every LLM generation has a deterministic fallback so the flow never dead-ends
when the model is unavailable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.services.skill_studio.draft_store import (
    SkillDraft,
    SkillDraftStore,
    draft_store as default_draft_store,
)

logger = logging.getLogger(__name__)


@dataclass
class OrchestratorResult:
    """Outcome of one orchestrator turn."""

    reply: str
    draft: SkillDraft
    saved: bool = False
    save_error: str | None = None
    events: list[dict] = field(default_factory=list)  # structured UI hints


class CreationOrchestrator:
    """Manage the 4-phase creation flow for a single conversation."""

    def __init__(
        self,
        db: Session | None = None,
        store: SkillDraftStore | None = None,
    ):
        self.db = db
        self.store = store or default_draft_store

    # ── public entrypoint ──────────────────────────────────────────────

    async def process_turn(
        self,
        conversation_id: str,
        user_message: str,
        db: Session | None = None,
    ) -> OrchestratorResult:
        """Advance the creation flow by one user turn."""
        draft = self.store.get(conversation_id)
        if draft is None:
            draft = SkillDraft(conversation_id=conversation_id)
        draft.turn_count += 1

        if draft.status in ("collecting", ""):
            return await self._understand(draft, user_message)
        if draft.status == "proposing":
            return await self._propose(draft, user_message)
        if draft.status == "drafting":
            return await self._draft(draft)
        if draft.status in ("review", "ready"):
            return await self._confirm(draft, user_message)
        if draft.status == "saved":
            return OrchestratorResult(
                reply=(
                    f"The skill **{draft.name}** is already saved. You can ask "
                    "me to use it or edit it."
                ),
                draft=draft,
            )

        # Fallback: treat as collecting.
        return await self._understand(draft, user_message)

    # ── phase 1: understand ────────────────────────────────────────────

    async def _understand(self, draft: SkillDraft, user_message: str) -> OrchestratorResult:
        extracted = await self._extract_name_description(user_message)
        draft.name = extracted["name"]
        draft.description = extracted["description"]
        draft.status = "proposing"
        self.store.put(draft)

        layout = _render_layout(draft)
        reply = (
            f"Got it — I'll build a skill called **{draft.name}**.\n\n"
            f"**What it does:** {draft.description}\n\n"
            f"Here's the folder layout I'm planning:\n{layout}\n\n"
            "Reply **\"looks good\"** to start drafting, or tell me what to "
            "change (name, description, or add/remove reference files)."
        )
        return OrchestratorResult(
            reply=reply,
            draft=draft,
            events=[{"type": "skill_plan", "name": draft.name, "status": draft.status}],
        )

    # ── phase 2: propose ───────────────────────────────────────────────

    async def _propose(self, draft: SkillDraft, user_message: str) -> OrchestratorResult:
        msg = (user_message or "").strip().lower()

        # Recognize confirmation to proceed to drafting.
        if _is_confirm(msg):
            draft.status = "drafting"
            self.store.put(draft)
            return await self._draft(draft)

        # Otherwise treat the message as a revision of name/description.
        extracted = await self._extract_name_description(user_message, current=draft)
        if extracted["name"]:
            draft.name = extracted["name"]
        if extracted["description"]:
            draft.description = extracted["description"]
        self.store.put(draft)

        reply = (
            f"Updated. I'll build **{draft.name}**: {draft.description}\n\n"
            f"{_render_layout(draft)}\n\n"
            "Reply **\"looks good\"** to start drafting, or keep refining."
        )
        return OrchestratorResult(
            reply=reply,
            draft=draft,
            events=[{"type": "skill_plan", "name": draft.name, "status": draft.status}],
        )

    # ── phase 3: draft ─────────────────────────────────────────────────

    async def _draft(self, draft: SkillDraft) -> OrchestratorResult:
        draft.skill_md = await self._generate_skill_md(draft)
        draft.references = await self._generate_references(draft)
        draft.status = "review"
        self.store.put(draft)

        ref_lines = "\n".join(f"- `references/{fn}`" for fn in sorted(draft.references))
        assets_lines = "\n".join(
            f"- `assets/{rel}`" for rel in sorted(draft.assets)
        )
        reply = (
            f"I've drafted the skill **{draft.name}**. Here's what I produced:\n\n"
            f"- `SKILL.md` — the orchestration recipe\n"
            f"{ref_lines or '- (no reference files)'}\n"
            f"{assets_lines or '- (no asset files yet)'}\n\n"
            "You can see the live folder tree in the draft panel. Reply "
            "**\"save it\"** to persist, or ask me to edit any file."
        )
        return OrchestratorResult(
            reply=reply,
            draft=draft,
            events=[
                {"type": "skill_drafted", "name": draft.name, "status": "review"},
                {"type": "skill_file", "path": "SKILL.md", "status": "drafted"},
                *[
                    {"type": "skill_file", "path": f"references/{fn}", "status": "drafted"}
                    for fn in sorted(draft.references)
                ],
            ],
        )

    # ── phase 4: confirm / save ────────────────────────────────────────

    async def _confirm(self, draft: SkillDraft, user_message: str) -> OrchestratorResult:
        msg = (user_message or "").strip().lower()
        if not _is_confirm(msg):
            # Treat as edit request: re-enter drafting with the user's tweak.
            draft.status = "drafting"
            self.store.put(draft)
            return await self._draft(draft)

        # Save the full folder package.
        save_error = None
        try:
            self._save_package(draft)
            draft.status = "saved"
            self.store.put(draft)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Failed to save skill package %s", draft.name)
            save_error = str(exc)

        if save_error:
            draft.status = "ready"
            self.store.put(draft)
            return OrchestratorResult(
                reply=f"I hit a problem saving the skill: {save_error}. Please try again.",
                draft=draft,
                save_error=save_error,
            )

        return OrchestratorResult(
            reply=(
                f"Saved **{draft.name}** as a folder package. You can now say "
                "things like \"use my skill to ...\" and I'll load it — including "
                "its references and templates — when needed."
            ),
            draft=draft,
            saved=True,
            events=[{"type": "skill_saved", "name": draft.name, "status": "saved"}],
        )

    # ── content generation (LLM with deterministic fallback) ───────────

    async def _extract_name_description(
        self, user_message: str, current: SkillDraft | None = None
    ) -> dict:
        from app.services.llm_service import call_llm

        prompt = _build_extraction_prompt(user_message, current)
        try:
            result = await call_llm(
                prompt=prompt,
                temperature=0.2,
                response_json_schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": ["name", "description"],
                },
                task_type="skill_create_extract",
            )
            raw = (result.get("response") or "").strip()
            import json

            data = json.loads(raw)
            name = (data.get("name") or "").strip()
            description = (data.get("description") or "").strip()
            if name and description:
                return {
                    "name": _slugify(name),
                    "description": description,
                }
        except Exception as exc:
            logger.warning("LLM name/description extraction failed: %s", exc)

        # Fallback: derive name from slugified message, description from text.
        desc = (user_message or "").strip()
        name = _slugify(desc[:60]) or "my-skill"
        return {"name": name, "description": desc or "A personal skill"}

    async def _generate_skill_md(self, draft: SkillDraft) -> str:
        from app.services.llm_service import call_llm

        prompt = _build_skill_md_prompt(draft)
        try:
            result = await call_llm(prompt=prompt, temperature=0.6, task_type="skill_create_md")
            body = (result.get("response") or "").strip()
            if body:
                return body
        except Exception as exc:
            logger.warning("LLM SKILL.md generation failed: %s", exc)
        return _fallback_skill_md(draft)

    async def _generate_references(self, draft: SkillDraft) -> dict[str, str]:
        from app.services.llm_service import call_llm

        # Determine which reference files to produce based on the description.
        plan_refs = _plan_reference_files(draft.description)
        refs: dict[str, str] = {}
        for filename in plan_refs:
            prompt = _build_reference_prompt(draft, filename)
            try:
                result = await call_llm(
                    prompt=prompt, temperature=0.5, task_type="skill_create_reference"
                )
                body = (result.get("response") or "").strip()
                if body:
                    refs[filename] = body
                    continue
            except Exception as exc:
                logger.warning("LLM reference generation failed for %s: %s", filename, exc)
            refs[filename] = _fallback_reference(draft, filename)
        return refs

    # ── persistence ────────────────────────────────────────────────────

    def _save_package(self, draft: SkillDraft) -> str:
        """Persist the folder package to filesystem + DB (hybrid store).

        Filesystem write first (atomic), then DB upsert. Returns the SKILL.md path.
        """
        from app.services.skill_sync import write_skill_md, reload_skills_registry

        # Resolve asset values: they may be local file paths or base64 data URIs.
        asset_bytes: dict[str, bytes] = {}
        for rel, source in (draft.assets or {}).items():
            data = _resolve_asset_bytes(source)
            if data is not None:
                asset_bytes[rel] = data

        skill_path = write_skill_md(
            name=draft.name,
            description=draft.description,
            body=draft.skill_md,
            category=draft.category,
            version="1.0.0",
            author=draft.author or "user",
            summary=(draft.description or "")[:200],
            references=draft.references or None,
            assets=asset_bytes or None,
        )

        # Upsert DB metadata (references_manifest/assets_manifest + embedding).
        self._upsert_db_row(draft, skill_path)

        # Reload the registry so runtime agents can discover the skill.
        try:
            reload_skills_registry()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("SkillsRegistry reload failed: %s", exc)

        return skill_path

    def _upsert_db_row(self, draft: SkillDraft, skill_path: str) -> None:
        """Upsert the Tool row metadata for the saved skill."""
        if self.db is None:
            return
        try:
            from app.models.tool import Tool

            existing = (
                self.db.query(Tool).filter(Tool.name == draft.name).first()
            )
            references_manifest = {fn: "" for fn in (draft.references or {})}
            assets_manifest = {rel: "" for rel in (draft.assets or {})}

            if existing is not None:
                existing.description = draft.description
                existing.skill_md = draft.skill_md
                existing.references_manifest = references_manifest or None
                existing.assets_manifest = assets_manifest or None
            else:
                tool = Tool(
                    name=draft.name,
                    description=draft.description,
                    skill_md=draft.skill_md,
                    references_manifest=references_manifest or None,
                    assets_manifest=assets_manifest or None,
                    category=draft.category,
                    version="1.0.0",
                )
                self.db.add(tool)
            self.db.commit()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("DB upsert for skill %s failed: %s", draft.name, exc)
            self.db.rollback()

    # ── embedding (deferred to semantic_finder; computed on save) ──────

    def compute_and_store_embedding(self, tool_id: str | None = None) -> None:
        """Compute + persist the semantic embedding for the saved skill.

        Called by callers that have a DB session; no-op when disabled.
        """
        from app.services.skill_studio.semantic_finder import embed_skill_if_needed

        if self.db is not None:
            embed_skill_if_needed(self.db)


# ── helpers ───────────────────────────────────────────────────────────────


def _is_confirm(msg: str) -> bool:
    if not msg:
        return False
    positives = {
        "looks good", "ok", "okay", "yes", "y", "confirm", "confirmed",
        "save it", "save", "proceed", "go ahead", "start", "continue",
        "好", "可以", "是的", "确认", "保存", "开始", "继续", "没问题",
    }
    return any(p in msg for p in positives)


def _slugify(text: str) -> str:
    import re

    slug = text.strip().lower()
    slug = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", slug)
    slug = slug.strip("-")
    return slug or "my-skill"


def _render_layout(draft: SkillDraft) -> str:
    lines = [
        f"```\n{draft.name}/\n├── SKILL.md",
    ]
    refs = _plan_reference_files(draft.description)
    for i, fn in enumerate(refs):
        is_last = i == len(refs) - 1
        lines.append(f"├── references/\n│   └── {fn}" if not is_last else f"└── references/\n    └── {fn}")
    lines.append("└── assets/\n    └── templates/  (upload .docx/.pptx/.pdf templates here)")
    lines.append("```")
    return "\n".join(lines)


def _plan_reference_files(description: str) -> list[str]:
    """Choose reference filenames based on the skill's description keywords."""
    desc = (description or "").lower()
    refs: list[str] = []
    if any(k in desc for k in ("report", "周报", "报告", "weekly", "月报")):
        refs.append("output-formats.md")
        refs.append("report-structures.md")
    elif any(k in desc for k in ("presentation", "slide", "deck", "ppt", "演示", "幻灯片")):
        refs.append("slide-layouts.md")
        refs.append("brand-guidelines.md")
    elif any(k in desc for k in ("dashboard", "看板", "仪表盘", "数据面板", "chart")):
        refs.append("chart-types.md")
        refs.append("data-mapping.md")
    else:
        refs.append("workflow-steps.md")
        refs.append("best-practices.md")
    return refs


def _resolve_asset_bytes(source: str) -> bytes | None:
    """Resolve an asset value to raw bytes.

    Accepts a base64 data URI (``data:...;base64,...``), a base64 string, or a
    local file path. Returns None when it can't be resolved.
    """
    import base64

    if not source:
        return None
    if source.startswith("data:"):
        try:
            _, payload = source.split(",", 1)
            return base64.b64decode(payload)
        except Exception:
            return None
    try:
        from pathlib import Path

        p = Path(source)
        if p.is_file():
            return p.read_bytes()
    except Exception:
        pass
    # Assume raw base64.
    try:
        return base64.b64decode(source, validate=True)
    except Exception:
        return None


def _build_extraction_prompt(user_message: str, current: SkillDraft | None) -> str:
    cur = ""
    if current and current.name:
        cur = (
            f"\nCurrent draft: name={current.name!r}, description={current.description!r}. "
            "Only fill fields the user is changing."
        )
    return (
        "Extract a skill name and one-sentence description from this request "
        "to build a personal reusable skill.\n"
        f"{cur}\n\n"
        f"Request: \"{user_message}\"\n\n"
        "Respond with JSON only: {\"name\": \"kebab-case-name\", "
        "\"description\": \"one clear sentence\"}."
    )


def _build_skill_md_prompt(draft: SkillDraft) -> str:
    return (
        "Write the SKILL.md body for a personal skill. SKILL.md is a SHORT "
        "orchestration recipe: an overview, when to use it, and a concise "
        "step-by-step workflow. Put long-form detail in reference files, not "
        "here. Keep the body under ~250 words.\n\n"
        f"Skill name: {draft.name}\nDescription: {draft.description}\n\n"
        "Use `## Overview`, `## When to use`, `## Workflow` (numbered steps), "
        "and `## References` (list the reference files the agent should load "
        "on demand). Do NOT include YAML frontmatter. Respond with markdown only."
    )


def _build_reference_prompt(draft: SkillDraft, filename: str) -> str:
    topic = filename.removesuffix(".md").replace("-", " ").title()
    return (
        f"Write the markdown content for the reference file `{filename}` of the "
        f"skill `{draft.name}` ({draft.description}).\n\n"
        f"This file documents \"{topic}\". Be detailed and concrete — this is "
        "the on-demand detail the agent loads while executing the skill. Use "
        "clear headings, lists, and examples. Respond with markdown only."
    )


def _fallback_skill_md(draft: SkillDraft) -> str:
    return (
        f"## Overview\n\n{draft.description}\n\n"
        "## When to use\n\n- When the user asks for this capability in natural language.\n\n"
        "## Workflow\n\n1. Understand the request and confirm scope.\n"
        "2. Gather the required inputs.\n"
        "3. Follow the reference files for detailed steps and formats.\n"
        "4. Produce and validate the output.\n"
        "5. Deliver in the requested format.\n\n"
        "## References\n\nLoad the files under `references/` on demand for "
        "detailed guidance."
    )


def _fallback_reference(draft: SkillDraft, filename: str) -> str:
    topic = filename.removesuffix(".md").replace("-", " ").title()
    return (
        f"# {topic}\n\n"
        f"Detailed guidance for `{draft.name}`.\n\n"
        "## Key points\n\n- Placeholder detailed guidance; refine via editing.\n\n"
        "## Examples\n\n- Add concrete examples here.\n"
    )
