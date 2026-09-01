"""Backfill dashboard + UI UX tools onto existing user-facing agents.

Idempotent: merges required tools into AgentApp.tool_config.enabled_tools for
agents that are expected to build live dashboards from chat.
"""
import sys

sys.path.insert(0, ".")

from app.database import SessionLocal
from app.models import AgentApp

REQUIRED_TOOLS = [
    "create_dashboard",
    "update_dashboard",
    "undo_dashboard_edit",
    "uiux_search",
    "uiux_design_system",
]

TARGET_AGENT_NAMES = {
    "general_assistant",
    "ecisco_bi_assistant",
}


def merge_tools(existing):
    config = dict(existing or {})
    enabled = list(config.get("enabled_tools") or [])
    for tool in REQUIRED_TOOLS:
        if tool not in enabled:
            enabled.append(tool)
    config["enabled_tools"] = enabled
    return config


def main():
    db = SessionLocal()
    try:
        rows = db.query(AgentApp).filter(AgentApp.name.in_(TARGET_AGENT_NAMES)).all()
        for row in rows:
            before = set((row.tool_config or {}).get("enabled_tools") or [])
            row.tool_config = merge_tools(row.tool_config)
            after = set(row.tool_config["enabled_tools"])
            added = sorted(after - before)
            print(f"{row.name}: added={added or []}")
        db.commit()
        print("DONE")
    finally:
        db.close()


if __name__ == "__main__":
    main()
