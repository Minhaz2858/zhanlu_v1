"""Processor for [[RESULT]] ... [[END]] blocks emitted by LLMs.

Some LLM system prompts instruct the model to emit a `[[RESULT]]` block at
the end of its reply to declare that a file/agent/automation/etc. has been
created.  The block looks like::

    [[RESULT]]
    {"type":"file","id":"...","name":"Sales_Report.docx","fields":{...}}
    [[END]]

The LLM-generated ``id`` is a *hallucinated* UUID that does NOT correspond
to any row in the ``artifacts`` table.  Without a post-processor the
frontend renders the card, fires a GET to ``/api/artifacts/<hallucinated>``,
and surfaces "Failed to load artifact: HTTP 404" to the user.

This module bridges that gap: any ``[[RESULT]]`` block with ``type: "file"``
is converted into a real ``create_artifact`` call (or skipped when no
renderable content is present), and the block's ``id`` is rewritten to the
real artifact id so the frontend's lookup succeeds.

The function is intentionally non-fatal: a malformed block or a failed
``create_artifact`` call is logged and the original text is left intact,
so the user always sees a reply.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Matches [[RESULT]]...[[END]] blocks.  Uses non-greedy match so two blocks
# in the same message don't collapse into one.  The body is captured
# verbatim; we JSON-parse it ourselves to surface clean errors.
RESULT_BLOCK_PATTERN = re.compile(
    r"\[\[RESULT\]\]\s*\n?([\s\S]*?)\[\[END\]\]",
    re.DOTALL,
)

# File types the artifact pipeline can actually render.  Unknown types are
# logged and left untouched in the text so the user still sees the result
# description; we never silently delete content.
_RENDERABLE_FILE_TYPES = {"docx", "pptx", "html", "pdf", "md", "xlsx"}


@dataclass
class ResultBlock:
    """A parsed [[RESULT]] block."""

    type: str                       # "file" / "agent" / "automation" / ...
    id: str                         # the (possibly hallucinated) id
    name: str
    fields: dict[str, Any]
    draft: bool
    start: int                      # char offset of "[[RESULT]]"
    end: int                        # char offset one past "[[END]]"
    raw: str                        # the full raw block

    @property
    def is_renderable_file(self) -> bool:
        return (
            self.type == "file"
            and isinstance(self.fields, dict)
            and (self.fields.get("file_type") or "").lower() in _RENDERABLE_FILE_TYPES
        )


def find_result_blocks(text: str) -> list[ResultBlock]:
    """Return all [[RESULT]] ... [[END]] blocks in ``text`` (in order).

    Malformed blocks (non-JSON, missing type) are silently skipped.
    """
    if not text:
        return []
    out: list[ResultBlock] = []
    for m in RESULT_BLOCK_PATTERN.finditer(text):
        raw_body = m.group(1).strip()
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            logger.debug("result_block: skipping malformed JSON (%s)", exc)
            continue
        if not isinstance(payload, dict):
            continue
        btype = str(payload.get("type") or "").strip()
        if not btype:
            continue
        fields = payload.get("fields") or {}
        if isinstance(fields, str):
            try:
                fields = json.loads(fields)
            except json.JSONDecodeError:
                fields = {}
        if not isinstance(fields, dict):
            fields = {}
        out.append(
            ResultBlock(
                type=btype,
                id=str(payload.get("id") or ""),
                name=str(payload.get("name") or ""),
                fields=fields,
                draft=bool(payload.get("draft")),
                start=m.start(),
                end=m.end(),
                raw=m.group(0),
            )
        )
    return out


def strip_result_blocks(text: str) -> str:
    """Remove all [[RESULT]] ... [[END]] blocks from ``text``.

    The frontend no longer needs the block once the post-processor has
    rewritten the artifact ids in the assistant text; the backend stores
    artifacts in the database and returns them via the artifacts endpoint.
    """
    if not text:
        return text
    cleaned = RESULT_BLOCK_PATTERN.sub("", text)
    # Tidy up the 3+ blank lines that removals often leave behind.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _hallucinated_id_is_suspicious(block: ResultBlock) -> bool:
    """Return True when the block's id looks hallucinated.

    A real artifact id is a UUIDv4 returned by ``uuid4().hex`` (no hyphens)
    or with hyphens.  The LLM is not supposed to fabricate one — but when
    it does, the id is usually a *random* UUID that simply doesn't exist
    in the artifacts table.  We can't know the truth without a DB lookup,
    so we lean on a cheap heuristic: any block of type=file with embedded
    content that has NOT been linked to a real artifact (link_to_message
    row absent) is treated as suspicious and forwarded to the
    create_artifact pipeline.  This function exists to give call sites a
    single place to swap the heuristic later (e.g. after we add a
    db-based id-existence check).
    """
    # Always treat file blocks with embedded content as candidates — the
    # frontend cannot render them without a real artifact, so it's safer
    # to (re)create the artifact than to leave a 404 in the UI.
    if not block.is_renderable_file:
        return False
    # A block with no id at all is trivially hallucinated.
    if not block.id:
        return True
    # A block with embedded content (markdown body, html, …) and no
    # file_url is suspicious: a real create_artifact result always
    # carries a file_url.
    has_embedded = any(
        block.fields.get(k) for k in ("content", "html_content", "markdown", "md_content")
    )
    has_url = bool(block.fields.get("file_url") or block.fields.get("preview_url"))
    return bool(has_embedded) and not has_url


def _build_create_artifact_args(block: ResultBlock) -> Optional[dict]:
    """Translate a renderable file block into ``create_artifact`` args.

    Returns ``None`` when we can't construct a meaningful payload (e.g.
    embedded content is missing entirely).  The caller logs and skips
    such blocks.
    """
    ftype = (block.fields.get("file_type") or "").lower()
    if ftype not in _RENDERABLE_FILE_TYPES:
        return None

    # Pick the first non-empty embedded body.  Skills vary in what they
    # use; we look for the four most common keys.
    content = (
        block.fields.get("content")
        or block.fields.get("html_content")
        or block.fields.get("markdown")
        or block.fields.get("md_content")
    )
    if not content and not block.fields.get("file_url"):
        # Nothing to render and no file_url to point at — nothing to do.
        return None

    payload: dict[str, Any] = {
        "title": block.name or f"{ftype}-artifact",
        "filename": block.name or f"{ftype}-artifact.{ftype}",
    }
    if content:
        # Map embedded content to the right create_artifact field.
        if ftype == "html":
            payload["html_content"] = content
        elif ftype == "md":
            payload["markdown"] = content
        else:
            # docx / pptx / pdf / xlsx all accept markdown as input
            # (the docx/pptx pipeline runs pandoc/python-pptx, others
            # accept a markdown body that the renderer turns into the
            # binary format).
            payload["markdown"] = content
    if block.fields.get("file_url"):
        payload["file_url"] = block.fields["file_url"]
    if block.fields.get("description"):
        payload["description"] = block.fields["description"]
    if block.fields.get("mime_type"):
        payload["mime_type"] = block.fields["mime_type"]

    return {
        "type": ftype,
        "title": payload["title"],
        "payload": payload,
        "skill": "llm_result_block",
    }


async def fulfill_result_blocks(
    assistant_content: str,
    db: "Any",                # sqlalchemy.orm.Session — typed as Any to avoid
    context: dict,            #   a hard import dependency at module load.
) -> tuple[str, list[dict]]:
    """Post-process [[RESULT]] blocks: create real artifacts, rewrite ids.

    Walks the assistant text left-to-right, finds each ``[[RESULT]]``
    block, and (when the block is a renderable file with embedded content)
    forwards it to the artifact tool so the database has a real row keyed
    to the real id.  The block's id is then replaced with the real one in
    the text so the frontend's GET succeeds.  All non-file blocks (agent,
    automation, etc.) are left untouched — those are entity-creation
    flows handled by separate tools and should already have real ids.

    Args:
        assistant_content: The raw assistant text from the LLM.
        db:                Active SQLAlchemy session.
        context:           ``{conversation_id, agent_app_id, ...}``.

    Returns:
        ``(rewritten_content, created_artifacts)`` where
        ``created_artifacts`` is a list of successful ``_create_artifact_tool``
        result dicts, one per fulfilled block.
    """
    if not assistant_content:
        return assistant_content, []

    blocks = find_result_blocks(assistant_content)
    if not blocks:
        return assistant_content, []

    created: list[dict] = []
    # Process blocks right-to-left so char offsets stay valid as we
    # rewrite the text.
    rewritten = assistant_content
    for block in reversed(blocks):
        if not _hallucinated_id_is_suspicious(block):
            continue
        args = _build_create_artifact_args(block)
        if args is None:
            logger.debug(
                "result_block: no renderable payload for type=%s name=%s",
                block.type, block.name,
            )
            continue
        try:
            # Lazy import so the artifact tool (and its heavy exporter
            # stack) is only pulled in when we actually have a block to
            # fulfill.  Keeps the cold-path overhead negligible.
            from app.services.generation_orchestrator import _create_artifact_tool
            result = await _create_artifact_tool(args=args, db=db, context=context)
        except Exception as exc:
            logger.warning(
                "result_block: create_artifact raised for block %s/%s: %s",
                block.type, block.name, exc,
            )
            continue
        if not isinstance(result, dict) or not result.get("success"):
            logger.warning(
                "result_block: create_artifact did not succeed for %s/%s: %s",
                block.type, block.name,
                (result or {}).get("error") if isinstance(result, dict) else result,
            )
            continue

        real_id = str(result.get("artifact_id") or "")
        if not real_id:
            logger.warning("result_block: create_artifact returned no artifact_id")
            continue

        created.append(result)

        # Rewrite the block in the text: replace the hallucinated id
        # (and any preview_url / file_url) with the real ones from the
        # artifact tool.  We do this as a targeted JSON splice so we
        # don't have to re-serialize the entire block — keys we don't
        # touch (name, fields, draft) stay exactly as the LLM wrote
        # them, which is what the frontend expects.
        try:
            raw_body = block.raw
            # Find the JSON body inside the raw block
            inner = raw_body[len("[[RESULT]]"): raw_body.rfind("[[END]]")].strip()
            parsed = json.loads(inner)
            parsed["id"] = real_id
            if result.get("file_url"):
                parsed["file_url"] = result["file_url"]
            if result.get("preview_url"):
                parsed["preview_url"] = result["preview_url"]
            parsed["draft"] = False
            new_block = (
                "[[RESULT]]\n"
                + json.dumps(parsed, ensure_ascii=False)
                + "\n[[END]]"
            )
            rewritten = rewritten[: block.start] + new_block + rewritten[block.end :]
        except Exception as exc:
            logger.warning(
                "result_block: failed to rewrite id in assistant text (non-fatal): %s",
                exc,
            )

    return rewritten, created


__all__ = [
    "ResultBlock",
    "find_result_blocks",
    "strip_result_blocks",
    "fulfill_result_blocks",
]
