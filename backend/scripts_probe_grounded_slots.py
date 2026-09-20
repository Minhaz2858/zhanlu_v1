"""P4 live probe: which question units are business metrics, and which are not.

The measured failures this probe witnesses:

* ``表 _ref_material_mapping 一共有多少行数据？`` returned ``COUNT(*)`` correctly and
  then disclosed that the source "has nothing for 一共有多少行数据,
  _ref_material_mapping";
* a structure question (表名/字段清单) produced slots for 订单 and 金额相关字段;
* a metadata query produced ``join path 'columns' is neither selected nor
  authorized`` for information_schema/pg_namespace.

Run (inside the container; ``backend/`` is a bind mount at ``/app``)::

    docker exec -i zhanlu-copy-backend python -u scripts_probe_grounded_slots.py \
        --kb aipdp_data_warehouse_prod --question '表 _ref_material_mapping 一共有多少行数据？'

    docker exec -i zhanlu-copy-backend python -u scripts_probe_grounded_slots.py \
        --kb PG-AUTO-API-ONLY \
        --question 'how many rows are in the customers table?' \
        --question '有哪些表，各自的字段清单是什么'

``--cold`` clears plan memory for the KB first, so a run cannot be answered from
a remembered plan. Reads only.

Prints, per question: ``unsupported_slots``, ``disclosure``,
``result_set.validation``, ``canonical_count``, the classified units, and the
narrative — the before/after witness for the non-metric-unit change.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from app.database import SessionLocal
from app.models.knowledge_base import KnowledgeBase

DEFAULT_QUESTIONS = [
    "表 _ref_material_mapping 一共有多少行数据？",
]


def _kb(db, name: str) -> KnowledgeBase:
    kb = (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.name == name, KnowledgeBase.is_deleted == False)  # noqa: E712
        .first()
    )
    if kb is None:
        raise SystemExit(f"no KB named {name!r} in this stack")
    return kb


def _clear_plan_memory(db, kb_id: str) -> int:
    from sqlalchemy import text

    return db.execute(
        text("delete from query_plan_memory where kb_id = :kb"), {"kb": kb_id}
    ).rowcount


def _show(tag: str, payload) -> None:
    print(f"  {tag:16}= " + json.dumps(payload, ensure_ascii=False, default=str)[:1400])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kb", required=True)
    parser.add_argument("--question", action="append", default=[])
    parser.add_argument("--cold", action="store_true")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        kb = _kb(db, args.kb)
        print(f"KB  {kb.name}  ({kb.db_type})  id={kb.id}")
        if args.cold:
            print(f"plan memory cleared: {_clear_plan_memory(db, kb.id)} row(s)")

        from app.services.db.nl_answer_service import NLAnswerService

        for question in args.question or DEFAULT_QUESTIONS:
            print("\n" + "=" * 72)
            print(f"QUESTION  {question}")
            result = asyncio.run(NLAnswerService(db).answer(kb.id, question))
            db.commit()

            print("\n── result ────────────────────────────────────────────────")
            print(f"success  = {result.get('success')}   error={result.get('error') or '-'}")
            print(f"rows     = {len(result.get('rows') or [])}")
            print(f"columns  = {result.get('columns')}")
            _show("unsupported", result.get("unsupported_slots") or [])
            _show("disclosure", result.get("disclosure") or "")
            _show("canonical_count", result.get("canonical_count") or {})

            print("\n── rows ─────────────────────────────────────────────────")
            for row in (result.get("rows") or [])[:5]:
                print("  " + json.dumps(row, ensure_ascii=False, default=str)[:600])

            print("\n── sql ──────────────────────────────────────────────────")
            print("  " + (result.get("sql") or "").replace("\n", "\n  ")[:900])

            plan = result.get("plan") or {}
            print("\n── plan ─────────────────────────────────────────────────")
            _show("tables", plan.get("tables"))
            _show("unsupported", plan.get("unsupported_slots") or [])
            _show("non_metric", plan.get("non_metric_units") or [])
            _show("evidence.kinds", [
                {"slot": e.get("slot"), "unit": e.get("unit"), "kind": e.get("kind")}
                for e in (plan.get("evidence") or [])
                if e.get("slot")
            ])
            plan_verdicts = (plan.get("unsupported_slots"), plan.get("join_paths"))
            print(f"  plan.join_paths = {json.dumps(plan_verdicts[1], ensure_ascii=False)}")

            crs = result.get("result_set") or {}
            print("\n── result_set ───────────────────────────────────────────")
            _show("validation", crs.get("validation"))
            _show("unsupported", crs.get("unsupported_slots") or [])
            _show("disclosure", crs.get("disclosure") or "")
            _show("canonical_count", crs.get("canonical_count") or {})
            _show("freshness", crs.get("freshness"))

            print("\n── narrative ────────────────────────────────────────────")
            print("  " + str(result.get("answer") or "").replace("\n", "\n  ")[:1200])
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
