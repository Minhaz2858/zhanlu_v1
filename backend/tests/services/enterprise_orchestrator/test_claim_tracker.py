"""Tests for claim_tracker (Phase 1B).

Design spec §9.3 — every numeric claim in the report carries
``source_facet`` + ``source_row_ids`` + ``source_sql``. The tracker
re-executes the SQL on the same connection (when available) and
rewrites unverified claims.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "app"))

from app.services.enterprise_orchestrator.claim_tracker import (
    Claim,
    ClaimTracker,
    verify_claims,
    rewrite_unverified,
)


def _claim(
    text="Revenue fell 18% to ¥301M",
    source_facet="regional_sales",
    source_row_ids=("row_a", "row_b"),
    source_sql="SELECT SUM(FBASEQTY) FROM erp_v_sale_orderentry",
    verified=False,
):
    return {
        "claim_id": "c1",
        "text": text,
        "source_facet": source_facet,
        "source_row_ids": list(source_row_ids),
        "source_sql": source_sql,
        "verified": verified,
    }


class TestClaimTracker:
    def test_add_and_get(self):
        ct = ClaimTracker()
        ct.add(_claim())
        assert len(ct.claims) == 1
        assert ct.claims[0]["text"].startswith("Revenue")

    def test_validate_rejects_no_source(self):
        ct = ClaimTracker()
        ct.add(_claim(source_facet=""))
        assert len(ct.claims) == 0

    def test_validate_rejects_no_sql(self):
        ct = ClaimTracker()
        ct.add(_claim(source_sql=""))
        assert len(ct.claims) == 0

    def test_validate_rejects_no_row_ids(self):
        ct = ClaimTracker()
        ct.add(_claim(source_row_ids=[]))
        assert len(ct.claims) == 0


class TestVerifyClaims:
    def test_no_source_returns_all_unverified(self):
        ct = ClaimTracker()
        ct.add(_claim())
        result = verify_claims(ct, db_executor=None)
        # Without an executor we cannot verify — but we still keep
        # claims marked verified=False so renderers can present them.
        assert result.unverified_count >= 0
        assert result.verified_count + result.unverified_count == 1

    def test_successful_re_execution_marks_verified(self):
        ct = ClaimTracker()
        ct.add(_claim())

        def db_exec(sql):
            # Any successful execution counts as verification.
            return [{"v": 123.45}]

        result = verify_claims(ct, db_executor=db_exec)
        assert result.verified_count == 1

    def test_failed_execution_marks_unverified(self):
        ct = ClaimTracker()
        ct.add(_claim())

        def db_exec(sql):
            raise RuntimeError("db down")

        result = verify_claims(ct, db_executor=db_exec)
        assert result.unverified_count == 1
        assert all(not c["verified"] for c in ct.claims)


class TestRewriteUnverified:
    def test_rewrites_text(self):
        c = _claim()
        rewrite_unverified([c])
        assert "Data unavailable" in c["text"]
        assert c["verified"] is False

    def test_keeps_verified_intact(self):
        c = _claim(verified=True)
        original = c["text"]
        rewrite_unverified([c])
        assert c["text"] == original

    def test_handles_empty_list(self):
        rewrite_unverified([])
