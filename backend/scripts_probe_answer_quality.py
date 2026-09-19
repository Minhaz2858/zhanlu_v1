"""Reproduce the user's complaint end to end: "the agent cannot answer from the DB".

Drives the REAL ``NLAnswerService.answer()`` (the sealed path the Data Agent
calls) with the exact questions that failed in the live chat on 2026-09-18, and
prints, per question:

  * the table-selection stage's log lines (INFO forced on in-process)
  * the SQL the model wrote
  * the row count and the answer's first lines
  * the wall-clock time

Ground truth to compare against (measured by ``scripts_probe_data_truth.py``):
``erp_product_sales_details`` has 0 rows after 2025-12-31, while 8 other tables
carry 2026-08/09 rows (erp_t_sal_deliverynotice 2,191, erp_t_crm_contract 297, …).

Run:

    docker exec zhanlu-copy-backend sh -lc 'cd /app && python scripts_probe_answer_quality.py > /tmp/aq.out 2>&1'
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time

from app.database import SessionLocal
from app.models.knowledge_base import KnowledgeBase
from app.services.db.nl_answer_service import NLAnswerService

KB_NAME = sys.argv[1] if len(sys.argv) > 1 else "aipdp_data_warehouse_prod"

QUESTIONS = [
    "本周销售情况，生成包含规格、编码、单价、数量、金额的多维度分析报表",
    "这个月的合同执行情况",
    "2026年8月合同执行情况总览：合同签订数量、实际发货数量、履约率(发货/合同)、合同金额、发货金额",
    "按客户分组统计 2026 年 9 月的合同签订数量",
]


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        self.records.append(record.getMessage())


async def main() -> None:
    db = SessionLocal()
    kb = db.query(KnowledgeBase).filter(KnowledgeBase.name == KB_NAME).first()
    if kb is None:
        print(f"KB {KB_NAME!r} not found")
        return
    print(f"KB {kb.name} catalog_status={kb.catalog_status}")

    target = logging.getLogger("app.services.db.nl_answer_service")
    prev = target.level
    target.setLevel(logging.INFO)
    cap = Capture()
    target.addHandler(cap)

    for i, q in enumerate(QUESTIONS, 1):
        cap.records.clear()
        print("\n" + "=" * 100)
        print(f"Q{i}: {q}")
        t0 = time.perf_counter()
        try:
            res = await NLAnswerService(db).answer(
                question=q, kb_id=kb.id, skip_narrate=True,
            )
        except Exception as e:
            print(f"  RAISED {type(e).__name__}: {e}")
            continue
        dt = time.perf_counter() - t0
        rows = res.get("rows") or []
        print(f"  success={res.get('success')}  rows={len(rows)}  {dt:.1f}s  "
              f"short_circuit={res.get('short_circuit')}")
        print(f"  SQL: {(res.get('sql') or '(none)')[:400]}")
        if rows:
            print(f"  row[0]: {str(rows[0])[:220]}")
        ans = (res.get("answer") or "").replace("\n", " ")
        print(f"  ANSWER: {ans[:400]}")
        print("  --- selection / pipeline log lines ---")
        for line in cap.records:
            if any(k in line for k in ("candidate retrieval", "domain scoping",
                                       "semantic retrieval", "TOC", "static route",
                                       "zero-row", "coverage probe", "freshness",
                                       "violation", "retry", "repair", "saved verified plan",
                                       "reusing verified plan", "falling back")):
                print(f"    * {line}")

    target.removeHandler(cap)
    target.setLevel(prev)


if __name__ == "__main__":
    asyncio.run(main())
