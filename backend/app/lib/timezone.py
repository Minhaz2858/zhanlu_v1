"""
Zhanlu timezone helpers — all user-visible timestamps are formatted in
Asia/Shanghai (UTC+8 / CST).  Internal DB storage stays UTC.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

CST = ZoneInfo("Asia/Shanghai")


def now_cst() -> datetime:
    """Current time in Asia/Shanghai."""
    return datetime.now(tz=CST)


def format_cst(dt: datetime) -> str:
    """Format a datetime as 'YYYY-MM-DD HH:MM' in Asia/Shanghai.

    Accepts both timezone-aware and naive UTC datetimes.
    """
    if dt.tzinfo is None:
        # Naive datetime — assume UTC
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(CST)
    return local.strftime("%Y-%m-%d %H:%M")
