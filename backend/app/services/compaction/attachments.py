"""Compact attachments — metadata preserved across compaction boundaries.

When the conversation is compacted, key metadata (task focus, recently read
files, invoked skills, verified work, etc.) is extracted and preserved as
attachment messages so the agent doesn't lose critical context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class CompactAttachment:
    """Structured compact asset carried across a compaction boundary."""

    kind: str
    title: str
    body: str
    metadata: dict[str, Any] = field(default_factory=dict)


MAX_COMPACT_ATTACHMENTS = 6


def _sanitize_metadata(value: Any) -> Any:
    """Recursively sanitize metadata for JSON serialization."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _sanitize_metadata(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_metadata(item) for item in value]
    return str(value)


def _create_attachment(
    kind: str,
    title: str,
    lines: list[str],
    *,
    metadata: dict[str, Any] | None = None,
) -> CompactAttachment | None:
    """Create an attachment from lines, filtering empty ones."""
    filtered = [line.rstrip() for line in lines if line and line.strip()]
    if not filtered:
        return None
    return CompactAttachment(
        kind=kind,
        title=title,
        body="\n".join(filtered),
        metadata=_sanitize_metadata(metadata or {}),
    )


def render_compact_attachment(attachment: CompactAttachment) -> dict[str, Any]:
    """Serialize a compact attachment into a conversation message dict."""
    header = f"[Compact attachment: {attachment.kind}] {attachment.title}".strip()
    text = f"{header}\n{attachment.body}".strip()
    return {"role": "user", "content": text}


def _extract_attachment_paths(messages: list[dict[str, Any]]) -> list[str]:
    """Extract file paths mentioned in messages for attachment preservation."""
    found: list[str] = []
    seen: set[str] = set()
    path_pattern = re.compile(r"path:\s*([^)\n]+)")
    attachment_pattern = re.compile(r"\[attachment:\s*([^\]]+)\]")

    for message in messages:
        content = message.get("content", "")
        if not isinstance(content, str):
            continue
        for match in path_pattern.findall(content):
            path = match.strip()
            if path and path not in seen:
                seen.add(path)
                found.append(path)
        for match in attachment_pattern.findall(content):
            path = match.strip()
            if path and "download failed" not in path and path not in seen:
                seen.add(path)
                found.append(path)
        if len(found) >= MAX_COMPACT_ATTACHMENTS:
            return found
    return found


def create_task_focus_attachment_if_needed(
    metadata: dict[str, Any],
) -> CompactAttachment | None:
    """Preserve the current task focus across compaction."""
    state = metadata.get("task_focus_state")
    if not isinstance(state, dict):
        return None
    goal = str(state.get("goal") or "").strip()
    recent_goals = [str(g).strip() for g in state.get("recent_goals", []) if str(g).strip()]
    active_artifacts = [str(a).strip() for a in state.get("active_artifacts", []) if str(a).strip()]
    verified_state = [str(v).strip() for v in state.get("verified_state", []) if str(v).strip()]
    next_step = str(state.get("next_step") or "").strip()

    if not any((goal, recent_goals, active_artifacts, verified_state, next_step)):
        return None

    lines = ["Current working focus to preserve across compaction:"]
    if goal:
        lines.append(f"- Goal: {goal}")
    if recent_goals:
        lines.append("- Recent user goals that still matter:")
        lines.extend(f"  - {item}" for item in recent_goals[-3:])
    if active_artifacts:
        lines.append("- Active artifacts in play:")
        lines.extend(f"  - {item}" for item in active_artifacts[-5:])
    if verified_state:
        lines.append("- Verified state already established:")
        lines.extend(f"  - {item}" for item in verified_state[-4:])
    if next_step:
        lines.append(f"- Suggested next step: {next_step}")

    return _create_attachment("task_focus", "Current working focus", lines, metadata={
        "goal": goal,
        "recent_goals": recent_goals[-3:],
        "active_artifacts": active_artifacts[-5:],
        "verified_state": verified_state[-4:],
        "next_step": next_step,
    })


