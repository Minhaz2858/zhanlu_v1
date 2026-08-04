"""Weekly report data layer — date resolution + number collection.

Port of EDIA 5.1.2 backend/src/service/weekly_report_data.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def resolve_week_dates(report_week: str | None = None) -> tuple[str, str, str]:
    """Resolve report week → (start, end, label).

    Accepted formats: "2026-W31", "2026-08-03", None (current week).
    Returns (YYYY-MM-DD start, YYYY-MM-DD end, "2026-W31" label)
    """
    now = datetime.now(timezone.utc)

    if report_week and report_week.startswith("20") and "-W" in report_week:
        # ISO week format: "2026-W31"
        from datetime import date
        year_str, week_str = report_week.split("-W")
        year = int(year_str)
        week = int(week_str)
        # Monday of ISO week
        jan4 = date(year, 1, 4)
        first_monday = jan4 - timedelta(days=jan4.weekday())
        week_start = first_monday + timedelta(weeks=week - 1)
    elif report_week and len(report_week) >= 10:
        # Date format: "2026-08-03"
        week_start_dt = datetime.fromisoformat(report_week.split("T")[0])
        # Align to Monday
        days_since_monday = week_start_dt.weekday()
        week_start = (week_start_dt - timedelta(days=days_since_monday)).date()
    else:
        # Current week → last Monday
        days_since_monday = now.weekday()
        week_start = (now - timedelta(days=days_since_monday)).date()

    # End = Sunday
    week_end = week_start + timedelta(days=6)
    # Label
    iso_year, iso_week, _ = week_start.isocalendar()
    week_label = f"{iso_year}-W{iso_week:02d}"

    return (
        week_start.isoformat(),
        week_end.isoformat(),
        week_label,
    )


def collect_all_numbers(product_id: str, db=None) -> dict:
    """Collect all quantitative numbers for a product section.

    Returns dict with keys: current_price, change_7d_pct, change_30d_pct,
    forecast_band (high/base/low), etc. All numbers are computed, never LLM-generated.
    """
    numbers: dict[str, float | None] = {
        "current_price": None, "change_7d_pct": 0.0, "change_30d_pct": 0.0,
        "forecast_band": None,
    }
    if db:
        try:
            from app.services.market_dashboard.service import MarketDashboardService
            svc = MarketDashboardService(db)
            snap = svc.build_snapshot()
            if product_id in snap:
                entry = snap[product_id]
                numbers["current_price"] = entry.get("latest_price")
                numbers["change_7d_pct"] = entry.get("change_7d_pct", 0)
        except Exception:
            pass
    return numbers


def build_prerendered_ctx(product_id: str, db=None) -> dict:
    """Deprecated alias for compatibility. Use collect_all_numbers instead."""
    return collect_all_numbers(product_id, db)
