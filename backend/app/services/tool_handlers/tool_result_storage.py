"""3-layer tool result persistence: inline preview + disk spill + turn budget.

When a tool returns more than ``resolve_threshold(tool_name)`` characters, we
persist the full result to disk (or to the artifact store), keep a short
preview inline in the conversation, and replace the rest with a reference
the LLM can fetch on demand.

This module is the persistence layer; budget_config.py holds the constants
and registry hooks. The artifact store is the existing
``app/services/artifacts/`` layer (best-effort) — we fall back to /tmp when
the artifact store rejects a write (e.g. when the artifact row has no
sandbox_id / org_id in the current call context).
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from app.services.tool_handlers.budget_config import (
    DEFAULT_PREVIEW_SIZE_CHARS,
    DEFAULT_BUDGET_CONFIG,
    BudgetConfig,
)

logger = logging.getLogger(__name__)


# Per-process spill directory. Created lazily on first spill.
_SPILL_DIR: Optional[Path] = None


def _get_spill_dir() -> Path:
    global _SPILL_DIR
    if _SPILL_DIR is None:
        spill_root = os.environ.get("ZHANLU_TOOL_SPILL_DIR", "/tmp/zhanlu_tool_spill")
        _SPILL_DIR = Path(spill_root)
        _SPILL_DIR.mkdir(parents=True, exist_ok=True)
    return _SPILL_DIR


def _make_truncation_notice(
    tool_name: str,
    total_chars: int,
    threshold: int | float,
    artifact_ref: Optional[str],
) -> str:
    """Build the inline notice that replaces the truncated tail.

    The LLM reads this and knows the rest is available via the artifact
    reference (or a follow-up tool call if the artifact store is unavailable).
    """
    threshold_str = "inf" if threshold == float("inf") else f"{int(threshold):,}"
    parts = [
        f"\n\n[Result truncated: {total_chars:,} chars > {threshold_str} threshold]",
    ]
    if artifact_ref:
        parts.append(
            f"Full result persisted as artifact `{artifact_ref}`. "
            f"Use the artifact read tool to fetch sections by offset/limit."
        )
    else:
        parts.append(
            "Full result was too large to inline. "
            "Consider reading sections (offset/limit) or storing the data in a file."
        )
    return "".join(parts)


def _truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars]


def _truncate_dict(data: dict, max_chars: int) -> tuple[dict, int]:
    """Truncate a dict's serialized form to max_chars. Returns (truncated, total)."""
    full = json.dumps(data, ensure_ascii=False, default=str)
    total = len(full)
    if total <= max_chars:
        return data, total
    # Greedy: keep top-level keys until budget exhausted.
    out: dict = {}
    running = 2  # for the opening/closing braces
    for k, v in data.items():
        kv = json.dumps({k: v}, ensure_ascii=False, default=str)
        if running + len(kv) + 2 > max_chars:
            out["_truncated_keys"] = (
                f"<{len(data) - len(out)} more keys omitted to fit budget>"
            )
            return out, total
        out[k] = v
        running += len(kv) + 2
    return out, total


def _try_artifact_store(
    tool_name: str,
    conversation_id: Optional[str],
    payload: Any,
) -> Optional[str]:
    """Best-effort write to the artifact store. Returns ref_id or None.

    Tries the existing ``app/services/artifacts/`` layer. On any failure
    (no sandbox context, missing model, import error) returns None so the
    caller can fall back to local-disk spill.
    """
    try:
        from app.services.artifacts.artifact_service import ArtifactService
    except Exception:
        return None
    try:
        svc = ArtifactService(None)  # best-effort; ignore DB session
        ref = svc.create_artifact(
            name=f"{tool_name}_result_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}",
            kind="tool_result",
            content_type="application/json",
            content=json.dumps(payload, ensure_ascii=False, default=str),
            sandbox_id=None,
            conversation_id=conversation_id,
        )
        if ref and getattr(ref, "id", None):
            return ref.id
    except Exception as exc:
        logger.debug("Artifact store write failed (falling back to disk): %s", exc)
    return None


def _try_disk_spill(tool_name: str, payload: Any) -> Optional[str]:
    """Spill a large result to /tmp/zhanlu_tool_spill and return a handle.

    The handle is the filename. The full file path is intentionally
    internal — agents shouldn't construct paths themselves.
    """
    try:
        spill_dir = _get_spill_dir()
        ref = f"{tool_name}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}.json"
        path = spill_dir / ref
        path.write_text(
            json.dumps(payload, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        return ref
    except Exception as exc:
        logger.warning("Disk spill failed: %s", exc)
        return None


def maybe_persist_tool_result(
    tool_name: str,
    result: Any,
    *,
    conversation_id: Optional[str] = None,
    config: Optional[BudgetConfig] = None,
) -> Any:
    """Return a result that fits the per-tool budget; spill the rest.

    Behavior:
      - If ``result`` is smaller than ``resolve_threshold(tool_name)`` chars,
        return it unchanged.
      - Otherwise, attempt to persist to the artifact store; on success
        return a truncated preview + the artifact ref.
      - On artifact-store failure, fall back to a /tmp spill and return a
        truncated preview + the disk handle.
      - If both persistence backends fail, return a hard-truncated string
        with a notice (last-resort, no full content recoverable).
    """
    cfg = config or DEFAULT_BUDGET_CONFIG
    threshold = cfg.resolve_threshold(tool_name)
    preview_size = cfg.preview_size

    # 1. Stringify for size measurement
    if isinstance(result, str):
        total = len(result)
    elif isinstance(result, dict):
        total = len(json.dumps(result, ensure_ascii=False, default=str))
    else:
        try:
            total = len(json.dumps(result, ensure_ascii=False, default=str))
        except Exception:
            total = len(str(result))

    if total <= threshold:
        return result

    # 2. Persist full payload
    artifact_ref = _try_artifact_store(tool_name, conversation_id, result)
    if artifact_ref is None:
        artifact_ref = _try_disk_spill(tool_name, result)

    # 3. Build truncated preview
    notice = _make_truncation_notice(tool_name, total, threshold, artifact_ref)
    if isinstance(result, str):
        return _truncate_text(result, preview_size) + notice
    if isinstance(result, dict):
        truncated, _ = _truncate_dict(result, preview_size - len(notice))
        truncated["_result_truncated"] = True
        truncated["_original_chars"] = total
        if artifact_ref:
            truncated["_artifact_ref"] = artifact_ref
        return truncated
    return {
        "success": True,
        "result_preview": _truncate_text(str(result), preview_size - 200),
        "_result_truncated": True,
        "_original_chars": total,
        "_artifact_ref": artifact_ref,
    }
