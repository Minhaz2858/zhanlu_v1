"""Seed golden test cases into agent_test_cases (idempotent upsert by name).

Golden cases are the regression corpus for the eval loop (GAP 2 build,
2026-08-29): every case is a known user_message with expected behavior
(accuracy floor, artifact type, grounding requirement). The eval CI harness
and the daily eval pipeline both judge real turns against these.

Usage:
    python -m scripts.seed_golden_test_cases [--app general_assistant]

Idempotent: existing cases with the same name are updated, not duplicated.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.database import SessionLocal
from app.models.agent_app import AgentApp
from app.models.agent_test_case import AgentTestCase

# Golden corpus. Each case:
#   name              unique slug
#   description       one-line intent
#   test_type         unit|integration|acceptance|regression
#   user_message      the exact user prompt the agent must handle
#   expected_behavior free-text behavioral contract
#   assertions        JSON list of deterministic assertions
#   expected_output   accuracy floor + artifact type + grounding flag
GOLDEN_CASES: list[dict] = [
    {
        "name": "general-greeting",
        "description": "Plain greeting — must answer helpfully, no tools needed.",
        "test_type": "unit",
        "user_message": "Hello, what can you help me with?",
        "expected_behavior": (
            "Respond with a helpful overview of capabilities; never fabricate "
            "data or call tools unnecessarily."
        ),
        "assertions": ["no_failed_required_tools"],
        "expected_output": {"expected_accuracy": 0.6, "require_no_failed_required": True},
    },
    {
        "name": "sql-query",
        "description": "NL → SQL over the connected datasource, grounded in schema.",
        "test_type": "integration",
        "user_message": "What were total sales last month?",
        "expected_behavior": (
            "Inspect the schema first, produce real SQL against the connected "
            "datasource, and report the number with its source. Never guess "
            "tables or columns."
        ),
        "assertions": ["schema_inspected", "real_sql", "grounding_must_pass"],
        "expected_output": {"expected_accuracy": 0.8, "grounding_must_pass": True},
    },
    {
        "name": "dashboard-build",
        "description": "Live dashboard build — must pass the quality gate (grade A/B).",
        "test_type": "acceptance",
        "user_message": "Build a sales dashboard for me.",
        "expected_behavior": (
            "Produce a live dashboard via create_fullstack_dashboard with "
            ">=2 sections, cross-widget filters, KPI row, trend and breakdown "
            "widgets; the build's own quality report must not be grade C."
        ),
        "assertions": ["dashboard_quality_not_c", "filters_declared", "sections_gt_1"],
        "expected_output": {"expected_accuracy": 0.8, "expected_artifact_type": "dashboard"},
    },
    {
        "name": "docx-memo",
        "description": "Word document generation.",
        "test_type": "acceptance",
        "user_message": "Write a one-page Word memo about the new leave policy.",
        "expected_behavior": (
            "Produce a real .docx deliverable (not a text description of one) "
            "with the requested content."
        ),
        "assertions": ["artifact_created"],
        "expected_output": {"expected_accuracy": 0.8, "expected_artifact_type": "docx", "expected_min_confidence": 0.5},
    },
    {
        "name": "pptx-report",
        "description": "PowerPoint deck generation.",
        "test_type": "acceptance",
        "user_message": "Create a 3-slide PPT summarizing quarterly revenue.",
        "expected_behavior": (
            "Produce a real .pptx deliverable (not a text description of one) "
            "with the requested slides."
        ),
        "assertions": ["artifact_created"],
        "expected_output": {"expected_accuracy": 0.8, "expected_artifact_type": "pptx", "expected_min_confidence": 0.5},
    },
    {
        "name": "delegation-parallel",
        "description": "3+ independent asks must fan out via delegate_task.",
        "test_type": "regression",
        "user_message": (
            "List the top 5 customers by revenue, top 5 products by volume, "
            "and top 3 regions by margin."
        ),
        "expected_behavior": (
            "Recognize the independent workstreams and either delegate via "
            "delegate_task(tasks=[...]) or answer all three accurately with "
            "real data. Never answer only one of the three."
        ),
        "assertions": ["all_parts_answered"],
        "expected_output": {"expected_accuracy": 0.8, "require_no_failed_required": True},
    },
]


def _resolve_app_id(db, app_name: str) -> str:
    app = db.query(AgentApp).filter(AgentApp.name == app_name).first()
    if not app:
        raise SystemExit(
            f"agent_app '{app_name}' not found — pass --app with an existing app name"
        )
    return app.id


def seed(db, app_name: str = "general_assistant") -> int:
    app_id = _resolve_app_id(db, app_name)
    upserted = 0
    for case in GOLDEN_CASES:
        existing = (
            db.query(AgentTestCase)
            .filter(AgentTestCase.agent_app_id == app_id, AgentTestCase.name == case["name"])
            .first()
        )
        payload = dict(case)
        del payload["name"]
        payload["input_json"] = {"user_message": payload.pop("user_message")}
        payload["expected_output_json"] = payload.pop("expected_output")
        if existing:
            for key, value in payload.items():
                setattr(existing, key, value)
        else:
            existing = AgentTestCase(
                agent_app_id=app_id,
                name=case["name"],
                status="pending",
                **payload,
            )
            db.add(existing)
        upserted += 1
    db.commit()
    return upserted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", default="general_assistant", help="agent_app name to bind cases to")
    args = parser.parse_args()
    db = SessionLocal()
    try:
        count = seed(db, app_name=args.app)
        print(f"seeded/upserted {count} golden test cases for app '{args.app}'")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
