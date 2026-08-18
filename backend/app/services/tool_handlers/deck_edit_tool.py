"""Deck edit tools — six deterministic per-slide edit tools for generated decks.

Every edit treats the ``DeckPlan`` persisted in the latest
``ArtifactVersion.source_json`` as the single source of truth; the
``.pptx`` binary is never re-parsed or re-exported through the free-form
pipeline.  The shared pipeline for every tool:

  1. load the artifact + guards (missing / cross-tenant / failed / no plan)
  2. apply a pure mutation from ``app.services.artifacts.editors.deck_edit``
  3. re-render via ``render_pptx_from_plan_sync``
  4. create a new version + store the ``original`` blob
  5. render fresh page thumbnails
  6. return a canonical result dict + a templated reply

Tools are registered with ``enabled_by_default=False`` and gated at the
routing layer by ``DECK_EDIT_ROUTING_ENABLED`` (see
``app.services.synexia.default_skills``), so they only ever surface when
explicitly enabled.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from app.services.artifacts.artifact_service import ArtifactService
from app.services.artifacts.editors import deck_edit as edit_ops
from app.services.artifacts.editors.deck_edit import DeckEditError
from app.services.artifacts.event_logger import log_deck_event_fire_and_forget
from app.services.artifacts.render_dispatcher import render_pptx_from_plan_sync
from app.services.artifacts.thumbnails import render_page_thumbnails
from app.services.synexia.contracts import DeckPlan
from app.services.tool_registry import registry

logger = logging.getLogger(__name__)

PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
THUMB_MIME = "image/png"

# ---------------------------------------------------------------------------
# Bilingual reply templates (en / zh).  `{tool}.success` is used after a
# successful edit, `{tool}.failure` for tool-level failures (guards return
# their own actionable messages).
# ---------------------------------------------------------------------------
EDIT_REPLIES: dict[str, dict[str, dict[str, str]]] = {
    "edit_slide": {
        "success": {
            "en": "Slide {n} updated. Version {version_number} is ready — [preview]({preview_url}).",
            "zh": "第 {n} 页已更新。版本 {version_number} 已就绪——[预览]({preview_url})。",
        },
        "failure": {
            "en": "Could not edit slide {n}: {reason}",
            "zh": "无法编辑第 {n} 页：{reason}",
        },
    },
    "add_slide": {
        "success": {
            "en": "Slide added. Version {version_number} is ready — [preview]({preview_url}).",
            "zh": "已添加页面。版本 {version_number} 已就绪——[预览]({preview_url})。",
        },
        "failure": {
            "en": "Could not add the slide: {reason}",
            "zh": "无法添加页面：{reason}",
        },
    },
    "restyle_deck": {
        "success": {
            "en": "Deck restyled. Version {version_number} is ready — [preview]({preview_url}).",
            "zh": "演示文稿已重新换肤。版本 {version_number} 已就绪——[预览]({preview_url})。",
        },
        "failure": {
            "en": "Could not restyle the deck: {reason}",
            "zh": "无法更换主题：{reason}",
        },
    },
    "update_chart": {
        "success": {
            "en": "Chart on slide {n} updated. Version {version_number} is ready — [preview]({preview_url}).",
            "zh": "第 {n} 页图表已更新。版本 {version_number} 已就绪——[预览]({preview_url})。",
        },
        "failure": {
            "en": "Could not update the chart: {reason}",
            "zh": "无法更新图表：{reason}",
        },
    },
    "remove_slide": {
        "success": {
            "en": "Slide {n} removed. Version {version_number} is ready — [preview]({preview_url}).",
            "zh": "第 {n} 页已删除。版本 {version_number} 已就绪——[预览]({preview_url})。",
        },
        "failure": {
            "en": "Could not remove slide {n}: {reason}",
            "zh": "无法删除第 {n} 页：{reason}",
        },
    },
    "reorder_slide": {
        "success": {
            "en": "Slides reordered. Version {version_number} is ready — [preview]({preview_url}).",
            "zh": "页面顺序已调整。版本 {version_number} 已就绪——[预览]({preview_url})。",
        },
        "failure": {
            "en": "Could not reorder the slides: {reason}",
            "zh": "无法调整页面顺序：{reason}",
        },
    },
}

# ---------------------------------------------------------------------------
# Tool definitions: (name, description, properties, required)
# ---------------------------------------------------------------------------
_TOOL_DEFS: list[tuple[str, str, dict[str, Any], list[str]]] = [
    (
        "edit_slide",
        "Edit a specific slide of a generated deck: title, subtitle, bullets, notes or KPI specs. "
        "Reads the persisted DeckPlan and re-renders a new artifact version.",
        {
            "artifact_id": {"type": "string", "description": "ID of the generated deck artifact"},
            "slide_index": {"type": "integer", "description": "0-based slide index to edit"},
            "changes": {
                "type": "object",
                "description": "Fields to patch: title, subtitle, bullets (list[str]), notes, kpi_specs (list of {label, value, unit?, delta?})",
                "properties": {
                    "title": {"type": "string"},
                    "subtitle": {"type": "string"},
                    "bullets": {"type": "array", "items": {"type": "string"}},
                    "notes": {"type": "string"},
                    "kpi_specs": {"type": "array", "items": {"type": "object"}},
                },
                "additionalProperties": False,
            },
        },
        ["artifact_id", "slide_index", "changes"],
    ),
    (
        "add_slide",
        "Insert a new slide into a generated deck. When slide_index is omitted the slide is added "
        "right before the closing slide.",
        {
            "artifact_id": {"type": "string", "description": "ID of the generated deck artifact"},
            "slide_index": {
                "type": "integer",
                "description": "0-based insertion index (optional; default = before the closing slide)",
            },
            "slide": {
                "type": "object",
                "description": "New slide definition. layout must be one of: cover, agenda, kpi_grid, "
                "chart_full, chart_with_bullets, findings_cards, insights_bullets, recommendations, "
                "data_table, methodology, section_divider, closing.",
                "properties": {
                    "layout": {"type": "string"},
                    "title": {"type": "string"},
                    "subtitle": {"type": "string"},
                    "bullets": {"type": "array", "items": {"type": "string"}},
                    "notes": {"type": "string"},
                },
                "required": ["layout", "title"],
                "additionalProperties": False,
            },
        },
        ["artifact_id", "slide"],
    ),
    (
        "restyle_deck",
        "Change a generated deck's theme, headline style, summary or methodology and re-render it.",
        {
            "artifact_id": {"type": "string", "description": "ID of the generated deck artifact"},
            "theme": {"type": "string", "description": "Theme name, e.g. zhanlu-blue, midnight, sunset"},
            "headline_style": {
                "type": "string",
                "description": "Headline style: topic, assertion or inspirational",
            },
            "summary": {"type": "string", "description": "New executive summary text"},
            "methodology": {"type": "string", "description": "New methodology text"},
        },
        ["artifact_id"],
    ),
    (
        "update_chart",
        "Change the chart on a specific slide of a generated deck: chart_type, x_key, y_keys or title.",
        {
            "artifact_id": {"type": "string", "description": "ID of the generated deck artifact"},
            "slide_index": {"type": "integer", "description": "0-based slide index containing the chart"},
            "chart": {
                "type": "object",
                "description": "Chart spec to apply. chart_type one of: bar, line, pie, donut, area, column.",
                "properties": {
                    "chart_type": {"type": "string"},
                    "x_key": {"type": "string"},
                    "y_keys": {"type": "array", "items": {"type": "string"}},
                    "title": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        ["artifact_id", "slide_index", "chart"],
    ),
    (
        "remove_slide",
        "Delete a slide from a generated deck. The cover slide (index 0) and the closing slide "
        "(last index) are protected and cannot be removed.",
        {
            "artifact_id": {"type": "string", "description": "ID of the generated deck artifact"},
            "slide_index": {"type": "integer", "description": "0-based slide index to remove"},
        },
        ["artifact_id", "slide_index"],
    ),
    (
        "reorder_slide",
        "Move a slide in a generated deck to a new position. The cover slide stays at index 0 and "
        "the closing slide stays at the last index.",
        {
            "artifact_id": {"type": "string", "description": "ID of the generated deck artifact"},
            "from_index": {"type": "integer", "description": "0-based current index of the slide to move"},
            "to_index": {"type": "integer", "description": "0-based target index"},
        },
        ["artifact_id", "from_index", "to_index"],
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_artifact_id(raw: Any) -> str:
    """LLMs sometimes pass artifact_id as an int; the service expects a str."""
    if raw is None:
        return ""
    if isinstance(raw, bool):
        return str(raw)
    return str(raw)


def _fail(raw_id: Any, message: str) -> dict:
    return {"success": False, "artifact_id": raw_id, "message": message}


def _tenant_error(artifact: Any, context: dict) -> Optional[str]:
    """Return an error message when the artifact does not belong to this context.

    The ``Artifact`` model inherits ``org_id``/``app_id`` from
    ``TimestampedBase``; the agent-loop context carries the same fields via
    ``data_ctx_extras``.  Ownership is also verified through
    ``created_by_agent_id`` / ``conversation_id`` (both only checked when
    both sides are present, so legacy artifacts stay editable).
    """
    ctx_org = context.get("org_id")
    ctx_app = context.get("app_id")
    org = getattr(artifact, "org_id", None)
    app = getattr(artifact, "app_id", None)
    if org and ctx_org and org != ctx_org:
        return "Permission denied: artifact belongs to another tenant"
    if app and ctx_app and app != ctx_app:
        return "Permission denied: artifact belongs to another tenant"

    ctx_agent = context.get("agent_app_id")
    agent = getattr(artifact, "created_by_agent_id", None)
    if agent and ctx_agent and agent != ctx_agent:
        return "Permission denied: artifact was created by another agent"

    ctx_conv = context.get("conversation_id")
    conv = getattr(artifact, "conversation_id", None)
    if conv and ctx_conv and conv != ctx_conv:
        return "Permission denied: artifact belongs to another conversation"

    return None


def _pick_lang(context: dict) -> str:
    meta = context.get("conversation_metadata") or {}
    lang = context.get("lang") or meta.get("lang") or "en"
    return "zh" if str(lang).lower().startswith("zh") else "en"


def _t(tool: str, key: str, context: dict, **fmt: Any) -> str:
    lang = _pick_lang(context)
    template = EDIT_REPLIES.get(tool, {}).get(key, {}).get(lang)
    if template is None:
        template = EDIT_REPLIES.get(tool, {}).get(key, {}).get("en", "")
    return template.format(**fmt)


def _changelog(tool: str, args: dict) -> str:
    if tool == "edit_slide":
        return f"Edited slide {args.get('slide_index')}"
    if tool == "add_slide":
        return "Added slide"
    if tool == "restyle_deck":
        return "Restyled deck"
    if tool == "update_chart":
        return f"Updated chart on slide {args.get('slide_index')}"
    if tool == "remove_slide":
        return f"Removed slide {args.get('slide_index')}"
    if tool == "reorder_slide":
        return f"Reordered slides ({args.get('from_index')} -> {args.get('to_index')})"
    return f"Edited deck via {tool}"


def _edit_instruction(tool: str, args: dict) -> str:
    return f"User requested deck edit: {_changelog(tool, args)}"


def _materialize_rows(plan: DeckPlan) -> list[dict[str, Any]]:
    """Collect the materialized chart/table rows persisted on each slide."""
    rows: list[dict[str, Any]] = []
    for slide in plan.slides:
        for row in slide.chart_rows or []:
            rows.append(dict(row))
        for row in slide.table_rows or []:
            rows.append(dict(row))
    return rows


def _rebuild_ctx(plan: DeckPlan) -> dict:
    """Rebuild a minimal layout context from the persisted plan.

    The full ExportContext is not persisted, but the layout engine only
    needs the theme tokens + style recipe; unknown themes fall back to the
    default palette inside ``_build_layout_ctx``.
    """
    return {
        "theme_tokens": {"name": plan.theme_recommendation or "zhanlu-blue"},
        "style_recipe": "sharp",
    }


def _apply_mutation(tool: str, plan: DeckPlan, args: dict) -> DeckPlan:
    """Dispatch to the pure mutation layer; raises DeckEditError on refusal."""
    if tool == "edit_slide":
        return edit_ops.edit_slide(plan, args["slide_index"], args["changes"])
    if tool == "add_slide":
        return edit_ops.add_slide(plan, args.get("slide_index"), args["slide"])
    if tool == "restyle_deck":
        changes: dict[str, Any] = {}
        if args.get("theme"):
            changes["theme_recommendation"] = args["theme"]
        if args.get("headline_style"):
            changes["headline_style"] = args["headline_style"]
        if args.get("summary"):
            changes["summary"] = args["summary"]
        if args.get("methodology"):
            changes["methodology"] = args["methodology"]
        if not changes:
            raise DeckEditError(
                "restyle_deck requires at least one of: theme, headline_style, summary, methodology"
            )
        return edit_ops.restyle_deck(plan, changes)
    if tool == "update_chart":
        return edit_ops.update_chart(plan, args["slide_index"], args["chart"])
    if tool == "remove_slide":
        return edit_ops.remove_slide(plan, args["slide_index"])
    if tool == "reorder_slide":
        return edit_ops.reorder_slide(plan, args["from_index"], args["to_index"])
    raise DeckEditError(f"unknown deck edit tool: {tool}")


# ---------------------------------------------------------------------------
# Shared edit pipeline
# ---------------------------------------------------------------------------


async def _run_deck_edit(
    tool: str,
    args: dict,
    db,
    user_id: Optional[str],
    context: dict,
) -> dict:
    raw_id = args.get("artifact_id")
    artifact_id = _normalize_artifact_id(raw_id)
    service = ArtifactService(db)

    # 1) artifact exists?
    try:
        artifact = service.get_artifact(artifact_id)
    except Exception as exc:
        logger.error("[deck_edit:%s] get_artifact(%s) failed: %s", tool, artifact_id, exc)
        return _fail(raw_id, f"Could not load artifact {raw_id}: {exc}")
    if artifact is None:
        return _fail(raw_id, f"Artifact {raw_id} not found — nothing to edit.")

    # 2) cross-tenant guard
    tenant_err = _tenant_error(artifact, context)
    if tenant_err:
        logger.warning("[deck_edit:%s] tenant guard rejected artifact %s", tool, artifact_id)
        return _fail(raw_id, tenant_err)

    # 3) failed artifacts are read-only
    if getattr(artifact, "status", None) == "failed":
        return _fail(
            raw_id,
            "This artifact previously failed to build and cannot be edited. "
            "Please regenerate it instead.",
        )

    # 4) single source of truth: the persisted DeckPlan
    try:
        version = service.get_current_version(artifact_id)
    except Exception as exc:
        logger.warning("[deck_edit:%s] get_current_version(%s) failed: %s", tool, artifact_id, exc)
        return _fail(raw_id, f"Could not load the current version: {exc}")
    source_json = {}
    if version is not None:
        source_json = version.source_json if isinstance(version.source_json, dict) else {}
    plan_dict = source_json.get("deck_plan")
    if not plan_dict:
        return _fail(
            raw_id,
            "No stored deck plan found for this artifact — it was probably not generated as a "
            "structured deck, so it cannot be edited in place.",
        )
    try:
        plan = DeckPlan.model_validate(plan_dict)
    except Exception as exc:
        logger.warning("[deck_edit:%s] invalid deck plan for %s: %s", tool, artifact_id, exc)
        return _fail(raw_id, f"Stored deck plan is invalid: {exc}")

    # 5) apply the deterministic mutation
    try:
        new_plan = _apply_mutation(tool, plan, args)
    except DeckEditError as exc:
        logger.info("[deck_edit:%s] mutation refused for %s: %s", tool, artifact_id, exc)
        return _fail(raw_id, str(exc))
    except Exception as exc:  # e.g. pydantic ValidationError
        logger.warning("[deck_edit:%s] mutation failed for %s: %s", tool, artifact_id, exc)
        return _fail(raw_id, f"Edit rejected: {exc}")

    # 6) re-render (single render pass for the edited plan)
    rows = _materialize_rows(new_plan)
    ctx = _rebuild_ctx(new_plan)
    try:
        pptx_bytes, report = render_pptx_from_plan_sync(
            new_plan, rows, ctx, user_message=_edit_instruction(tool, args)
        )
    except Exception as exc:
        logger.error("[deck_edit:%s] render failed for %s: %s", tool, artifact_id, exc)
        return _fail(raw_id, f"Re-render failed: {exc}")
    if not pptx_bytes:
        return _fail(raw_id, "Re-render produced an empty file — please try again.")

    # 7) persist a new version + original blob
    try:
        new_version = service.create_version(
            artifact_id=artifact_id,
            changelog=_changelog(tool, args),
            source_json={"deck_plan": new_plan.model_dump(mode="json")},
            produced_by_skill="deck_edit",
        )
    except Exception as exc:
        logger.error("[deck_edit:%s] create_version failed for %s: %s", tool, artifact_id, exc)
        return _fail(raw_id, f"Failed to create a new version: {exc}")
    if new_version is None:
        return _fail(raw_id, "Failed to create a new version for this edit.")

    file_name = f"{getattr(artifact, 'title', None) or 'deck'}.pptx"
    try:
        service.store_blob(new_version.id, "original", file_name, PPTX_MIME, pptx_bytes)
    except Exception as exc:
        logger.error("[deck_edit:%s] store original blob failed for %s: %s", tool, new_version.id, exc)
        return _fail(raw_id, f"Failed to store the rendered file: {exc}")

    # 8) fresh thumbnails (best-effort, never blocks the edit result)
    try:
        thumbs = render_page_thumbnails("pptx", pptx_bytes) or []
    except Exception as exc:
        logger.warning("[deck_edit:%s] thumbnails failed for %s: %s", tool, new_version.id, exc)
        thumbs = []
    for i, thumb in enumerate(thumbs):
        try:
            service.store_blob(new_version.id, "thumbnail", f"slide-{i + 1}.png", THUMB_MIME, thumb)
        except Exception as exc:
            logger.warning("[deck_edit:%s] thumbnail store failed for %s: %s", tool, new_version.id, exc)

    try:
        service.mark_version_built(new_version.id, validation_report=report)
    except Exception as exc:
        logger.warning("[deck_edit:%s] mark_version_built failed for %s: %s", tool, new_version.id, exc)

    preview_url = f"/api/artifacts/{artifact_id}/preview"
    download_url = f"/api/artifacts/{artifact_id}/download"
    message = _t(
        tool,
        "success",
        context,
        n=args.get("slide_index", ""),
        version_number=getattr(new_version, "version_number", ""),
        preview_url=preview_url,
    )
    return {
        "success": True,
        "artifact_id": getattr(artifact, "id", raw_id),
        "version_id": getattr(new_version, "id", None),
        "version_number": getattr(new_version, "version_number", None),
        "file_url": download_url,
        "preview_url": preview_url,
        "download_url": download_url,
        "file_name": file_name,
        "mime_type": PPTX_MIME,
        "file_size": len(pptx_bytes),
        "message": message,
    }

    # Phase 5 — usage instrumentation (fire-and-forget).
    try:
        org_id = (context or {}).get("org_id", "default-org")
        app_id = (context or {}).get("app_id", "default-app")
        log_deck_event_fire_and_forget(
            None, "deck_edited", artifact_id=getattr(artifact, "id", None),
            user_id=user_id,
            metadata={"edit_kind": tool, "version_number": getattr(new_version, "version_number", None)},
            org_id=org_id, app_id=app_id,
        )
    except Exception as exc:  # noqa: BLE001 — instrumentation must not break the edit
        logger.warning("[deck_edit:%s] event log failed: %s", tool, exc)


def _make_handler(tool: str):
    async def _handler(
        args: dict,
        db,
        user_id: Optional[str],
        context: Optional[dict] = None,
    ) -> dict:
        return await _run_deck_edit(tool, args or {}, db, user_id, context or {})

    return _handler


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def _register() -> None:
    for name, description, properties, required in _TOOL_DEFS:
        registry.register(
            name=name,
            schema={
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                        "additionalProperties": False,
                    },
                },
            },
            handler=_make_handler(name),
            category="artifact_edit",
            toolset="artifact_edit",
            enabled_by_default=False,
            description=description,
            emoji="\u270f\ufe0f",
            max_result_size_chars=4000,
        )


_register()
