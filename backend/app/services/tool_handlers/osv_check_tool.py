"""osv_check tool — look up CVEs in the OSV (Open Source Vulnerabilities) database.

Queries https://api.osv.dev/v1/query for known vulnerabilities affecting
the given package + version. Returns a list of {id, summary, severity, references}.

No API key required. Network-bound.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from sqlalchemy.orm import Session

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)

OSV_URL = "https://api.osv.dev/v1/query"


async def _osv_check(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    package = (args.get("package") or "").strip()
    version = (args.get("version") or "").strip()
    ecosystem = (args.get("ecosystem") or "PyPI").strip()
    if not package:
        return {"success": False, "error": "package is required"}
    payload: dict = {"package": {"name": package, "ecosystem": ecosystem}}
    if version:
        payload["version"] = version
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(OSV_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        return {"success": False, "error": f"OSV query failed: {exc}"}

    vulns = data.get("vulns") or []
    out = []
    for v in vulns:
        out.append({
            "id": v.get("id"),
            "summary": v.get("summary") or v.get("details", "")[:200],
            "aliases": v.get("aliases", []),
            "severity": [
                s.get("score") for s in v.get("severity", []) if s.get("score")
            ],
            "references": [r.get("url") for r in v.get("references", []) if r.get("url")][:3],
        })
    return {
        "success": True,
        "package": package,
        "version": version,
        "ecosystem": ecosystem,
        "vulnerability_count": len(out),
        "vulnerabilities": out,
    }


OSV_CHECK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "osv_check",
        "description": (
            "Look up known CVEs in the OSV database for a package + "
            "version. Returns a list of {id, summary, severity, "
            "references}. Use before adding a dependency to a project."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "package": {"type": "string", "description": "Package name (e.g. 'requests')."},
                "version": {"type": "string", "description": "Version string (e.g. '2.31.0')."},
                "ecosystem": {"type": "string", "description": "Package ecosystem (default 'PyPI').", "default": "PyPI"},
            },
            "required": ["package"],
        },
    },
}

registry.register(
    name="osv_check",
    schema=OSV_CHECK_SCHEMA,
    handler=_osv_check,
    category="security",
    toolset="security",
    description="OSV.dev CVE lookup for a package + version.",
    emoji="🛡️",
    max_result_size_chars=20_000,
)
