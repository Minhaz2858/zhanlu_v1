"""P4 T5 — deterministic quote verification.

A candidate quote becomes evidence ONLY if the exact (whitespace-
normalized) span exists in the referenced source text. The ORIGINAL
verbatim span is always what gets stored; normalization is for matching
only. Untraceable quotes NEVER produce verified evidence.
"""

from __future__ import annotations

import pytest

from app.services.ontology.quote_verifier import (
    normalize_ws,
    verify_quote,
)


SOURCE = (
    "Section 2 — Definitions.\n"
    "Customer   means\nany entity  that buys goods from the company.\n"
    "Refunds are approved by the finance department only."
)


def test_exact_span_verifies():
    ok, span = verify_quote(SOURCE,
                            "Customer   means\nany entity  that buys goods")
    assert ok is True
    # the ORIGINAL verbatim span (with source whitespace) is returned
    assert span == "Customer   means\nany entity  that buys goods"


def test_whitespace_variant_of_verbatim_verifies():
    """The LLM may collapse whitespace; matching is normalized but the
    stored span is the source's original characters."""
    ok, span = verify_quote(SOURCE, "Customer means any entity that buys")
    assert ok is True
    assert span == "Customer   means\nany entity  that buys"


def test_untraceable_quote_never_verifies():
    ok, span = verify_quote(SOURCE, "Customers are always right")
    assert ok is False
    assert span is None


def test_paraphrase_never_verifies():
    ok, span = verify_quote(SOURCE, "a customer purchases goods")
    assert ok is False
    assert span is None


def test_empty_quote_never_verifies():
    assert verify_quote(SOURCE, "") == (False, None)
    assert verify_quote(SOURCE, "   \n  ") == (False, None)


def test_empty_source_never_verifies():
    assert verify_quote("", "Customer means") == (False, None)


def test_huge_quote_rejected():
    """A 'quote' longer than the source is untraceable by definition."""
    ok, span = verify_quote(SOURCE, SOURCE + " and more " * 10)
    assert ok is False


def test_quote_across_line_boundary_verifies():
    ok, span = verify_quote(
        SOURCE, "that buys goods from the company. Refunds are approved")
    assert ok is True
    assert "buys goods from the company." in span
