"""Prompt A/B golden-set runner (P2-8, IBM iterate-and-refine).

Runs the golden query set (``prompt_golden_set.json``) through two prompt
variants using ``prompt_ab_test.run_ab_test`` and persists the verdict to
the ``prompt_ab_tests`` table via ``persist_ab_result``.

Usage (inside the backend container):

    python -m scripts.run_prompt_golden_set \
        --variant-a-file /tmp/prompt_a.txt \
        --variant-b-file /tmp/prompt_b.txt \
        [--queries-file scripts/prompt_golden_set.json] [--limit 10]

Variants default to the CURRENT production baseline vs. itself (smoke
run) when no files are given — useful to verify the harness works.

The PROMPT_AB_TEST_ENABLED flag is not required for this script (it is a
manual/CI entry point, not the runtime path), but results are only
meaningful with a reachable LLM endpoint.

Exit code 0 on success; 2 when every query failed (endpoint down).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


def _read_prompt(path: str | None, fallback: str) -> str:
    if not path:
        return fallback
    return Path(path).read_text(encoding="utf-8")


async def _run(args: argparse.Namespace) -> int:
    from app.services.prompt_ab_test import persist_ab_result, run_ab_test
    from app.database import SessionLocal

    golden_path = Path(args.queries_file)
    data = json.loads(golden_path.read_text(encoding="utf-8"))
    queries = [str(q) for q in data.get("queries", [])]
    if args.limit:
        queries = queries[: args.limit]
    if not queries:
        print(f"No queries in {golden_path}")
        return 2

    baseline = (
        "You are a helpful assistant for an enterprise data platform. "
        "Answer with grounded, specific figures and cite your source."
    )
    prompt_a = _read_prompt(args.variant_a_file, baseline)
    prompt_b = _read_prompt(args.variant_b_file, baseline)

    print(f"Running {len(queries)} golden queries (A vs B)…")
    result = await asyncio.to_thread(run_ab_test, prompt_a, prompt_b, queries)

    print(
        f"winner={result.winner} confidence={result.confidence} "
        f"mean_a={result.mean_score_a} mean_b={result.mean_score_b} "
        f"(A:{result.wins_a} B:{result.wins_b} ties:{result.ties} "
        f"failed:{len(queries) - result.total_queries})"
    )
    for row in result.per_query_results:
        print(
            f"  [{row['winner']:>3}] a={row['score_a']:.1f} b={row['score_b']:.1f}  "
            f"{row['query'][:70]}"
        )

    db = SessionLocal()
    try:
        ok = persist_ab_result(db, result)
        print(f"persisted={ok}")
    finally:
        db.close()

    return 0 if result.total_queries > 0 else 2


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant-a-file", help="Text file with prompt variant A")
    parser.add_argument("--variant-b-file", help="Text file with prompt variant B")
    parser.add_argument(
        "--queries-file",
        default=str(Path(__file__).parent / "prompt_golden_set.json"),
    )
    parser.add_argument("--limit", type=int, default=0, help="Limit queries (smoke runs)")
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
