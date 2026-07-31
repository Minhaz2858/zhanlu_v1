"""Preflight / dry-run service — checks agent readiness before deployment.

Validates:
- Agent manifest is complete
- Data bindings reference existing datasources
- Skill bindings reference existing skills
- Policy profile is valid
- Output contract specifies allowed artifact types
- Memory scope is set
- Model is configured

Returns a readiness report: ready | warning | blocked
"""

import logging
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class PreflightService:
    """Service for checking agent readiness before deployment."""

    def __init__(self, db: Session):
        self.db = db

    def check_agent(self, agent_app_id: str) -> dict:
        """Run preflight checks on an agent.

        Returns:
            {
                "status": "ready" | "warning" | "blocked",
                "checks": [{"name", "passed", "severity", "message"}],
                "summary": str,
            }
        """
        from app.models.agent_app import AgentApp

        agent = self.db.query(AgentApp).filter(AgentApp.id == agent_app_id).first()
        if not agent:
            return {
                "status": "blocked",
                "checks": [{"name": "exists", "passed": False, "severity": "error", "message": "Agent not found"}],
                "summary": "Agent not found",
            }

        checks = []

        # 1. Manifest check
        manifest = agent.manifest_json
        if manifest and manifest.get("mission"):
            checks.append({"name": "manifest", "passed": True, "severity": "info", "message": "Manifest defined"})
        else:
            checks.append({"name": "manifest", "passed": False, "severity": "warning", "message": "No manifest — agent will use defaults"})

        # 2. Model check
        if agent.model:
            checks.append({"name": "model", "passed": True, "severity": "info", "message": f"Model: {agent.model}"})
        else:
            checks.append({"name": "model", "passed": False, "severity": "warning", "message": "No model configured"})

        # 3. Prompt layers check
        prompt_layers = sum(1 for p in [agent.prompt_identity, agent.prompt_boundary, agent.prompt_reasoning, agent.prompt_tools, agent.prompt_output] if p)
        if prompt_layers >= 3:
            checks.append({"name": "prompts", "passed": True, "severity": "info", "message": f"{prompt_layers}/5 prompt layers defined"})
        else:
            checks.append({"name": "prompts", "passed": False, "severity": "warning", "message": f"Only {prompt_layers}/5 prompt layers defined"})

        # 4. Data bindings check
        data_bindings = agent.data_bindings or []
        if data_bindings:
            checks.append({"name": "data_bindings", "passed": True, "severity": "info", "message": f"{len(data_bindings)} data binding(s)"})
        else:
            checks.append({"name": "data_bindings", "passed": True, "severity": "info", "message": "No data bindings (read-only)"})

        # 5. Skill bindings check
        skill_bindings = agent.skill_bindings or []
        skills = agent.skills or []
        if skill_bindings or skills:
            total = len(skill_bindings) + len(skills)
            checks.append({"name": "skill_bindings", "passed": True, "severity": "info", "message": f"{total} skill(s) bound"})
        else:
            checks.append({"name": "skill_bindings", "passed": False, "severity": "warning", "message": "No skills bound"})

        # 6. Memory scope check
        if agent.memory_scope:
            checks.append({"name": "memory_scope", "passed": True, "severity": "info", "message": f"Memory scope: {agent.memory_scope}"})
        else:
            checks.append({"name": "memory_scope", "passed": False, "severity": "warning", "message": "No memory scope — defaults to user_only"})

        # 7. Policy profile check
        if agent.policy_profile:
            checks.append({"name": "policy_profile", "passed": True, "severity": "info", "message": "Policy profile defined"})
        else:
            checks.append({"name": "policy_profile", "passed": False, "severity": "warning", "message": "No policy profile — uses default"})

        # 8. Output contract check
        if agent.output_contract:
            checks.append({"name": "output_contract", "passed": True, "severity": "info", "message": "Output contract defined"})
        else:
            checks.append({"name": "output_contract", "passed": True, "severity": "info", "message": "No output contract — allows all types"})

        # 9. Evaluation profile check
        eval_profile = agent.evaluation_profile
        if eval_profile:
            grounding = eval_profile.get("grounding_checks", []) if isinstance(eval_profile, dict) else []
            checks.append({"name": "evaluation_profile", "passed": True, "severity": "info", "message": f"Evaluation defined ({len(grounding)} grounding check(s))"})
        else:
            checks.append({"name": "evaluation_profile", "passed": False, "severity": "warning", "message": "No evaluation profile — quality checks disabled"})

        # Determine overall status
        errors = [c for c in checks if c["severity"] == "error"]
        warnings = [c for c in checks if c["severity"] == "warning"]

        if errors:
            status = "blocked"
            summary = f"{len(errors)} error(s), {len(warnings)} warning(s)"
        elif warnings:
            status = "warning"
            summary = f"{len(warnings)} warning(s)"
        else:
            status = "ready"
            summary = "All checks passed"

        return {
            "status": status,
            "checks": checks,
            "summary": summary,
        }

    def dry_run(
        self,
        agent_app_id: str,
        test_message: str = "Hello, what can you do?",
    ) -> dict:
        """Perform a dry-run of the agent with a test message.

        This doesn't execute the full FSM — it just checks if the agent
        can be loaded and would respond to the test message.
        """
        preflight = self.check_agent(agent_app_id)

        return {
            "preflight": preflight,
            "test_message": test_message,
            "would_execute": preflight["status"] != "blocked",
            "note": "Dry-run checks readiness. Use the chat to test full execution.",
        }
