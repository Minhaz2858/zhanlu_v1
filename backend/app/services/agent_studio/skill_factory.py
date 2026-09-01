"""Skill factory — generates skill packages from description/template/trace/code.

The factory takes a natural language description or template and produces
a complete skill package:
- SKILL.md (methodology documentation with frontmatter)
- manifest.yaml (inputs, outputs, version)

Generated skills are persisted to ``~/.zhanlu/skills/`` via
``skill_sync.write_skill_md()`` and the SkillsRegistry is reloaded so the
skill is immediately available to all runtime agents (progressive
disclosure + load_skill_body).

Generated skills also enter the governance pipeline as SkillCandidates
for review tracking.
"""

import logging
from typing import Optional
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.skill_candidate import SkillCandidate

logger = logging.getLogger(__name__)


class SkillFactory:
    """Factory for generating skill packages from various sources."""

    def __init__(self, db: Session):
        self.db = db

    async def create_from_description(
        self,
        name: str,
        description: str,
        artifact_type: Optional[str] = None,
        org_id: str = "default-org",
        app_id: str = "default-app",
    ) -> SkillCandidate:
        """Create a skill candidate from a natural language description.

        The LLM generates a complete SKILL.md methodology document —
        step-by-step instructions, tool references, and best practices.
        The generated skill is immediately persisted to the filesystem
        and the SkillsRegistry is reloaded so runtime agents can discover
        and invoke it via ``load_skill_body``.
        """
        from app.services.llm_service import call_llm

        # Generate SKILL.md body via LLM
        extraction_prompt = _build_skill_generation_prompt(name, description, artifact_type)

        generated_body = ""
        llm_used = False
        try:
            result = await call_llm(prompt=extraction_prompt, temperature=0.7)
            generated_body = (result.get("response") or "").strip()
            if generated_body:
                llm_used = True
        except Exception as exc:
            logger.warning("LLM generation failed for skill %r, using fallback: %s", name, exc)

        if not llm_used or not generated_body:
            generated_body = _generate_fallback_body(name, description, artifact_type)

        # Persist to filesystem so the skill is immediately available to agents
        skill_path = _persist_skill(
            name=name,
            description=description,
            body=generated_body,
            artifact_type=artifact_type,
        )

        # Generate manifest
        manifest = {
            "name": name,
            "version": "1.0.0",
            "description": description,
            "artifact_type": artifact_type,
            "source": "llm" if llm_used else "fallback",
        }

        # Create candidate for governance/review pipeline
        candidate = SkillCandidate(
            id=str(uuid4()),
            name=name,
            description=description,
            source_type="description",
            source_data={
                "description": description,
                "artifact_type": artifact_type,
                "llm_used": llm_used,
                "skill_path": skill_path,
            },
            generated_code=None,
            generated_manifest=manifest,
            generated_skill_md=generated_body,
            status="quarantined",
            org_id=org_id,
            app_id=app_id,
        )
        self.db.add(candidate)
        self.db.commit()
        self.db.refresh(candidate)

        logger.info(
            "Created skill candidate %s from description (name=%s, llm=%s, path=%s)",
            candidate.id, name, llm_used, skill_path,
        )
        return candidate

    async def create_from_template(
        self,
        name: str,
        template_code: str,
        description: Optional[str] = None,
        org_id: str = "default-org",
        app_id: str = "default-app",
    ) -> SkillCandidate:
        """Create a skill candidate from a code template.

        Generates a SKILL.md body describing how to use the provided code,
        then persists both to the filesystem.
        """
        desc = description or f"Skill from template: {name}"

        # Generate a SKILL.md body that wraps the provided code
        body = _generate_code_skill_body(name, desc, template_code)

        skill_path = _persist_skill(
            name=name,
            description=desc,
            body=body,
        )

        candidate = SkillCandidate(
            id=str(uuid4()),
            name=name,
            description=desc,
            source_type="template",
            source_data={"template_code": template_code, "skill_path": skill_path},
            generated_code=template_code,
            generated_manifest={
                "name": name,
                "version": "1.0.0",
                "description": desc,
            },
            generated_skill_md=body,
            status="quarantined",
            org_id=org_id,
            app_id=app_id,
        )
        self.db.add(candidate)
        self.db.commit()
        self.db.refresh(candidate)
        return candidate

    async def create_from_code(
        self,
        name: str,
        code: str,
        description: Optional[str] = None,
        org_id: str = "default-org",
        app_id: str = "default-app",
    ) -> SkillCandidate:
        """Create a skill candidate from raw code."""
        return await self.create_from_template(name, code, description, org_id, app_id)


