"""Policy evaluator — evaluates execution plans against YAML policy packs.

Runs at two levels:
1. GATE (whole-plan) — before any execution begins
2. ACT (per-node) — before each node executes

Policy packs are YAML files in backend/policies/:
- default.yaml — baseline rules for all executions
- artifact.yaml — rules for artifact-generating tasks
- skill_review.yaml — rules for skill review pipeline

The evaluator returns a PolicyDecision:
- decision: "allow" | "deny" | "require_confirm"
- risk_tier: "low" | "medium" | "high"
- reasons: list of human-readable explanations
- conditions: list of conditions that must be met
"""

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Policy pack directory
POLICY_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "policies")

# In-memory cache of loaded policy packs
_policy_cache = {}


def load_policy_pack(name: str = "default") -> dict:
    """Load a YAML policy pack by name.

    Falls back to default rules if the file doesn't exist.
    """
    if name in _policy_cache:
        return _policy_cache[name]

    policy_path = os.path.join(POLICY_DIR, f"{name}.yaml")

    if os.path.exists(policy_path):
        try:
            import yaml
            with open(policy_path, "r") as f:
                pack = yaml.safe_load(f) or {}
            _policy_cache[name] = pack
            logger.info("Loaded policy pack: %s (%d rules)", name, len(pack.get("rules", [])))
            return pack
        except Exception as e:
            logger.warning("Failed to load policy pack %s: %s — using defaults", name, e)

    # Default inline policy
    default_pack = {
        "name": name,
        "rules": [
            {
                "id": "allow_low_risk",
                "condition": "risk_tier == 'low'",
                "decision": "allow",
                "description": "Low-risk tasks are always allowed",
            },
            {
                "id": "confirm_medium_risk",
                "condition": "risk_tier == 'medium'",
                "decision": "require_confirm",
                "description": "Medium-risk tasks require user confirmation",
            },
            {
                "id": "deny_high_risk_without_approval",
                "condition": "risk_tier == 'high' and not has_approval",
                "decision": "deny",
                "description": "High-risk tasks require explicit approval",
            },
        ],
    }
    _policy_cache[name] = default_pack
    return default_pack


def evaluate_plan(plan, task_spec: dict, agent_name: str) -> dict:
    """Evaluate a plan at the GATE level (whole-plan check).

    Returns a PolicyDecision dict:
        decision: "allow" | "deny" | "require_confirm"
        risk_tier: "low" | "medium" | "high"
        reasons: list[str]
        conditions: list[str]
    """
    # Determine risk tier based on plan content
    risk_tier = _assess_risk(plan, task_spec)
    reasons = []
    conditions = []

    # Load relevant policy packs
    default_pack = load_policy_pack("default")

    # Check if artifact generation is involved
    has_artifact = any(
        node.output_artifact_type for node in (plan.nodes if plan else [])
    )
    if has_artifact:
        artifact_pack = load_policy_pack("artifact")
        # Apply artifact-specific rules
        for rule in artifact_pack.get("rules", []):
            if rule.get("decision") == "require_confirm":
                conditions.append(rule.get("description", "Artifact rule"))

    # Determine decision based on risk tier
    if risk_tier == "low":
        decision = "allow"
        reasons.append("Low-risk task — auto-approved")
    elif risk_tier == "medium":
        decision = "require_confirm"
        reasons.append("Medium-risk task — user confirmation recommended")
    else:  # high
        decision = "require_confirm"
        reasons.append("High-risk task — explicit approval required")
        conditions.append("User must approve before execution")

    # Check for sandbox execution (always medium+ risk)
    has_sandbox = any(
        node.node_type == "sandbox" for node in (plan.nodes if plan else [])
    )
    if has_sandbox and risk_tier == "low":
        risk_tier = "medium"
        decision = "require_confirm"
        reasons.append("Sandbox execution elevates risk to medium")

    result = {
        "decision": decision,
        "risk_tier": risk_tier,
        "reasons": reasons,
        "conditions": conditions,
        "policy_packs": ["default"] + (["artifact"] if has_artifact else []),
    }

    logger.info("Policy decision: %s (risk=%s, reasons=%d)", decision, risk_tier, len(reasons))
    return result


def evaluate_node(node, policy_decision: dict) -> dict:
    """Evaluate a single plan node at the ACT level (per-node check).

    Returns:
        decision: "allow" | "deny" | "require_confirm"
        reason: str
    """
    # If the plan-level decision was "deny", all nodes are denied
    if policy_decision.get("decision") == "deny":
        return {"decision": "deny", "reason": "Plan-level denial"}

    # Sandbox nodes always require confirmation (unless already approved)
    if node.node_type == "sandbox":
        if policy_decision.get("risk_tier") in ("medium", "high"):
            return {
                "decision": "allow",  # Already confirmed at gate level
                "reason": "Sandbox execution approved at gate",
            }

    # NL2SQL nodes are low-risk (read-only)
    if node.node_type == "nl2sql":
        return {"decision": "allow", "reason": "Read-only data query"}

    # Default: allow
    return {"decision": "allow", "reason": "Node approved"}


def _assess_risk(plan, task_spec: dict) -> str:
    """Assess the risk tier of a plan based on its content."""
    if not plan or not plan.nodes:
        return "low"

    # High risk indicators
    for node in plan.nodes:
        if node.node_type == "sandbox":
            return "medium"  # Sandbox is medium by default, high if external
        if node.requires_confirmation:
            return "high"

    # Check task kind
    task_kind = task_spec.get("task_kind", "general")
    if task_kind == "configure_system":
        return "medium"  # System configuration is medium risk

    if task_spec.get("complexity") == "complex":
        return "medium"

    return "low"
