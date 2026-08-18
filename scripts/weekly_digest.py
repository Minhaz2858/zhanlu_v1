#!/usr/bin/env python3
"""Phase 5 — Weekly deck usage digest.

Queries the ``artifact_events`` table for the trailing ``--days`` window
(default 7) and aggregates deck lifecycle activity: counts per event type,
distinct decks touched, and a per-day breakdown.  Intended to be wired into a
cron / scheduled task once Phase 5 has accumulated real data.

Privacy: only structural metadata is read (event_type / counts / timestamps).
No slide content is touched.

Usage:
    docker exec zhanlu-backend bash -c "cd /app && python scripts/weekly_digest.py --days 7"
    docker exec zhanlu-backend bash -c "cd /app && python scripts/weekly_digest.py --json"
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make the backend importable when run as a bare script inside the container.
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from sqlalchemy import func  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models.artifact_event import ArtifactEvent  # noqa: E402


def build_digest(days: int = 7) -> dict:
    """Aggregate artifact_events for the trailing ``days`` window."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    db = SessionLocal()
    try:
        q = db.query(ArtifactEvent).filter(ArtifactEvent.created_date >= since)
        rows = q.all()

        by_type: dict[str, int] = {}
        distinct_decks: set[str] = set()
        edited_kinds: dict[str, int] = {}
        per_day: dict[str, int] = {}
        for ev in rows:
            by_type[ev.event_type] = by_type.get(ev.event_type, 0) + 1
            if ev.artifact_id:
                distinct_decks.add(ev.artifact_id)
            day = ev.created_date.date().isoformat() if ev.created_date else "unknown"
            per_day[day] = per_day.get(day, 0) + 1
            if ev.event_type == "deck_edited" and ev.metadata_json:
                try:
                    import json as _json
                    kind = _json.loads(ev.metadata_json).get("edit_kind", "unknown")
                    edited_kinds[kind] = edited_kinds.get(kind, 0) + 1
                except Exception:
                    pass

        return {
            "window_days": days,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "since": since.isoformat(),
            "total_events": len(rows),
            "distinct_decks": len(distinct_decks),
            "by_event_type": by_type,
            "by_day": dict(sorted(per_day.items())),
            "edit_kinds": edited_kinds,
        }
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Weekly deck usage digest.")
    p.add_argument("--days", type=int, default=7, help="Trailing window in days.")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    args = p.parse_args(argv)

    digest = build_digest(args.days)
    if args.json:
        print(json.dumps(digest, indent=2, ensure_ascii=False))
    else:
        print("=" * 56)
        print(f"  Deck Usage Digest — last {digest['window_days']} days")
        print("=" * 56)
        print(f"  Total events : {digest['total_events']}")
        print(f"  Distinct decks: {digest['distinct_decks']}")
        print("  By type:")
        for et, c in digest["by_event_type"].items():
            print(f"    - {et:<18} {c}")
        if digest["edit_kinds"]:
            print("  Edit kinds:")
            for k, c in digest["edit_kinds"].items():
                print(f"    - {k:<18} {c}")
        print("  By day:")
        for d, c in digest["by_day"].items():
            print(f"    - {d}: {c}")
        print("=" * 56)
    return 0


if __name__ == "__main__":
    sys.exit(main())
