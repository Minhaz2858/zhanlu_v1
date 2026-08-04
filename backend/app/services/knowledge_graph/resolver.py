"""Product name → product_id resolver for the Ecisco BI domain.

Port of EDIA 5.1.2 backend/src/domain/product_resolver.py.

Maps user-language product names (Chinese, English, abbreviations, aliases)
to canonical `product_id` strings.  Supports all 12 C5/C9 products by default.

Uses a static mapping table with no external DB dependency.
"""

from __future__ import annotations

import re

# ── Alias mapping — user language → canonical product_id ──────────────────

_ALIAS_MAP: dict[str, list[str]] = {
    "crude_oil": ["原油", "brent", "dubai", "crude oil", "crude", "wti"],
    "naphtha": ["石脑油", "naphtha", "轻石脑油", "重石脑油", "naph", "nap"],
    "cracked_c5": ["裂解c5", "裂解碳五", "cracked c5", "裂解c5馏分", "c5裂解", "c5馏分", "c5"],
    "mixed_c5": ["混合碳五", "混合c5", "mixed c5", "混合碳5"],
    "isoprene": ["异戊二烯", "isoprene", "ip", "iso", "异戊"],
    "piperylene": ["间戊二烯", "piperylene", "pip", "pipe", "间戊"],
    "dcpd": ["双环戊二烯", "dcpd", "二聚环戊二烯", "dimer", "dcp"],
    "sis": ["sis", "苯乙烯异戊二烯苯乙烯", "sis橡胶", "sis弹性体", "苯乙烯-异戊二烯-苯乙烯"],
    "cracked_c9": ["裂解c9", "裂解碳九", "cracked c9", "c9馏分", "c9"],
    "styrene": ["苯乙烯", "styrene", "sm", "st"],
    "c5_resin": ["碳五石油树脂", "c5石油树脂", "c5树脂", "c5 resin", "碳五树脂", "石油树脂"],
    "raffinate_c5": ["抽余碳五", "抽余c5", "raffinate c5", "raffinate", "抽余油"],
    "blowing_agent": ["戊烷发泡剂", "发泡剂", "blowing agent", "pentane", "戊烷"],
}

# ── Product ID → display label ────────────────────────────────────────────

_PRODUCT_LABELS: dict[str, str] = {
    "crude_oil": "Crude Oil / 原油",
    "naphtha": "Naphtha / 石脑油",
    "cracked_c5": "Cracked C5 / 裂解C5",
    "mixed_c5": "Mixed C5 / 混合碳五",
    "isoprene": "Isoprene / 异戊二烯",
    "piperylene": "Piperylene / 间戊二烯",
    "dcpd": "DCPD / 双环戊二烯",
    "sis": "SIS / 苯乙烯-异戊二烯-苯乙烯",
    "cracked_c9": "Cracked C9 / 裂解C9",
    "styrene": "Styrene / 苯乙烯",
    "c5_resin": "C5 Petroleum Resin / 碳五石油树脂",
    "raffinate_c5": "Raffinate C5 / 抽余碳五",
    "blowing_agent": "Pentane Blowing Agent / 戊烷发泡剂",
}


def resolve_product_id(user_input: str) -> str | None:
    """Map a user-language product string to a canonical product ID.

    Returns the canonical product_id (e.g. "isoprene") or None if no match.
    Case-insensitive.  Matches both exact and substring patterns.

    Examples:
        resolve_product_id("异戊二烯")    → "isoprene"
        resolve_product_id("isoprene")   → "isoprene"
        resolve_product_id("IP")         → "isoprene"
        resolve_product_id("unknown")    → None
    """
    normalized = user_input.strip().lower()
    if not normalized:
        return None

    # Exact ID match first
    if normalized in _ALIAS_MAP:
        return normalized

    # Phase 1: Exact alias match — highest priority
    for pid, aliases in _ALIAS_MAP.items():
        for alias in aliases:
            if normalized == alias:
                return pid

    # Phase 2: Substring match (user input IS a substring of an alias)
    # Shortest alias wins to avoid overly greedy matches (e.g. "异戊二烯"
    # matching "苯乙烯-异戊二烯-苯乙烯" instead of the exact "异戊二烯" alias).
    best_id: str | None = None
    best_len = 999999
    for pid, aliases in _ALIAS_MAP.items():
        for alias in aliases:
            if normalized in alias and len(alias) < best_len:
                best_id = pid
                best_len = len(alias)

    if best_id:
        return best_id

    # Phase 3: Alias IS a substring of user input
    for pid, aliases in _ALIAS_MAP.items():
        for alias in aliases:
            if alias in normalized:
                # Prefer longer alias match within the input
                if len(alias) > best_len:
                    best_id = pid
                    best_len = len(alias)

    return best_id if best_len < 999999 else None