# ── Helpers ─────────────────────────────────────────────────────────────


def _persist_skill(
    name: str,
    description: str,
    body: str,
    artifact_type: Optional[str] = None,
) -> str:
    """Write SKILL.md to filesystem and reload the SkillsRegistry.

    Returns the absolute path of the written file.
    """
    from app.services.skill_sync import write_skill_md, reload_skills_registry

    skill_path = write_skill_md(
        name=name,
        description=description,
        body=body,
        category="custom",
        version="1.0.0",
        author="skill_agent",
        summary=description[:200] if description else None,
    )
    reload_skills_registry()
    return skill_path


def _build_skill_generation_prompt(
    name: str,
    description: str,
    artifact_type: Optional[str],
) -> str:
    """Build the LLM prompt for generating a SKILL.md methodology document."""
    artifact_line = f"\nOutput artifact type: {artifact_type}\n" if artifact_type else ""
    return f"""You are a skill authoring assistant for the Zhanlu platform. Generate a complete SKILL.md methodology document for the following skill:

Name: {name}
Description: {description}{artifact_line}

The SKILL.md should include these sections (use ## headings):

1. **Overview** — A brief description of what the skill does and when to use it.
2. **Prerequisites** — Any setup, tools, or permissions needed.
3. **Steps** — Detailed step-by-step instructions (use numbered lists). Be specific and actionable.
4. **Tool References** — Which tools/APIs to call and how (if applicable).
5. **Best Practices** — Tips for quality output and common pitfalls to avoid.
6. **Example Usage** — A concrete example scenario showing the skill in action.

Write in clear, professional markdown. Be thorough but concise.
Do NOT include YAML frontmatter (it will be added automatically).
Do NOT include any code fences around the entire document.
Respond with ONLY the markdown body."""


def _generate_fallback_body(
    name: str,
    description: str,
    artifact_type: Optional[str],
) -> str:
    """Generate fallback SKILL.md body when LLM is unavailable."""
    artifact_line = f"\n\n**Output artifact:** {artifact_type}" if artifact_type else ""
    return f"""## Overview

{description}{artifact_line}

## Prerequisites

- Access to the Zhanlu platform
- Appropriate permissions for the target resources

## Steps

1. **Understand the requirement** — Review the task description and identify the key objectives.
2. **Gather inputs** — Collect all necessary data, context, and configuration.
3. **Execute the workflow** — Follow the methodology to produce the desired output.
4. **Validate results** — Check the output against the expected criteria.
5. **Deliver** — Present the final result in the appropriate format.

## Best Practices

- Break complex tasks into smaller, manageable steps.
- Validate intermediate results before proceeding.
- Document any deviations from the standard workflow.

## Example Usage

This skill is activated when the agent detects a task matching its description. Follow the steps above to complete the task efficiently.
"""


def _generate_code_skill_body(
    name: str,
    description: str,
    code: str,
) -> str:
    """Generate a SKILL.md body for a code-based skill."""
    # Truncate code if very long to keep the SKILL.md manageable
    display_code = code if len(code) <= 4000 else code[:4000] + "\n# ... (truncated)"
    return f"""## Overview

{description}

## Prerequisites

- Python 3.12+ runtime environment
- Required dependencies as specified in the code

## Steps

1. **Review the code** — Understand the script's entry point and expected inputs.
2. **Prepare inputs** — Create the necessary input files or configuration.
3. **Execute** — Run the script with the appropriate arguments.
4. **Check output** — Validate the generated output files.

## Code

```python
{display_code}
```

## Best Practices

- Test with sample inputs before running on production data.
- Review the code for security concerns before execution.
- Keep a backup of inputs before running destructive operations.

## Example Usage

Invoke this skill's bundled script via the `skills` tool with action="run".
"""
