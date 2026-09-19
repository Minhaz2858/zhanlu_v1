"""What the table selector ACTUALLY sees, and what it picks.

Shows BOTH surfaces, so the difference is visible rather than asserted:

  A. the LEGACY renderer (``format_toc_text(token_budget=1200)``) — the
     baseline that starved the selector: 14 of 143 tables, alphabetical head;
  B. the LIVE path (``retrieve_for_question`` → ``format_toc_for_selection``),
     which is what ``NLAnswerService.answer()`` now uses.

and then asks the same model the same questions over each, so the pick can be
compared instead of argued about.

Read-only. Run:

    docker exec zhanlu-copy-backend sh -lc 'cd /app && python -u scripts_probe_toc_selection.py > /tmp/toc.out 2>&1'
"""
from __future__ import annotations

import asyncio
import logging
import sys

from app.database import SessionLocal
from app.models.knowledge_base import KnowledgeBase
from app.services.db.nl_answer_service import NLAnswerService
from app.services.db.table_retrieval import (
    format_candidates_block,
    retrieve_for_question,
)
from app.services.knowledge_graph.schema_linker import (
    build_full_toc,
    format_toc_for_selection,
    format_toc_text,
)

KB_NAME = sys.argv[1] if len(sys.argv) > 1 else "aipdp_data_warehouse_prod"
QUESTIONS = [
    "这个月的合同执行情况",
    "本周销售情况，生成包含规格、编码、单价、数量、金额的多维度分析报表",
]


async def main() -> None:
    db = SessionLocal()
    kb = db.query(KnowledgeBase).filter(KnowledgeBase.name == KB_NAME).first()
    if kb is None:
        print(f"KB {KB_NAME!r} not found")
        return

    toc = build_full_toc(db, [kb.id])
    print(f"KB {kb.name}  catalog tables: {len(toc)}")
    print(f"  with row_count=0/missing: {len([t for t in toc if not t.get('row_count')])}")
    print(f"  with NO coverage max_date: {len([t for t in toc if not t.get('max_date')])}")

    legacy = format_toc_text(toc, token_budget=1200)
    legacy_shown = len([ln for ln in legacy.splitlines() if ln.startswith("  ") and "more tables" not in ln])
    print(f"\nA. LEGACY renderer: chars={len(legacy)} tables_shown={legacy_shown}/{len(toc)}"
          f" truncated={'yes' if 'not shown due to budget' in legacy else 'no'}")

    svc = NLAnswerService(db)
    for q in QUESTIONS:
        print("\n" + "=" * 100)
        print(f"Q: {q}")
        res = retrieve_for_question(db, [kb.id], q, toc)
        print(f"  retrieval: method={res.method} candidates={len(res.candidates)} "
              f"uncovered={res.uncovered}")
        for c in res.candidates[:8]:
            print(f"    {c.score:>5.2f} {c.table_name:<34} {c.evidence}")

        block = format_candidates_block(res)
        live = format_toc_for_selection(toc, res.names)
        if block:
            live = f"{block}\n\n{live}"
        catalog_names = {t["table_name"] for t in toc}

        print(f"  B. LIVE surface: chars={len(live)} "
              f"candidates_rendered={len(res.names)}")

        for label, text in (("A/l legacy", legacy), ("B/ live  ", live)):
            try:
                picked = await svc._select_tables_from_toc(
                    q, text, len(toc), known_names=catalog_names,
                )
            except Exception as e:
                picked = f"ERR {type(e).__name__}: {e}"
            print(f"    {label} -> {picked}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
