#!/usr/bin/env python3
"""A/B probe: does the LLM planner actually produce the stored plan?

Runs the REAL ``plan_dag.generate_plan`` code path inside the backend
container against the REAL configured LLM endpoint, and reports which plan
ended up in the DB:

  * LLM-authored node names  -> planner worked
  * the generic default plan -> planner silently fell back (the bug)

Any "LLM plan generation failed" warning emitted during the call is captured
and printed, so a pass cannot be faked by an empty log window.
"""
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


_cap = Capture()
logging.getLogger("app.services.synexia.plan_dag").addHandler(_cap)

from app.database import SessionLocal  # noqa: E402
from app.models.execution import Execution  # noqa: E402
from app.services.synexia import plan_dag  # noqa: E402

SPEC = {
    "task_kind": "create_artifact",
    "artifact_intents": ["report"],
    "requires_data": True,
    "entities": {"time_range": "本周"},
    "user_message": (
        "本周销售情况,生成包含规格,编码,单价,数量,金额的多维度分析报表 ,直接输出"
    ),
}

db = SessionLocal()
try:
    ex = db.query(Execution).order_by(Execution.created_date.desc()).first()
    if ex is None:
        print("NO EXECUTION ROW — cannot satisfy plans.execution_id FK")
        sys.exit(2)
    print("execution_id:", ex.id)

    default_names = [s.get("name") for s in plan_dag._build_default_plan(SPEC, "general_assistant")]
    print("default plan names:", default_names)

    plan = plan_dag.generate_plan(
        db=db,
        execution_id=ex.id,
        task_spec=SPEC,
        context_manifest={},
        agent_name="general_assistant",
    )

    nodes = [(n.node_type, n.name) for n in plan.nodes]
    print("STORED PLAN NODES:", nodes)
    warns = [m for m in _cap.messages if "plan generation failed" in m]
    print("FALLBACK WARNINGS:", warns)
    print("VERDICT:", "FALLBACK (bug still present)" if warns else "LLM PLAN (fix working)")
    print("USED_DEFAULT_PLAN:", [n[1] for n in nodes] == default_names)
finally:
    db.close()
