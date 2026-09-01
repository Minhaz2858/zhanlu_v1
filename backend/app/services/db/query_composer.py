"""Data-Concepts-aware query composer.

Reads the Data Concepts catalog (business term → table.column mapping),
groups requested metrics by table, and generates consolidated SQL queries
that fetch multiple metrics in a single SELECT — eliminating the need
for separate ask_data_agent calls per metric.

This is the "C" optimization: "volume + revenue + margin" becomes one
SQL with 3 SUM() expressions instead of 3 separate sub-agent runs
(40-100s each → 1-5s total).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# Business-term → catalog concept matching
_METRIC_ALIASES: dict[str, list[str]] = {
    "volume": ["volume", "qty", "quantity", "数量", "销量", "出货量", "frealqty"],
    "revenue": ["revenue", "sales", "amount", "收入", "营收", "销售额", "fyksbghamount"],
    "margin": ["margin", "profit", "毛利", "利润", "fyksbghallamount"],
    "inventory": ["inventory", "stock", "库存", "存量", "fqty"],
    "cost": ["cost", "expense", "成本", "费用"],
}


@dataclass
class ComposedQuery:
    """A consolidated SQL query covering one or more metrics from one table."""

    label: str
    sql: str
    data_source_id: str
    metric_group: list[str] = field(default_factory=list)


@dataclass
class _ConceptEntry:
    """Parsed concept from the catalog."""

    name: str  # "Sales (销售)"
    tables: list[str]  # ["sales_orders", "order_lines"]
    measures: dict[str, str]  # {"Volume": "qty", "Revenue": "amount"}
    columns: list[str]  # all column names
    date_column: str | None = None


def _parse_concept_catalog(catalog_text: str) -> dict[str, _ConceptEntry]:
    """Parse the Data Concepts catalog text into structured entries.

    Returns dict keyed by lowercase concept name (e.g., "sales").
    """
    if not catalog_text or not catalog_text.strip():
        return {}

    entries: dict[str, _ConceptEntry] = {}
    current_entry: _ConceptEntry | None = None

    for line in catalog_text.splitlines():
        line = line.strip()
        if not line:
            continue

        # Match concept header: "- **Sales (销售)**: table1 + table2"
        header_match = re.match(
            r"-\s*\*\*(.+?)\*\*:\s*(.+)", line
        )
        if header_match:
            name = header_match.group(1).strip()
            tables_str = header_match.group(2).strip()
            tables = [t.strip() for t in re.split(r"\s*\+\s*", tables_str)]
            # Normalize name for lookup
            key = name.lower().split("(")[0].strip()
            current_entry = _ConceptEntry(
                name=name,
                tables=tables,
                measures={},
                columns=[],
            )
            entries[key] = current_entry
            continue

        if current_entry is None:
            continue

        # Match measures line: "Measures: Volume→FREALQTY, Revenue→FYKSBGHAMOUNT"
        measures_match = re.match(r"Measures:\s*(.+)", line, re.IGNORECASE)
        if measures_match:
            for pair in re.split(r",\s*", measures_match.group(1)):
                arrow_match = re.match(r"(.+?)→(.+)", pair.strip())
                if arrow_match:
                    current_entry.measures[arrow_match.group(1).strip()] = arrow_match.group(2).strip()
            continue

        # Match columns line: "Columns: FREALQTY, FYKSBGHAMOUNT, ..."
        columns_match = re.match(r"Columns:\s*(.+)", line, re.IGNORECASE)
        if columns_match:
            cols = [c.strip() for c in re.split(r",\s*", columns_match.group(1))]
            current_entry.columns.extend(cols)
            continue

        # Match date column line: "Date column: FDATE"
        date_match = re.match(r"Date\s*column:\s*(.+)", line, re.IGNORECASE)
        if date_match:
            current_entry.date_column = date_match.group(1).strip()
            continue

    return entries


def _map_metrics_to_tables(
    metrics: list[str],
    catalog: dict[str, _ConceptEntry],
) -> dict[str, list[tuple[str, str, str]]]:
    """Map each requested metric to the best concept entry and column.

    Returns dict keyed by concept key → list of (metric_name, column_name, table_name).
    """
    if not catalog:
        return {}

    groups: dict[str, list[tuple[str, str, str]]] = {}

    for metric in metrics:
        metric_lower = metric.lower().strip()
        matched = False

        # Try to find the metric in each concept entry's measures
        for key, entry in catalog.items():
            for measure_name, col_name in entry.measures.items():
                # Check if the metric matches the measure name or any alias
                measure_lower = measure_name.lower()
                col_lower = col_name.lower()
                if (
                    metric_lower == measure_lower
                    or metric_lower == col_lower
                    or any(alias == measure_lower or alias == col_lower
                           for alias in _METRIC_ALIASES.get(metric_lower, []))
                ):
                    table = entry.tables[0] if entry.tables else ""
                    groups.setdefault(key, []).append((metric, col_name, table))
                    matched = True
                    break

            if matched:
                break

        if not matched:
            logger.debug(
                "query_composer: metric '%s' not found in any concept entry",
                metric,
            )

    return groups


def _period_to_sql_filter(period: str, date_column: str) -> str:
    """Convert a period string like '2026-07' or 'July 2026' to a SQL WHERE clause."""
    if not period or not date_column:
        return ""

    # Normalize: "July 2026" → "2026-07"
    month_map = {
        "january": "01", "february": "02", "march": "03", "april": "04",
        "may": "05", "june": "06", "july": "07", "august": "08",
        "september": "09", "october": "10", "november": "11", "december": "12",
        "一月": "01", "二月": "02", "三月": "03", "四月": "04",
        "五月": "05", "六月": "06", "七月": "07", "八月": "08",
        "九月": "09", "十月": "10", "十一月": "11", "十二月": "12",
    }

    normalized = period.strip()

    # Try "YYYY-MM" format
    ym_match = re.match(r"(\d{4})-(\d{2})", normalized)
    if ym_match:
        year, month = ym_match.group(1), ym_match.group(2)
        next_month = int(month) + 1
        next_year = int(year)
        if next_month > 12:
            next_month = 1
            next_year += 1
        return (
            f"{date_column} >= '{year}-{month}-01' AND "
            f"{date_column} < '{next_year:04d}-{next_month:02d}-01'"
        )

    # Try "Month YYYY" format (e.g. "July 2026")
    month_year_match = re.match(r"(\w+)\s+(\d{4})", normalized)
    if month_year_match:
        month_name = month_year_match.group(1).lower()
        year = month_year_match.group(2)
        month = month_map.get(month_name)
        if month:
            next_month_num = int(month) + 1
            next_year = int(year)
            if next_month_num > 12:
                next_month_num = 1
                next_year += 1
            return (
                f"{date_column} >= '{year}-{month}-01' AND "
                f"{date_column} < '{next_year:04d}-{next_month_num:02d}-01'"
            )

    # Fallback: just use the raw string as a LIKE filter
    return f"{date_column} LIKE '{normalized}%'"


def compose_queries(
    metrics: list[str],
    kb_id: str,
    filters: dict[str, str] | None = None,
    concept_catalog: str = "",
) -> list[ComposedQuery]:
    """Compose consolidated SQL queries from requested metrics and Data Concepts.

    Groups metrics by table, generates one SQL per table with all metrics
    as aggregated columns, and applies the date filter from ``filters``.

    Returns [] when the catalog doesn't cover any of the requested metrics
    (caller should fall back to ask_data_agent).
    """
    if not concept_catalog or not metrics:
        return []

    catalog = _parse_concept_catalog(concept_catalog)
    if not catalog:
        return []

    groups = _map_metrics_to_tables(metrics, catalog)
    if not groups:
        return []

    period = (filters or {}).get("period", "")
    queries: list[ComposedQuery] = []

    for concept_key, metric_tuples in groups.items():
        entry = catalog.get(concept_key)
        if not entry or not entry.tables:
            continue

        table = entry.tables[0]  # primary table
        date_col = entry.date_column

        # Build SELECT expressions: SUM(col) AS metric_name
        select_parts: list[str] = []
        metric_names: list[str] = []
        for metric_name, col_name, _ in metric_tuples:
            safe_alias = re.sub(r"[^a-zA-Z0-9_]", "_", metric_name.lower())
            select_parts.append(f"SUM({col_name}) AS {safe_alias}")
            metric_names.append(metric_name)

        if not select_parts:
            continue

        # Build WHERE clause
        where_parts: list[str] = []
        if date_col and period:
            date_filter = _period_to_sql_filter(period, date_col)
            if date_filter:
                where_parts.append(date_filter)

        where_clause = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""

        sql = f"SELECT {', '.join(select_parts)} FROM {table}{where_clause}"

        label = re.sub(r"[^a-zA-Z0-9]", "_", concept_key).lower()[:40] + "_metrics"

        queries.append(ComposedQuery(
            label=label,
            sql=sql,
            data_source_id=kb_id,
            metric_group=metric_names,
        ))

    return queries
