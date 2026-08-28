"""Task A2: deck_router defaults to the HTML design renderer ('sandbox').

Previously route_deck returned 'structured' for any plain request unless a
deck_type of investor_deck/marketing or a design keyword was present.  With
PPT_DESIGN_BY_DEFAULT=True the default flips: every pptx request routes to
'sandbox' (HTML design renderer) unless the user explicitly asked for a plain
data dump / simple text output.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from app.services.artifacts.deck_router import route_deck


def test_plain_request_defaults_to_sandbox():
    """A plain ppt request without design keywords now routes to sandbox."""
    assert route_deck(None, "make a market view ppt") == "sandbox"


def test_plain_data_dump_goes_structured():
    """Explicit plain / data-dump intent still routes to structured."""
    assert route_deck(None, "just a plain data dump of the numbers") == "structured"


def test_plain_chinese_data_table_goes_structured():
    """Explicit plain Chinese table intent routes to structured."""
    assert route_deck(None, "简单纯文本数据表") == "structured"


def test_design_keywords_still_route_to_sandbox():
    """Existing sandbox keyword signals keep working."""
    assert route_deck(None, "beautiful investor deck") == "sandbox"
