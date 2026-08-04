"""Weekly report parser — extract sections from raw markdown.

Port of EDIA 5.1.2 backend/src/service/weekly_report_parser.py.
"""

from __future__ import annotations

import re


def parse_report_to_sections(markdown: str) -> list[dict]:
    """Parse a completed weekly report markdown into structured sections.

    Returns list of {product_id, product_name_cn, narrative, numbers}
    """
    sections: list[dict] = []
    # Match ## Product Name (product_id) ... pattern
    pattern = re.compile(
        r"##\s+(?P<name>[^(]+)\s*\((?P<id>[^)]+)\)\s*\n"
        r"本周价格:\s*(?P<price>[^\n]+)\n\n"
        r"(?P<narrative>.*?)(?=\n##|\Z)",
        re.DOTALL,
    )
    for m in pattern.finditer(markdown):
        sections.append({
            "product_id": m.group("id").strip(),
            "product_name_cn": m.group("name").strip(),
            "price_line": m.group("price").strip(),
            "narrative": m.group("narrative").strip(),
        })
    return sections
