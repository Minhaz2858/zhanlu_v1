#!/usr/bin/env python3
"""P0 Phase 3: publish the 7 manufacturing agents and bind the sandbox KB.

Idempotent; run inside the backend container:

    docker exec -w /app -e PYTHONPATH=/app zhanlu-backend python \
        /app/scripts/publish_manufacturing_agents.py

Publishing makes the agents visible to `list_available_agents` (delegation
directory) — the Phase-1 breakpoint ① fix. KB binding merges (never
overwrites existing bindings).
"""
from sqlalchemy import text

from app.database import SessionLocal
from app.models.agent_app import AgentApp

AGENTS = [
    "Predictive Maintenance Agent",
    "Quality Control Agent",
    "Supply Chain Optimization Agent",
    "Production Scheduling Agent",
    "Inventory Management Agent",
    "Autonomous Vehicle Systems Agent",
    "Demand Forecasting Agent",
]

SANDBOX_KB_NAME = "Auto Sales Sandbox (Pilot)"

# Model pinning: the org default resolved to qwen3.7-flash whose DashScope
# free tier was exhausted mid-P0 (live error: AllocationQuota.FreeTierOnly).
# The routing precedence is Project > Agent > catalog: an AgentApp pins its
# LLM via the `llm_model_id` FK into llm_models (NOT the legacy `model`
# string). Point the agents at an ACTIVE, KEYED catalog row. Verified
# 2026-10-31 with test_llm_endpoint probes: qwen3.7-flash = 403 quota
# exhausted (same DashScope account as the env key); qwen3.5-omni-plus =
# 200 OK (separate funded account). The keyed deepseek/glm rows are all
# is_deleted=true, so omni-plus is the only working keyed option today.
PINNED_MODEL_ID_SQL = (
    "SELECT id FROM llm_models WHERE model_id = 'qwen3.5-omni-plus' "
    "AND org_id = 'default-org' AND COALESCE(api_key, '') <> '' "
    "AND is_deleted = false ORDER BY created_date DESC LIMIT 1"
)


def main():
    from app.models.knowledge_base import KnowledgeBase

    db = SessionLocal()
    try:
        kb = (
            db.query(KnowledgeBase)
            .filter(
                KnowledgeBase.name == SANDBOX_KB_NAME,
                KnowledgeBase.is_deleted == False,  # noqa: E712
            )
            .first()
        )
        assert kb is not None, (
            "sandbox KB not found — run scripts/pilot_demand_forecast_e2e.py first"
        )
        for name in AGENTS:
            app = (
                db.query(AgentApp)
                .filter(AgentApp.name == name, AgentApp.is_deleted == False)  # noqa: E712
                .first()
            )
            assert app is not None, f"missing AgentApp {name}"
            changed = []
            if app.status != "published":
                app.status = "published"
                changed.append("published")
            pinned = db.execute(text(PINNED_MODEL_ID_SQL)).scalar()
            if pinned and app.llm_model_id != pinned:
                app.llm_model_id = pinned
                changed.append(f"llm_model_id={pinned[:8]}")
            kbs = list(app.knowledge_bases or [])
            if kb.id not in kbs:
                kbs.append(kb.id)
                app.knowledge_bases = kbs
                changed.append(f"bound KB {kb.id[:8]}")
            db.add(app)
            print(f"{name}: status={app.status} kbs={len(app.knowledge_bases or [])} "
                  f"{'| ' + ', '.join(changed) if changed else '(no change)'}")
        db.commit()
        print("PUBLISHED %d agents" % len(AGENTS))
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
