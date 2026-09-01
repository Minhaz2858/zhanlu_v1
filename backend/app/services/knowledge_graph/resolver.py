"""Product name → product_id resolver.

Maps user-language product names (Chinese, English, abbreviations, aliases)
to canonical `product_id` strings.  The alias table and display labels are
loaded from the per-app domain configuration (keys ``product_aliases`` /
``product_labels``); with no config present the resolver behaves generically
(empty catalog — lookups return None / [] and nothing ever crashes).
"""

from __future__ import annotations

import re

from app.services.domain_config import get_domain_config

# Per-app domain config name. The resolver is app-agnostic; "" resolves to
# the generic (empty) config unless an app name is configured at startup.
_DOMAIN_CONFIG_NAME = ""


def _load_alias_map() -> dict[str, list[str]]:
    """Load the product alias table from the app's domain config.

    Key ``product_aliases`` maps product_id → list of user-language aliases.
    Missing/invalid config → empty mapping (generic, never raises).
    """
    cfg = get_domain_config(_DOMAIN_CONFIG_NAME) or {}
    raw = cfg.get("product_aliases") or {}
    out: dict[str, list[str]] = {}
    for pid, aliases in raw.items():
        if isinstance(aliases, (list, tuple)):
            cleaned = [str(a) for a in aliases if a is not None and str(a).strip()]
            if cleaned:
                out[str(pid)] = cleaned
    return out


def _load_product_labels() -> dict[str, str]:
    """Load display labels from the app's domain config (key ``product_labels``)."""
    cfg = get_domain_config(_DOMAIN_CONFIG_NAME) or {}
    raw = cfg.get("product_labels") or {}
    return {str(k): str(v) for k, v in raw.items() if v is not None and str(v).strip()}


def _alias_map() -> dict[str, list[str]]:
    """Current alias mapping (re-read per call; config dict is lru-cached)."""
    return _load_alias_map()


def resolve_product_id(user_input: str) -> str | None:
    """Map a user-language product string to a canonical product ID.

    Returns the canonical product_id or None if no match.
    Case-insensitive.  Matches both exact and substring patterns.

    With no domain config loaded the catalog is empty and every input
    resolves to None (fully generic behavior).
    """
    normalized = user_input.strip().lower()
    if not normalized:
        return None

    alias_map = _alias_map()

    # Exact ID match first
    if normalized in alias_map:
        return normalized

    # Phase 1: Exact alias match — highest priority
    for pid, aliases in alias_map.items():
        for alias in aliases:
            if normalized == alias:
                return pid

    # Phase 2: Substring match (user input IS a substring of an alias)
    # Shortest alias wins to avoid overly greedy matches.
    best_id: str | None = None
    best_len = 999999
    for pid, aliases in alias_map.items():
        for alias in aliases:
            if normalized in alias and len(alias) < best_len:
                best_id = pid
                best_len = len(alias)

    if best_id:
        return best_id

    # Phase 3: Alias IS a substring of user input
    for pid, aliases in alias_map.items():
        for alias in aliases:
            if alias in normalized:
                # Prefer longer alias match within the input
                if len(alias) > best_len:
                    best_id = pid
                    best_len = len(alias)

    return best_id if best_len < 999999 else None


def extract_product_ids_in_text(text: str) -> list[str]:
    """Extract all recognized product IDs from free-form text.

    Uses longest-match-first to handle ambiguous aliases.
    """
    found: dict[str, int] = {}  # pid → match length
    normalized = text.lower()

    # Sort aliases by length descending — longest match wins
    all_aliases: list[tuple[str, str, int]] = []  # (pid, alias, len)
    for pid, aliases in _alias_map().items():
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
    """Return display label for a product_id for context injection.

    Falls back to the product_id itself when no label is configured.
    """
    return _load_product_labels().get(product_id, product_id)


def list_supported_product_ids() -> list[str]:
    """Return all canonical product IDs from the configured catalog."""
    return sorted(_alias_map().keys())


def get_alias_mapping() -> dict[str, list[str]]:
    """Return the full alias mapping dict (read-only)."""
    return dict(_alias_map())


# ── Listing query helpers ────────────────────────────────────────────────


def looks_like_sinopec_listing_query(text: str) -> bool:
    """Detect if the user query looks like a listing/auction-price request.

    Signal terms come from the app's domain config (key
    ``listing_query_signals``); defaults to generic listing/auction terms.
    Legacy name kept for compatibility.
    """
    cfg = get_domain_config(_DOMAIN_CONFIG_NAME) or {}
    signals = cfg.get("listing_query_signals") or ["挂拍", "牌价", "挂牌", "listing", "list price"]
    lower = text.lower()
    return any(str(s).lower() in lower for s in signals if s)


def default_sinopec_listing_product_ids() -> list[str]:
    """Return product IDs typically looked up for listing prices.

    Loaded from the app's domain config (key ``listing_product_ids``);
    empty config → empty list (generic).
    """
    cfg = get_domain_config(_DOMAIN_CONFIG_NAME) or {}
    return [str(p) for p in (cfg.get("listing_product_ids") or []) if p]
