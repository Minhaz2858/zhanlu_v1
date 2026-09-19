"""Q3, reproduced: "2026年8月合同执行情况总览".

The question asks for FIVE things: contract count, shipped quantity,
fulfilment rate, contract amount, shipped amount. Every guard in the pipeline
passed the answer through, because every guard stops at the rows: the contract
side is right, the shipped side comes back ``0`` / ``0.0``, and nothing
distinguishes "the warehouse has no shipments in August" from "the join that
was supposed to produce shipments did not resolve".

Run (inside the container, /app is a bind mount of the host backend/):

    docker exec -i zhanlu-copy-backend python -u scripts_probe_grounded_q3.py
    docker exec -i zhanlu-copy-backend python -u scripts_probe_grounded_q3.py --cold

``--cold`` clears plan memory for the KB first, so the run cannot be answered
from a remembered plan.

Reads only. Prints the result dict's P3 keys (``plan`` / ``result_set`` /
``unsupported_slots``) when the caller supports them, so the same script is the
before/after witness for the grounded-plan change.
"""

from __future__ import annotations

import asyncio
import json
import sys

from app.database import SessionLocal
from app.models.knowledge_base import KnowledgeBase

QUESTION = (
    "2026年8月合同执行情况总览：合同签订数量、实际发货数量、履约率(发货/合同)、"
    "合同金额、发货金额"
)
KB_NAME = "aipdp_data_warehouse_prod"


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


#: The measured failure shape, verbatim from the live run that produced it:
#: the contract side correct, the shipped side 0.0 because the delivery→contract
#: join did not resolve. `--zero` replays it through the REAL pipeline (real
#: plan, real contract check, real narration, real disclosure) by standing in
#: for the executor only, so the disclosure path is what is being tested — not
#: a hand-built payload.
MEASURED_ZERO_ROWS = [
    {
        "contract_count": 177,
        "shipped_qty": 0.0,
        "fulfillment_rate": 0.0,
        "contract_amount": 293325407.26,
        "shipment_amount": 0.0,
    }
]


def _install_zero_executor() -> None:
    """Make the executor return the measured zero rows, and nothing else."""
    from app.services.db.query_service import QueryService

    original = QueryService.execute

    def _execute(self, kb_id, sql, **kw):  # noqa: ANN001, ARG001
        return {
            "rows": [dict(r) for r in MEASURED_ZERO_ROWS],
            "columns": list(MEASURED_ZERO_ROWS[0].keys()),
            "sql": sql,
            "source_id": kb_id,
            "source": {"id": kb_id, "name": "INJECTED (measured failure shape)"},
            "row_count": len(MEASURED_ZERO_ROWS),
            "truncated": False,
        }

    QueryService.execute = _execute  # type: ignore[method-assign]
    print("executor REPLACED with the measured zero shape (contract ok, shipped 0.0)")
    return original


def main() -> int:
    cold = "--cold" in sys.argv or "--zero" in sys.argv
    if "--zero" in sys.argv:
        _install_zero_executor()
    db = SessionLocal()
    try:
        kb = _kb(db, KB_NAME)
        print(f"KB   {kb.name}  ({kb.db_type})  id={kb.id}")
        if cold:
            print(f"plan memory cleared: {_clear_plan_memory(db, kb.id)} row(s)")

        from app.services.db.nl_answer_service import NLAnswerService

        result = asyncio.run(NLAnswerService(db).answer(kb.id, QUESTION))
        db.commit()

        print("\n── result ────────────────────────────────────────────────")
        print(f"success  = {result.get('success')}")
        print(f"tables   = {result.get('provenance', {}).get('tables')}")
        print(f"window   = {result.get('provenance', {}).get('requested_window')}")
        print(f"rows     = {len(result.get('rows') or [])}")
        print(f"columns  = {result.get('columns')}")
        print(f"repaired = {result.get('repaired_from_error')}")
        for key in ("disclosure", "contract_violations", "unsupported_slots"):
            if result.get(key):
                print(f"{key:17}= {result[key]}")

        print("\n── rows ─────────────────────────────────────────────────")
        for row in (result.get("rows") or [])[:5]:
            print("  " + json.dumps(row, ensure_ascii=False, default=str))

        print("\n── sql ──────────────────────────────────────────────────")
        print("  " + (result.get("sql") or "").replace("\n", "\n  "))

        print("\n── plan (P3) ────────────────────────────────────────────")
        plan = result.get("plan")
        print("  absent" if plan is None else "  " + json.dumps(plan, ensure_ascii=False, indent=2)[:2600])

        print("\n── canonical result set (P3) ────────────────────────────")
        crs = result.get("result_set")
        if crs is None:
            print("  absent")
        else:
            slim = {k: v for k, v in crs.items() if k != "rows"}
            print("  " + json.dumps(slim, ensure_ascii=False, indent=2, default=str)[:2200])

        print("\n── narrative ────────────────────────────────────────────")
        print("  " + str(result.get("answer") or "").replace("\n", "\n  "))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
