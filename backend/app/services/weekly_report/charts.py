"""Weekly report charts — stub renderer.

Port of EDIA 5.1.2 backend/src/service/weekly_report_charts.py.
Full matplotlib-based chart rendering will be added when chart dependencies
are available in the zhanlu container.
"""

from __future__ import annotations


def render_chart_stub(product_id: str, numbers: dict, week_start: str, week_end: str) -> dict:
    """Return a chart description stub (no actual image rendering yet).

    Returns a dict with chart metadata suitable for the report markdown.
    """
    return {
        "product_id": product_id,
        "chart_type": "price_line_7d",
        "week_range": f"{week_start} - {week_end}",
        "current_price": numbers.get("current_price"),
        "change_7d_pct": numbers.get("change_7d_pct", 0),
        "rendered": False,  # matplotlib not yet available
        "description": (
            f"{product_id} 7-day price trend. "
            f"Current: {numbers.get('current_price') or 'N/A'} "
            f"({numbers.get('change_7d_pct', 0):+.1f}%)"
        ),
    }


def render_chart_from_snapshot_payload(payload: dict) -> bytes | None:
    """Stub — returns None until matplotlib is available in container."""
    return None
