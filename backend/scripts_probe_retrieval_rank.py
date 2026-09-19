"""Rank-only probe: what does deterministic retrieval offer for these questions?

The 2026-09-18 failure was not in the model — it was in what the model was shown.
``scripts_probe_answer_quality.py`` measures the ANSWER; this measures the RANK,
which is where the four live questions were actually lost: pure-CJK questions had
no lexical match at all (``_tokens`` cannot split Chinese), so ``rank_relations``
returned nothing with real evidence and the ordering fell to whatever weak signal
remained.

Prints, per question: the retrieval method, the top-K candidates in rank order
with their score and their evidence strings, and the wall-clock cost of the
retrieval call. Run against the live warehouse to compare before/after.

    docker exec zhanlu-copy-backend sh -lc \
      'cd /app && python scripts_probe_retrieval_rank.py > /tmp/rank.out 2>&1'
"""
from __future__ import annotations

import json
import sys
import time

from app.database import SessionLocal
from app.models.knowledge_base import KnowledgeBase
from app.services.db.table_retrieval import retrieve_for_question
from app.services.knowledge_graph.business_context import parse_relative_window
from app.services.knowledge_graph.schema_linker import build_full_toc

KB_NAME = sys.argv[1] if len(sys.argv) > 1 else "aipdp_data_warehouse_prod"
TOP_K = int(sys.argv[2]) if len(sys.argv) > 2 else 12

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
    print(f"KB {kb.name} catalog_status={kb.catalog_status} kb_id={kb.id}")

    t0 = time.perf_counter()
    toc = build_full_toc(db, [kb.id])
    print(f"TOC: {len(toc)} tables built in {time.perf_counter() - t0:.2f}s\n")

    for i, q in enumerate(QUESTIONS, 1):
        window = None
        try:
            window = parse_relative_window(q)
        except Exception as e:  # absolute periods are not parsed — noted, not fixed here
            print(f"  window parse failed: {e}")
        t1 = time.perf_counter()
        res = retrieve_for_question(
            db, [kb.id], q, toc, window=window, limit=40,
        )
        dt = time.perf_counter() - t1
        print("=" * 100)
        print(f"Q{i}: {q}")
        print(f"  method={res.method} candidates={len(res.candidates)} "
              f"window={res.window} uncovered={res.uncovered} {dt:.3f}s")
        for c in res.candidates[:TOP_K]:
            print(f"    {c.score:7.3f}  {c.table_name:38s} {list(c.evidence)}")
        print(f"  breakdown: {json.dumps(res.as_dict().get('candidates', [])[:3], ensure_ascii=False)}")


if __name__ == "__main__":
    main()