def extract_product_ids_in_text(text: str) -> list[str]:
    """Extract all recognized product IDs from free-form text.

    Uses longest-match-first to handle ambiguous cases like "c5" vs "c5_resin".
    """
    found: dict[str, int] = {}  # pid → match length
    normalized = text.lower()

    # Sort aliases by length descending — longest match wins
    all_aliases: list[tuple[str, str, int]] = []  # (pid, alias, len)
    for pid, aliases in _ALIAS_MAP.items():
        for alias in aliases:
            all_aliases.append((pid, alias, len(alias)))
    all_aliases.sort(key=lambda x: x[2], reverse=True)

    for pid, alias, _ in all_aliases:
        if pid in found:
            continue  # already matched with a longer alias
        # Match as whole word or bounded by non-alphanumeric
        pattern = re.compile(rf"(?:^|[^a-zA-Z\u4e00-\u9fff]){re.escape(alias)}(?:$|[^a-zA-Z\u4e00-\u9fff])")
        if pattern.search(normalized):
            found[pid] = len(alias)

    return sorted(found.keys())


def split_product_tokens(text: str) -> list[str]:
    """Split Chinese/English text into individual product name tokens.

    Returns deduplicated product IDs found.
    """
    # Common Chinese separators
    for sep in ["、", "，", ",", "和", "与", "及", "以及", "或", "or", "and", ";", "；"]:
        text = text.replace(sep, " ")

    parts = text.split()
    ids: list[str] = []
    seen: set[str] = set()

    # Try multi-token windows (2-4 tokens)
    for window in [4, 3, 2, 1]:
        for i in range(len(parts) - window + 1):
            chunk = " ".join(parts[i : i + window]).strip()
            pid = resolve_product_id(chunk)
            if pid and pid not in seen:
                seen.add(pid)
                ids.append(pid)

    # Also try the whole text as one chunk
    pid = resolve_product_id(text.strip())
    if pid and pid not in seen:
        ids.append(pid)

    return ids


def product_id_to_context_label(product_id: str) -> str:
    """Return display label for a product_id for context injection."""
    return _PRODUCT_LABELS.get(product_id, product_id)


def list_supported_product_ids() -> list[str]:
    """Return all 12 canonical product IDs."""
    return sorted(_ALIAS_MAP.keys())


def get_alias_mapping() -> dict[str, list[str]]:
    """Return the full alias mapping dict (read-only)."""
    return dict(_ALIAS_MAP)


# ── Sinopec listing query helpers ─────────────────────────────────────────


def looks_like_sinopec_listing_query(text: str) -> bool:
    """Detect if the user query looks like a Sinopec listing request.

    Examples: "挂拍价", "牌价", "listing price", "挂牌"
    """
    signals = ["挂拍", "牌价", "挂牌", "listing", "list price", "sinopec", "中石化"]
    lower = text.lower()
    return any(s in lower for s in signals)


def default_sinopec_listing_product_ids() -> list[str]:
    """Return product IDs typically looked up for Sinopec listing."""
    return [
        "isoprene", "piperylene", "dcpd", "cracked_c5",
        "cracked_c9", "c5_resin", "sis", "styrene",
        "naphtha", "blowing_agent",
    ]
