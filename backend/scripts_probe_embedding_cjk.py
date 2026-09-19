"""Is the configured embedder usable for Chinese here — measured, not assumed?

The fusion in ``retrieval_evidence`` has an embedding component and it is GATED
by whether the similarity distribution carries signal (top − median), not by a
model-name capability table. This probe produces the numbers that justify the
gate on the live catalog: for each of the four questions that failed live, it
embeds every catalog table's description and reports the distribution and where
the tables that actually hold the answer land.

    docker exec zhanlu-copy-backend sh -lc \
      'cd /app && python scripts_probe_embedding_cjk.py > /tmp/emb.out 2>&1'
"""
from __future__ import annotations

import statistics
import sys
import time

from app.database import SessionLocal
from app.models.knowledge_base import KnowledgeBase
from app.services.db.lexical import cosine

KB_NAME = sys.argv[1] if len(sys.argv) > 1 else "aipdp_data_warehouse_prod"

CASES = [
    (
        "本周销售情况，生成包含规格、编码、单价、数量、金额的多维度分析报表",
        ("erp_v_sale_huizhou", "erp_t_sal_orderentry", "erp_v_sale_guangdong"),
        ("auth_user", "auth_group", "alert_rules"),
    ),
    (
        "这个月的合同执行情况",
        ("erp_t_crm_contract", "erp_t_crm_contractentry", "erp_v_contract_execution"),
        ("auth_user", "auth_group", "chain_product_kind"),
    ),
    (
        "2026年8月合同执行情况总览：合同签订数量、实际发货数量、履约率(发货/合同)、合同金额、发货金额",
        ("erp_t_crm_contract", "erp_t_sal_deliverynotice", "erp_v_contract_execution"),
        ("auth_user", "actual_price"),
    ),
    (
        "按客户分组统计 2026 年 9 月的合同签订数量",
        ("erp_t_crm_contract",),
        ("auth_user", "market_prices"),
    ),
]


def main() -> None:
    from app.services.knowledge_graph.schema_linker import build_full_toc

    db = SessionLocal()
    kb = db.query(KnowledgeBase).filter(KnowledgeBase.name == KB_NAME).first()
    if kb is None:
        print(f"KB {KB_NAME!r} not found")
        return
    toc = build_full_toc(db, [kb.id])
    print(f"KB {kb.name}: {len(toc)} tables")

    from app.services.document_ingestion.embedder import (
        _resolve_model_kind,
        get_embedding_function,
    )

    kind = _resolve_model_kind()
    t0 = time.perf_counter()
    try:
        ef = get_embedding_function()
        name = ef.name() if hasattr(ef, "name") else "?"
    except Exception as e:
        print(f"embedder FAILED to load (kind={kind!r}): {type(e).__name__}: {e}")
        print("=> the component must degrade to a recorded 'unavailable' note")
        return
    print(f"RAG_EMBEDDING_MODEL kind={kind!r} loaded {name!r} in {time.perf_counter() - t0:.2f}s")

    docs = [
        f"{t['table_name']}: {t.get('description_zh') or ''} {t.get('description_en') or ''}"
        for t in toc
    ]
    t1 = time.perf_counter()
    doc_vecs = ef(docs)
    print(f"embedded {len(docs)} docs in {time.perf_counter() - t1:.2f}s\n")

    for question, relevant, unrelated in CASES:
        qvec = ef([question])[0]
        sims = [(cosine(qvec, v), t["table_name"]) for v, t in zip(doc_vecs, toc)]
        values = [s for s, _ in sims]
        top, med, lo = max(values), statistics.median(values), min(values)
        ranks = {nm: i + 1 for i, (_s, nm) in enumerate(sorted(sims, reverse=True))}
        print(f"Q: {question}")
        print(f"   cosine  max={top:.4f}  median={med:.4f}  min={lo:.4f}  "
              f"spread(max-median)={top - med:.4f}")
        best_rel = min((ranks.get(nm, 10**6) for nm in relevant), default=None)
        best_unrel = min((ranks.get(nm, 10**6) for nm in unrelated), default=None)
        print(f"   best relevant rank={best_rel}  best unrelated rank={best_unrel}  -> "
              f"{'relevant leads' if best_rel and best_unrel and best_rel < best_unrel else 'NOISE'}")
        for nm in relevant:
            print(f"     relevant {nm:32s} rank {ranks.get(nm, '-')}  "
                  f"cos={dict((t, s) for s, t in sims).get(nm, 0):.4f}")
        print()


if __name__ == "__main__":
    main()
