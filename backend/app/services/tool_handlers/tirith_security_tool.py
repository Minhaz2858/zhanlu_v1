"""tirith_security tool — pre-flight safety check for shell commands.

Scans a command string for dangerous patterns (rm -rf /, secret reads,
sudoers mods, ssh backdoors, exfiltration) and returns a verdict.

Adapted from hermes' tirith. Lightweight: pure regex, no AST parsing.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from app.services.tool_registry import registry


# Each pattern: (regex, severity, label)
_RULES: List[Tuple[str, str, str]] = [
    (r"\brm\s+-\w*r\w*f", "critical", "recursive_delete"),
    (r"\brm\s+-\w*r\w*\s+/(?:\s|$|;|\|)", "critical", "recursive_delete_root"),
    (r"\bcurl\b[^\n]*\|\s*(sh|bash|zsh)", "critical", "curl_pipe_shell"),
    (r"\bwget\b[^\n]*\|\s*(sh|bash|zsh)", "critical", "wget_pipe_shell"),
    (r"cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass|id_rsa|/etc/shadow|/etc/passwd)", "high", "read_secrets"),
    (r"echo\s+[^\n]*(\.env|credentials|api[_-]?key|token|secret|password)", "high", "echo_secrets"),
    (r"chmod\s+777", "high", "world_writable"),
    (r"chmod\s+\+s", "critical", "setuid"),
    (r"\bsudo\b[^\n]*(visudo|/etc/sudoers|/etc/passwd|/etc/shadow)", "critical", "sudoers_mod"),
    (r">\s*/etc/(passwd|shadow|sudoers|hosts)", "critical", "etc_write"),
    (r"authorized_keys", "high", "ssh_backdoor"),
    (r"ssh\s+-o\s+StrictHostKeyChecking=no", "medium", "ssh_no_strict_host_key"),
    (r"\bdd\s+if=", "critical", "disk_overwrite"),
    (r":()\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "critical", "fork_bomb"),
    (r"base64\s+(-d|--decode)[^\n]*\|\s*(sh|bash)", "high", "base64_decode_pipe"),
    (r"\bnc\s+-l[^\n]*\d{2,5}", "medium", "netcat_listener"),
]


def _scan(command: str) -> List[dict]:
    hits: List[dict] = []
    for pattern, severity, label in _RULES:
        m = re.search(pattern, command, re.IGNORECASE)
        if m:
            hits.append({
                "rule": label,
                "severity": severity,
                "matched": m.group(0),
            })
    return hits


async def _tirith_security(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    command = (args.get("command") or "").strip()
    if not command:
        return {"success": False, "error": "command is required"}
    hits = _scan(command)
    severity_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    max_sev = max((severity_rank[h["severity"]] for h in hits), default=0)
    verdict = (
        "block" if max_sev >= 4 else
        "review" if max_sev >= 3 else
        "caution" if max_sev >= 2 else
        "safe"
    )
    return {
        "success": True,
        "command": command,
        "verdict": verdict,
        "max_severity": max_sev,
        "hits": hits,
        "hit_count": len(hits),
    }


TIRITH_SECURITY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "tirith_security",
        "description": (
            "Pre-flight safety scan for a shell command. Detects "
            "destructive operations (rm -rf, dd), secret reads, "
            "sudoers modifications, ssh backdoors, fork bombs, and "
            "common exfiltration patterns. Returns a verdict "
            "(safe | caution | review | block) and the matched rules."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to scan."},
            },
            "required": ["command"],
        },
    },
}

registry.register(
    name="tirith_security",
    schema=TIRITH_SECURITY_SCHEMA,
    handler=_tirith_security,
    category="security",
    toolset="security",
    description="Pre-flight safety scan for shell commands.",
    emoji="🔍",
)
