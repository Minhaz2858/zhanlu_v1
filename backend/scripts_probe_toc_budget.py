"""Counterfactual: is the 1,200-token TOC budget what breaks table selection?

Measured BEFORE this script (``scripts_probe_toc_selection.py``):
  * 143 catalog tables, ``format_toc_text(toc, token_budget=1200)`` renders 16
    of them, in ``build_full_toc``'s alphabetical order — the model never sees
    a single ``erp_t_*`` / ``sale_erp_v_*`` business table.
  * the model then picked ``['actual_price', '_ref_material_mapping',
    'chain_product_kind']`` for a sales question and
    ``['contract_execution', 'contract', 'contract_execution_detail']``
    (nonexistent names) for a contract question.

This holds the model, the question and the catalog fixed and varies ONLY the
render budget, so the difference is attributable to the budget rather than to
selection quality in general. Read-only; costs one LLM call per row.

    docker exec zhanlu-copy-backend sh -lc 'cd /app && python -u scripts_probe_toc_budget.py > /tmp/tb.out 2>&1'
"""
from __future__ import annotations

import asyncio

from app.database import SessionLocal
from app.models.knowledge_base import KnowledgeBase
from app.services.db.nl_answer_service import NLAnswerService
from app.services.knowledge_graph.schema_linker import build_full_toc, format_toc_text

KB_NAME = "aipdp_data_warehouse_prod"
QUESTIONS = [
    "这个月的合同执行情况",
    "本周销售情况，生成包含规格、编码、单价、数量、金额的多维度分析报表",
]
BUDGETS = [1200, 6000, 60000]


async def main() -> None:
    db = SessionLocal()
    kb = db.query(KnowledgeBase).filter(KnowledgeBase.name == KB_NAME).first()
    if kb is None:
        print(f"KB {KB_NAME!r} not found")
        return
    toc = build_full_toc(db, [kb.id])
    svc = NLAnswerService(db)
    total = len(toc)
    print(f"catalog tables: {total}")

    for budget in BUDGETS:
        text = format_toc_text(toc, token_budget=budget)
        shown = len([ln for ln in text.splitlines()
                     if ln.strip() and not ln.strip().startswith("--")
                     and not ln.strip().startswith("...")])
        cut = "yes" if "not shown due to budget" in text else "no"
        print(f"\n=== token_budget={budget}: chars={len(text)} tables_shown={shown} "
              f"truncated={cut} ===")
        for q in QUESTIONS:
            try:
                picked = await svc._select_tables_from_toc(q, text, total)
            except Exception as e:
                picked = f"ERR {type(e).__name__}: {e}"
            print(f"  Q: {q[:46]}")
            print(f"     -> {picked}")


if __name__ == "__main__":
    asyncio.run(main())