def create_recent_files_attachment_if_needed(
    read_file_state: Any,
) -> CompactAttachment | None:
    """Preserve recently read file paths across compaction."""
    if not isinstance(read_file_state, list) or not read_file_state:
        return None
    lines = ["Recently read files that may still matter:"]
    entries: list[dict[str, Any]] = []
    normalized = [
        e for e in read_file_state
        if isinstance(e, dict) and str(e.get("path") or "").strip()
    ]
    normalized.sort(key=lambda e: float(e.get("timestamp") or 0.0), reverse=True)
    for entry in normalized[:4]:
        path = str(entry.get("path") or "").strip()
        span = str(entry.get("span") or "").strip()
        preview = str(entry.get("preview") or "").strip()
        if not path:
            continue
        bullet = f"- {path}"
        if span:
            bullet += f" ({span})"
        lines.append(bullet)
        if preview:
            lines.append(f"  Preview: {preview}")
        entries.append({"path": path, "span": span, "preview": preview})
    return _create_attachment("recent_files", "Recently read files", lines, metadata={"entries": entries})


def create_recent_verified_work_attachment_if_needed(
    verified_work: Any,
) -> CompactAttachment | None:
    """Preserve recently verified work items across compaction."""
    if not isinstance(verified_work, list) or not verified_work:
        return None
    entries = [str(e).strip() for e in verified_work[-8:] if str(e).strip()]
    if not entries:
        return None
    return _create_attachment(
        "recent_verified_work",
        "Recently verified work",
        ["These steps or conclusions were explicitly verified before compaction:"] + [f"- {e}" for e in entries],
        metadata={"entries": entries},
    )


def create_invoked_skills_attachment_if_needed(
    invoked_skills: Any,
) -> CompactAttachment | None:
    """Preserve invoked skills list across compaction."""
    if not isinstance(invoked_skills, list) or not invoked_skills:
        return None
    normalized = [str(s).strip() for s in invoked_skills[-8:] if str(s).strip()]
    if not normalized:
        return None
    return _create_attachment(
        "invoked_skills",
        "Skills used earlier in the session",
        ["The following skills were invoked and may still shape the next step:", "- " + ", ".join(normalized)],
        metadata={"skills": normalized},
    )


def create_async_agent_attachment_if_needed(
    async_agent_state: Any,
) -> CompactAttachment | None:
    """Preserve async agent / background task state across compaction."""
    if not isinstance(async_agent_state, list) or not async_agent_state:
        return None
    entries = [str(e).strip() for e in async_agent_state[-6:] if str(e).strip()]
    if not entries:
        return None
    return _create_attachment(
        "async_agents",
        "Async agent and background task state",
        ["Recent async-agent/background-task activity:"] + [f"- {e}" for e in entries],
        metadata={"entries": entries},
    )


def create_work_log_attachment_if_needed(
    recent_work_log: Any,
) -> CompactAttachment | None:
    """Preserve recent work log across compaction."""
    if not isinstance(recent_work_log, list) or not recent_work_log:
        return None
    entries = [str(e).strip() for e in recent_work_log[-8:] if str(e).strip()]
    if not entries:
        return None
    return _create_attachment(
        "recent_work_log",
        "Recent execution checkpoints",
        ["Recent work and verification steps taken in this session:"] + [f"- {e}" for e in entries],
        metadata={"entries": entries},
    )


def _create_recent_attachments_attachment_if_needed(
    attachment_paths: list[str],
) -> CompactAttachment | None:
    """Preserve local attachment paths across compaction."""
    if not attachment_paths:
        return None
    return _create_attachment(
        "recent_attachments",
        "Recent local attachments",
        ["Keep these local attachment paths in working memory:"] + [f"- {path}" for path in attachment_paths],
        metadata={"paths": attachment_paths},
    )


def build_compact_attachments(
    messages: list[dict[str, Any]],
    *,
    metadata: dict[str, Any] | None,
) -> list[CompactAttachment]:
    """Build all applicable compact attachments from messages and metadata."""
    metadata = metadata or {}
    attachment_paths = _extract_attachment_paths(messages)
    builders = [
        create_task_focus_attachment_if_needed(metadata),
        create_recent_verified_work_attachment_if_needed(metadata.get("recent_verified_work")),
        _create_recent_attachments_attachment_if_needed(attachment_paths),
        create_recent_files_attachment_if_needed(metadata.get("read_file_state")),
        create_invoked_skills_attachment_if_needed(metadata.get("invoked_skills")),
        create_async_agent_attachment_if_needed(metadata.get("async_agent_state")),
        create_work_log_attachment_if_needed(metadata.get("recent_work_log")),
    ]
    return [att for att in builders if att is not None]
