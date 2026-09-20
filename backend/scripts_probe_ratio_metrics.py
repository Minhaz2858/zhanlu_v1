"""The ≈17,957% ratio, replayed through the REAL pipeline — before/after witness.

The measured failure: ``履约率`` came back ≈17,957% because it was computed as
``SUM(out_fqty) / COUNT(DISTINCT FID)`` — a summed QUANTITY divided by a count of
contract DOCUMENTS. The SQL ran, the columns existed, the values were non-zero,
so nothing convicted it and the figure was published as a percentage with only a
"treat as unverified" caveat.

What this probe shows, on the live warehouse and the live plan:

* A (``--ab`` only): the same rows through the pipeline with the ratio check
  removed — the pre-change behaviour, for comparison.
* B: the same rows through the pipeline with the ratio check — the ratio is
  refused (``ratio_unverified``), the warning reaches the narrator, and the
  numerator/denominator that ARE sound stay published.

Reads only (the executor is stood in for, as ``scripts_probe_grounded_q3.py
--zero`` does); nothing is written except the run's own chat row, which the
service owns.

Run inside the container (``/app`` is a bind mount of the host ``backend/``):

    docker exec -i zhanlu-copy-backend python -u scripts_probe_ratio_metrics.py
    docker exec -i zhanlu-copy-backend python -u scripts_probe_ratio_metrics.py --ab
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

#: The measured ratio, verbatim: 176 contract documents, 3,160,537.6 shipped over
#: the window, hence 17,957.6 "percent" from the quantity/count division.
MEASURED_RATIO_ROWS = [
    {
        "contract_count": 176,
        "shipped_qty": 3160537.6,
        "fulfillment_rate": 17957.6,
        "contract_amount": 293325407.26,
        "shipment_amount": 3160537.6,
    }
]


def _kb(db, name: str) -> KnowledgeBase:
    kb = db.query(KnowledgeBase).filter(KnowledgeBase.name == name).one_or_none()
    if kb is None:
        raise SystemExit(f"KB {name!r} not found")
    return kb


def _install_measured_executor() -> None:
    from app.services.db.query_service import QueryService

    def _execute(self, kb_id, sql, **kw):  # noqa: ANN001, ARG001
        return {
            "rows": [dict(r) for r in MEASURED_RATIO_ROWS],
            "columns": list(MEASURED_RATIO_ROWS[0].keys()),
            "sql": sql,
            "source_id": kb_id,
            "source": {"id": kb_id, "name": "INJECTED (measured ≈17,957% shape)"},
            "row_count": len(MEASURED_RATIO_ROWS),
            "truncated": False,
        }

    QueryService.execute = _execute  # type: ignore[method-assign]
    print("executor REPLACED with the measured ratio shape (履约率 = 17,957.6)")


def _disable_ratio_check() -> None:
    """The pre-change behaviour, exactly: the CRS never validated a ratio."""
    from app.services.db import grounding

    grounding.validate_ratio_columns = lambda *a, **kw: []  # type: ignore[assignment]
    grounding.ratio_directive = lambda *a, **kw: ""  # type: ignore[assignment]
    print("ratio check DISABLED for this run (pre-change comparison)")


def _run(kb_id: str, label: str) -> dict:
    from app.services.db.nl_answer_service import NLAnswerService

    db = SessionLocal()
    try:
        result = asyncio.run(NLAnswerService(db).answer(kb_id, QUESTION))
        db.commit()
    finally:
        db.close()

    print(f"\n── {label} ───────────────────────────────────────")
    print(f"success   = {result.get('success')}")
    print(f"rows      = {json.dumps(result.get('rows'), ensure_ascii=False, default=str)[:400]}")
    print(f"sql       = {(result.get('sql') or '').replace(chr(10), ' ')}")
    result_set = result.get("result_set") or {}
    print(f"complete  = {result_set.get('complete')}")
    verdicts = result_set.get("ratio_validations") or []
    print(f"ratios    = {len(verdicts)} validated")
    for verdict in verdicts:
        print(
            "ratio     = "
            + json.dumps(
                {
                    "metric": verdict.get("metric"),
                    "column": verdict.get("column"),
                    "value": verdict.get("value"),
                    "ok": verdict.get("ok"),
                    "reasons": verdict.get("reasons"),
                    "checks": verdict.get("checks"),
                    "grains": [
                        verdict.get("contract", {}).get("numerator_grain"),
                        verdict.get("contract", {}).get("denominator_grain"),
                    ],
                },
                ensure_ascii=False,
            )
        )
    print(f"warnings  = {json.dumps(result_set.get('warnings'), ensure_ascii=False)[:600]}")
    print(f"slots     = {json.dumps(result_set.get('unsupported_slots'), ensure_ascii=False)[:500]}")
    print(f"disclosure= {result.get('disclosure')}")
    narrative = result.get("answer") or result.get("narrative") or ""
    print(f"narrative = {str(narrative)[:500]}")
    guarded = bool(result_set.get("warnings")) or any(
        not v.get("ok") for v in (result_set.get("ratio_validations") or [])
    )
    if "17957" in str(narrative) or "17,957" in str(narrative):
        print(
            ">> the ratio appears in the prose "
            + ("WITH the guard's caveat attached" if guarded else "!! UNGUARDED")
        )
    return result


def main() -> int:
    db = SessionLocal()
    try:
        kb = _kb(db, KB_NAME)
        print(f"KB   {kb.name}  ({kb.db_type})  id={kb.id}")
        kb_id = kb.id
    finally:
        db.close()

    _install_measured_executor()
    if "--ab" in sys.argv:
        _disable_ratio_check()
        _run(kb_id, "A: pre-change (no ratio check)")
        # re-import fresh copies of the two functions and run again
        import importlib

        from app.services.db import grounding

        importlib.reload(grounding)
        _install_measured_executor()
        _run(kb_id, "B: with ratio validation")
    else:
        _run(kb_id, "measured ≈17,957% case, with ratio validation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
