"""Ecisco BI Weekly Report — generate C5/C9 weekly market analysis.

Port of EDIA 5.1.2 backend/src/service/weekly_report_*.py
"""

from .generator import WeeklyReportService, generate_weekly_report
from .data import resolve_week_dates, collect_all_numbers
from .charts import render_chart_stub
from .parser import parse_report_to_sections
from .tool import register_weekly_report_tools

__all__ = [
    "WeeklyReportService",
    "generate_weekly_report",
    "resolve_week_dates",
    "collect_all_numbers",
    "render_chart_stub",
    "parse_report_to_sections",
    "register_weekly_report_tools",
]
