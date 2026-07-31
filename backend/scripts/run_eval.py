"""Run the eval harness against a golden dataset — a CI quality gate.

Loads a golden dataset (JSON array of case specs), runs each case through
the SynexiaFSM, scores the resulting trace, prints a per-case + aggregate
report, and exits non-zero when the pass rate falls below a threshold.

Usage (from the backend/ directory, with DATABASE_URL + LLM configured):

    PYTHONPATH=. python scripts/run_eval.py \\
        --dataset scripts/golden_dataset.example.json \\
        --min-pass-rate 0.8

The script uses the live FSM executor (``run_case_via_fsm``). For CI
replay without LLM cost, substitute a recorded-trace executor (see
``app.services.synexia.eval_harness`` — the scorer consumes ``EvalTrace``,
which can be built from a stored Execution/ObservationRecord audit trail).
"""

from __future__ import annotations

import argparse
import json
import sys

from app.database import SessionLocal
from app.services.synexia.eval_harness import (
    EvalRunner,
    load_cases_from_dicts,
    run_case_via_fsm,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the agent eval harness (CI gate).")
    parser.add_argument(
        "--dataset", required=True,
        help="Path to a JSON array of golden case specs.",
    )
    parser.add_argument(
        "--min-pass-rate", type=float, default=0.8,
        help="Minimum pass rate for CI success (default: 0.8).",
    )
    args = parser.parse_args()

    try:
        with open(args.dataset, encoding="utf-8") as fh:
            specs = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        print(f"ERROR loading dataset {args.dataset}: {e}", file=sys.stderr)
        return 2

    cases = load_cases_from_dicts(specs if isinstance(specs, list) else [])
    if not cases:
        print(f"ERROR: no cases loaded from {args.dataset}", file=sys.stderr)
        return 2

    db = SessionLocal()
    try:
        report = EvalRunner(cases, lambda c: run_case_via_fsm(db, c)).run_all()
    finally:
        db.close()

    print(
        f"Eval: {report.passed_count}/{report.total} passed "
        f"(pass_rate={report.pass_rate:.1%}, mean_score={report.mean_score:.3f}, "
        f"mean_confidence={report.mean_confidence:.3f})"
    )
    for r in report.results:
        status = "PASS" if r.passed else "FAIL"
        extra = f"  err={r.error}" if r.error else ""
        sig = ",".join(f"{k}={'Y' if v else 'N'}" for k, v in (r.signals or {}).items())
        print(f"  [{status}] {r.case_id}  score={r.score:.2f}  conf={r.confidence:.2f}  [{sig}]{extra}")

    if report.pass_rate < args.min_pass_rate:
        print(
            f"\nCI GATE FAILED: pass_rate {report.pass_rate:.1%} "
            f"< min {args.min_pass_rate:.1%}", file=sys.stderr,
        )
        return 1
    print(f"\nCI GATE PASSED: pass_rate {report.pass_rate:.1%} >= min {args.min_pass_rate:.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
