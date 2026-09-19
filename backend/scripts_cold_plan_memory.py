"""Snapshot and clear plan memory for the four probe questions (a COLD run).

``query_plan_memory`` is why a repeat of these questions looked answered: the
probe's own earlier runs saved verified plans, and retrieval then scored those
tables 3.0 "verified plan for this question" — i.e. the ranking was being helped
by the answer it was supposed to be measuring. A cold run means no plan.

The rows are dumped to JSON first (nothing is destroyed silently); the probe
saves fresh ones as it verifies answers, which is the intended steady state.

    docker exec zhanlu-copy-backend sh -lc \
      'cd /app && python scripts_cold_plan_memory.py aipdp_data_warehouse_prod'
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from app.database import SessionLocal
from app.models.knowledge_base import KnowledgeBase
from app.models.query_plan_memory import QueryPlanMemory
from app.services.db.plan_memory import question_signature

KB_NAME = sys.argv[1] if len(sys.argv) > 1 else "aipdp_data_warehouse_prod"
OUT = Path("/app/plan_memory_snapshot.json") if Path("/app").exists() else Path("./plan_memory_snapshot.json")

QUESTIONS = [
    "本周销售情况，生成包含规格、编码、单价、数量、金额的多维度分析报表",
    "这个月的合同执行情况",
    "2026年8月合同执行情况总览：合同签订数量、实际发货数量、履约率(发货/合同)、合同金额、发货金额",
    "按客户分组统计 2026 年 9 月的合同签订数量",
]


def main() -> None:
    db = SessionLocal()
    kb = db.query(KnowledgeBase).filter(KnowledgeBase.name == KB_NAME).first()
    if kb is None:
        print(f"KB {KB_NAME!r} not found")
        return

    sigs = {question_signature(q): q for q in QUESTIONS}
    rows = (
        db.query(QueryPlanMemory)
        .filter(QueryPlanMemory.kb_id == kb.id, QueryPlanMemory.is_deleted.is_(False))
        .all()
    )
    print(f"plan memory rows for {kb.name}: {len(rows)}")
    snapshot = []
    hit = 0
    for r in rows:
        if r.signature not in sigs:
            continue
        hit += 1
        snapshot.append(
            {
                "id": r.id, "signature": r.signature, "question": r.question,
                "tables": list(r.tables or []), "sql": r.sql or "",
                "dimensions": list(r.dimensions or []), "hits": r.hits or 0,
                "verified_at": str(r.verified_at),
            }
        )
        print(f"  clearing: {r.question[:70]!r}")
        db.delete(r)
    db.commit()
    OUT.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
    print(f"deleted {hit} row(s) for the four probe questions; snapshot -> {OUT}")


if __name__ == "__main__":
    main()
