"""Task A4: market/industry intent resolves to the business theme preset.

``select_theme()`` maps ``user_message`` keywords to a ``ThemePreset`` via the
``_KEYWORD_OVERRIDES`` table (first match wins).  Market / industry / market-data
intent — in English and Chinese — should resolve to the catalog's business
preset (``swiss_modern``, the strict-grid corporate look) instead of falling
through to the ``data_report`` deck-type default (``electric_studio``).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from app.services.artifacts.themes import select_theme
from app.services.synexia.contracts import DeckPlan

# The catalog's business/industry preset (12-preset catalog, no literal
# "business" name — swiss_modern is the corporate register: Helvetica,
# strict grid, red accent blocks; "report" already maps there).
BUSINESS_PRESET = "swiss_modern"


def _plan():
    return DeckPlan(title="C5/C9 Market View", slides=[])


def test_market_intent_resolves_to_business_preset():
    theme = select_theme(
        _plan(), "make a c5 c9 market view ppt using market data"
    )
    assert theme.name == BUSINESS_PRESET


def test_industry_intent_resolves_to_business_preset():
    theme = select_theme(_plan(), "industry overview deck for the c5 c9 vertical")
    assert theme.name == BUSINESS_PRESET


def test_chinese_market_intent_resolves_to_business_preset():
    theme = select_theme(_plan(), "c5 c9 市场行情")
    assert theme.name == BUSINESS_PRESET


def test_chinese_industry_intent_resolves_to_business_preset():
    theme = select_theme(_plan(), "c5 c9 行业分析")
    assert theme.name == BUSINESS_PRESET
