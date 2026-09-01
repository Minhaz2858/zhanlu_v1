"""skills_guard tool — pre-load safety check for a skill.

Scans a skill's SKILL.md content for prompt-injection patterns, dangerous
shell commands, and policy violations. Returns a verdict (safe |
caution | review | block) and the matched patterns.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional

from sqlalchemy.orm import Session

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)

_THREAT_PATTERNS: List[tuple] = [
    (r"ignore\s+(?:all\s+)?previous\s+instructions", "high", "prompt_injection"),
    (r"forget\s+(?:everything|all|previous)", "high", "memory_wipe"),
    (r"disregard\s+(?:your|all|any)\s+(?:instructions|rules|guidelines)", "high", "rule_bypass"),
    (r"you\s+are\s+now\s+(?:a|an)\s+(?:different|new|unrestricted)", "high", "identity_override"),
    (r"reveal\s+(?:the\s+)?system\s+prompt", "high", "system_prompt_leak"),
    (r"<\s*\|im_start\|\s*>", "high", "framing_token"),
    (r"<\s*\|im_end\|\s*>", "high", "framing_token"),
    (r"\[INST\]", "high", "framing_token"),
    (r"\[/INST\]", "high", "framing_token"),
    (r"cat\s+[^\n]*(\.env|id_rsa|\.netrc|/etc/shadow|credentials)", "medium", "secret_access"),
    (r"rm\s+-\w*r\w*f", "medium", "destructive_command"),
    (r"curl\s+[^\n]*\|\s*(?:sh|bash)", "medium", "remote_exec"),
    (r"authorized_keys", "medium", "ssh_backdoor"),
    (r"sudoers", "medium", "sudoers_mod"),
    (r"\$\{?(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)[A-Z_]*\}?", "low", "secret_var"),
]


def _scan(content: str) -> List[dict]:
    hits = []
    for pat, sev, label in _THREAT_PATTERNS:
        m = re.search(pat, content, re.IGNORECASE)
        if m:
            hits.append({"rule": label, "severity": sev, "matched": m.group(0)[:100]})
    return hits


async def _skills_guard(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    name = (args.get("name") or "").strip()
    content = args.get("content", "")
    if not name and not content:
        return {"success": False, "error": "name or content is required"}
    if not content and name:
        try:
            from app.services.skills_loader import load_skill
            content = load_skill(name, db=db) or ""
        except Exception as exc:
            return {"success": False, "error": f"Failed to load skill: {exc}"}
    if not content:
        return {"success": False, "error": f"Skill not found: {name}"}
    hits = _scan(content)
    sev_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    max_sev = max((sev_rank[h["severity"]] for h in hits), default=0)
    verdict = (
        "block" if max_sev >= 4 else
        "review" if max_sev >= 3 else
        "caution" if max_sev >= 2 else
        "safe"
    )
    return {
        "success": True,
        "name": name,
        "verdict": verdict,
        "max_severity": max_sev,
        "hits": hits,
        "hit_count": len(hits),
    }


SKILLS_GUARD_SCHEMA = {
    "type": "function",
    "function": {
        "name": "skills_guard",
        "description": (
            "Pre-load safety check for a skill's SKILL.md content. "
            "Detects prompt injection, framing tokens, secret access, "
            "destructive commands, and SSH/sudoers tampering. Returns "
            "a verdict and matched patterns."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name to check (loaded from registry)."},
                "content": {"type": "string", "description": "Or provide the SKILL.md content directly."},
            },
        },
    },
}

registry.register(
    name="skills_guard",
    schema=SKILLS_GUARD_SCHEMA,
    handler=_skills_guard,
    category="security",
    toolset="security",
    description="Pre-load safety check for a skill's SKILL.md.",
    emoji="🛡️",
)
