"""One-shot backfill: compute embeddings for existing agent memories.

Run after deploying the ``embedding`` column (migration 023) so that
pre-existing memories become semantically retrievable. Safe to re-run —
it only touches rows whose ``embedding`` is NULL.

Usage (from the backend/ directory, with DATABASE_URL set):

    PYTHONPATH=. python scripts/backfill_memory_embeddings.py                  # all agents
    PYTHONPATH=. python scripts/backfill_memory_embeddings.py --agent APP_ID   # one agent
    PYTHONPATH=. python scripts/backfill_memory_embeddings.py --limit 500      # cap rows

The script is intentionally synchronous and sequential: memory corpora are
small (hundreds to low thousands of rows) and correctness/observability
matters more than throughput here. Failures on individual rows are counted
but never abort the run.
"""

from __future__ import annotations

import argparse
import sys

from app.database import SessionLocal
from app.services.memory_advanced import backfill_embeddings


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill agent memory embeddings.")
    parser.add_argument(
        "--agent",
        default=None,
        help="Scope to a single agent_app_id (default: all agents).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of rows processed (default: no cap).",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        summary = backfill_embeddings(db, agent_app_id=args.agent, limit=args.limit)
    finally:
        db.close()

    print(
        f"Backfill complete: processed={summary['processed']} "
        f"embedded={summary['embedded']} failed={summary['failed']}"
    )
    # Non-zero exit only if nothing could be embedded despite pending rows.
    if summary["processed"] > 0 and summary["embedded"] == 0:
        print(
            "WARNING: 0 embeddings written. Check OPENAI_API_KEY / "
            "EMBEDDING_MODEL / that the provider exposes an /embeddings endpoint.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
