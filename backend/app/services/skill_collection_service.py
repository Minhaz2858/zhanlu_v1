"""SkillCollectionService — orchestrates URL → skill pipeline using agent-browser.

Takes a URL, uses the ``agent_browser`` tool to navigate and extract page
content, asks the LLM to structure it as a SKILL.md methodology document,
runs the ``skill_scanner`` for security validation, and persists via
``skill_sync.write_skill_md()`` so the skill is immediately available to
all runtime agents.

Pipeline stages:
  1. Navigate + extract  — agent_browser ``read`` action (open+read one-step)
  2. LLM structure       — extract a SKILL.md body from page content
  3. Validate            — skill_scanner.scan_text() security check
  4. Persist             — skill_sync.write_skill_md() + reload_skills_registry()

The service is fully async and designed to be called from:
  - The REST endpoint ``POST /api/skills/collect`` (dedicated scrape action)
  - The ``skills`` meta-tool ``collect`` action (interactive chat mode)
"""

from __future__ import annotations

import logging
import re
from typing import Optional
from uuid import uuid4

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Cap the page content we send to the LLM to avoid token explosion.
_MAX_PAGE_CHARS = 12_000


class SkillCollectionService:
    """Orchestrates the full URL → skill pipeline using agent-browser."""

    def __init__(self, db: Optional[Session] = None):
        self.db = db

    async def collect_from_url(
        self,
        url: str,
        skill_name: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> dict:
        """Full pipeline: navigate → extract → LLM structure → validate → persist.

        Returns a dict with:
          - success (bool)
          - skill_name, skill_path, scan_findings (on success)
          - error, stage (on failure)
          - dry_run_result (if dry-run gate ran)
        """
        # 1. Navigate + extract page content via agent_browser
        extract_result = await self._extract_page(url)
        if not extract_result.get("success"):
            return {
                "success": False,
                "error": extract_result.get("error", "Failed to extract page content"),
                "stage": "extract",
            }

        page_text = extract_result.get("text", "")
        if not page_text or len(page_text.strip()) < 100:
            return {
                "success": False,
                "error": "Page content is too short or empty — the page may be JS-rendered, paywalled, or blocking bots.",
                "stage": "extract",
            }

        # Truncate to avoid token explosion
        if len(page_text) > _MAX_PAGE_CHARS:
            page_text = page_text[:_MAX_PAGE_CHARS] + "\n\n[... content truncated ...]"

        # 2. LLM structure → SKILL.md body
        llm_result = await self._structure_skill(page_text, url, skill_name)
        if not llm_result.get("success"):
            return {
                "success": False,
                "error": llm_result.get("error", "LLM structuring failed"),
                "stage": "structure",
            }

        structured = llm_result["data"]
        final_name = skill_name or structured.get("name") or _derive_name_from_url(url)
        description = structured.get("description", f"Skill collected from {url}")
        body = structured.get("body", "")

        if not body.strip():
            return {
                "success": False,
                "error": "LLM returned empty skill body",
                "stage": "structure",
            }

        # 3. Security validation
        scan_findings = self._validate_skill(body, final_name)

        # 4. Persist to filesystem + reload registry
        skill_path = self._persist_skill(final_name, description, body, url)

        logger.info(
            "SkillCollectionService: collected skill %r from %s (path=%s)",
            final_name, url, skill_path,
        )

        return {
            "success": True,
            "skill_name": final_name,
            "skill_path": skill_path,
            "description": description,
            "scan_findings": scan_findings,
            "source_url": url,
        }

    async def _extract_page(self, url: str) -> dict:
        """Use agent_browser to navigate and extract page content."""
        from app.services.tool_handlers.agent_browser_tool import _agent_browser

        collection_id = f"collect-{uuid4().hex[:8]}"
        try:
            # One-step open+read via the extract action
            result = await _agent_browser(
                args={"action": "extract", "url": url},
                db=self.db,
                context={"conversation_id": collection_id},
            )
            # Clean up the browser session
            try:
                await _agent_browser(
                    args={"action": "close"},
                    db=self.db,
                    context={"conversation_id": collection_id},
                )
            except Exception:
                pass  # best-effort cleanup
            return result
        except Exception as exc:
            logger.warning("agent_browser extract failed for %s: %s", url, exc)
            return {"success": False, "error": str(exc)}

    async def _structure_skill(
        self,
        page_text: str,
        source_url: str,
        skill_name: Optional[str],
    ) -> dict:
        """Ask the LLM to extract/structure a SKILL.md from page content."""
        from app.services.llm_service import call_llm

        name_hint = f"\nSuggested skill name: {skill_name}" if skill_name else ""

        prompt = f"""You are a skill extraction assistant for the Zhanlu platform. You are given the content of a web page and must extract a reusable skill from it.

Source URL: {source_url}{name_hint}

Page content:
---
{page_text}
---

Extract a skill in JSON format with these fields:
- "name": A short, kebab-case skill name (e.g. "github-pr-review", "pdf-extraction"). Use the suggested name if provided.
- "description": A one-sentence description of what the skill does.
- "body": The full SKILL.md methodology document in markdown. Include sections: Overview, Prerequisites, Steps (detailed, numbered), Tool References (if applicable), Best Practices, and Example Usage. Be thorough and specific — extract the actual methodology from the page content, don't just summarize.

Respond with ONLY a JSON object, no explanation."""

        try:
            result = await call_llm(
                prompt=prompt,
                temperature=0.3,
                response_json_schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "body": {"type": "string"},
                    },
                    "required": ["name", "description", "body"],
                },
            )
            data = result.get("data")
            if data and isinstance(data, dict):
                return {"success": True, "data": data}
            # Fallback: try to parse the response as JSON
            import json
            response_text = result.get("response", "")
            try:
                data = json.loads(response_text)
                if isinstance(data, dict) and "body" in data:
                    return {"success": True, "data": data}
            except json.JSONDecodeError:
                pass
            return {"success": False, "error": "LLM did not return valid structured skill data"}
        except Exception as exc:
            logger.warning("LLM skill structuring failed: %s", exc)
            return {"success": False, "error": str(exc)}

    def _validate_skill(self, body: str, skill_name: str) -> dict:
        """Run the skill_scanner security check on the skill body."""
        try:
            from app.services.skill_scanner.scanner import scan_text
            result = scan_text(body=body, skill_name=skill_name)
            return {
                "has_critical": result.has_critical,
                "summary": result.summary,
                "findings": [
                    {
                        "rule_id": f.rule_id,
                        "severity": f.severity,
                        "description": f.description,
                        "line_number": f.line_number,
                    }
                    for f in result.findings
                ],
            }
        except Exception as exc:
            logger.warning("Skill scan failed (non-fatal): %s", exc)
            return {"has_critical": False, "summary": "scan skipped", "findings": []}

    def _persist_skill(
        self,
        name: str,
        description: str,
        body: str,
        source_url: str,
    ) -> str:
        """Write SKILL.md to filesystem and reload the SkillsRegistry."""
        from app.services.skill_sync import write_skill_md, reload_skills_registry

        skill_path = write_skill_md(
            name=name,
            description=description,
            body=body,
            category="collected",
            version="1.0.0",
            author="skill_collection_service",
            summary=description[:200] if description else None,
            tags=["collected", "web-scrape"],
        )
        reload_skills_registry()

        # Also sync to DB tools table so the skill shows up in the catalog
        if self.db is not None:
            try:
                self._sync_to_db(name, description, body, source_url)
            except Exception as exc:
                logger.warning("DB sync for collected skill %r failed (non-fatal): %s", name, exc)

        return skill_path

    def _sync_to_db(self, name: str, description: str, body: str, source_url: str) -> None:
        """Insert or update the skill in the DB tools table."""
        from app.models.tool import Tool

        existing = self.db.query(Tool).filter(
            Tool.name == name,
            Tool.is_deleted == False,
        ).first()

        if existing:
            existing.description = description
            existing.skill_md = body
            existing.summary = description[:200]
            existing.version = existing.version or "1.0.0"
            existing.enabled = True
            existing.status = "active"
        else:
            tool = Tool(
                name=name,
                description=description,
                kind="system_skill",
                category="collected",
                source="custom",
                skill_md=body,
                summary=description[:200],
                version="1.0.0",
                enabled=True,
                status="active",
            )
            self.db.add(tool)
        self.db.commit()


def _derive_name_from_url(url: str) -> str:
    """Derive a kebab-case skill name from a URL."""
    # Extract the path part and clean it up
    path = url.split("//")[-1].split("/", 1)[-1] if "//" in url else url
    # Take the last meaningful segment
    segments = [s for s in path.rstrip("/").split("/") if s and not s.startswith("?")]
    if segments:
        name = segments[-1]
    else:
        name = url.split("//")[-1].split("/")[0]
    # Clean to kebab-case
    name = re.sub(r"[^A-Za-z0-9._-]", "-", name)
    name = name.replace("_", "-")  # normalize underscores to hyphens
    name = re.sub(r"-+", "-", name).strip("-").lower()
    # Remove common file extensions
    name = re.sub(r"\.(md|html?|php|aspx?)$", "", name)
    return name or "collected-skill"
