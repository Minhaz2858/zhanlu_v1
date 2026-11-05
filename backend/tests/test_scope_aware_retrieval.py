"""Stage 2B-2 — confidence-aware retrieval + scope-aware rerouting.

The eight acceptance criteria from the 2B-2 authorization (2026-10-30),
all against synthetic schemas (zero customer-specific names):

1. Human override beats inferred classification.
2. Low-confidence inference cannot dominate retrieval.
3. Company-wide query cannot silently select a regional partition.
4. Explicit regional query selects the matching partition.
5. Date-coverage rerouting respects scope.
6. Missing scope evidence preserves existing behavior.
7. No compatible substitute produces an honest degradation.
8. Dynamic synthetic databases work without customer-specific names.

Guiding principle under test: retrieval finds the most relevant data, but
scope enforcement prevents the most relevant WRONG dataset from becoming
the answer.
"""

from __future__ import annotations

from datetime import date

from app.services.db.table_retrieval import (
    RetrievalResult,
    TableCandidate,
    classification_adjustments,
    reroute_uncovered,
    retrieve_for_question,
    scope_disclosure_sentence,
)


def _toc_entry(name, max_date="2026-10-05", min_date="2026-01-01"):
    return {
        "table_name": name,
        "description_en": f"{name} records",
        "min_date": min_date,
        "max_date": max_date,
        "row_count": 100,
        "columns": [],
    }


def _res(*candidates):
    return RetrievalResult(
        candidates=[
            TableCandidate(
                table_name=n, score=s, covers_window=cw,
            )
            for n, s, cw in candidates
        ]
    )


_WINDOW = (date(2026, 10, 1), date(2026, 10, 7))
_COVERED = "2026-10-05"   # reaches the window
_STALE = "2026-09-30"     # ends before the window starts


def _facts(name, *, ds=None, ds_source=None, ds_conf=None, pv=None,
           sclass=None, sc_source=None, sc_conf=None):
    return {name: {
        "dataset_scope": ds,
        "dataset_scope_source": ds_source,
        "dataset_scope_confidence": ds_conf,
        "partition_value": pv,
        "schema_class": sclass,
        "schema_class_source": sc_source,
        "schema_class_confidence": sc_conf,
    }}


# ── 1 & 2: confidence-aware ranking ───────────────────────────────────────


class TestConfidenceAwareRanking:
    def test_human_override_beats_inferred_classification(self):
        """Criterion 1: identical evidence; the human 'global' verdict must
        outrank a 0.9-confidence inferred 'global'."""
        toc = [_toc_entry("alpha"), _toc_entry("beta")]
        facts = {}
        facts.update(_facts(
            "alpha", ds="global", ds_source="human", ds_conf=None,
        ))
        facts.update(_facts(
            "beta", ds="global", ds_source="inferred", ds_conf=0.9,
        ))
        res = retrieve_for_question(
            None, ["kb"], "records", toc, enabled=False,
            classification_facts=facts,
        )
        ranks = {c.table_name: i for i, c in enumerate(res.candidates)}
        assert ranks["alpha"] < ranks["beta"], (
            "human override must outrank a 0.9-confidence inference"
        )

    def test_low_confidence_inference_cannot_dominate(self):
        """Criterion 2: at 0.3 confidence even the largest partition boost
        stays below the core evidence weights — an inference can refine,
        never dominate."""
        from app.services.db import table_retrieval as tr

        low = tr._W_SCOPE_PARTITION_MATCH * 0.3
        assert low < tr._W_PLAN, "an inference must not outrank a verified plan"

    def test_inferred_low_confidence_is_bounded_refinement(self):
        """A 0.3-confidence matching partition gets exactly 0.6 — bounded."""
        adj = classification_adjustments(
            {
                "match_low": {
                    "dataset_scope": "partition:region",
                    "dataset_scope_source": "inferred",
                    "dataset_scope_confidence": 0.3,
                    "partition_value": "north",
                },
            },
            "north orders report",
        )
        assert adj["match_low"][0] == 0.6  # 2.0 × 0.3

    def test_system_penalty_scales_with_confidence(self):
        high = classification_adjustments(
            _facts("plat", sclass="system", sc_source="inferred", sc_conf=0.9),
            "anything",
        )["plat"][0]
        low = classification_adjustments(
            _facts("plat", sclass="system", sc_source="inferred", sc_conf=0.3),
            "anything",
        )["plat"][0]
        assert high < low < 0     # both penalties, the confident one harder
        assert low > -2.0         # a 0.3 guess buries less than a text hit

    def test_unknown_classification_imposes_nothing(self):
        assert classification_adjustments(_facts("mystery"), "records") == {}


# ── 3-7: scope-aware rerouting ─────────────────────────────────────────────


class TestScopeAwareReroute:
    def _scenario(self, spare_name, spare_scope, spare_pv=None):
        """Convected 'stale_tbl' selected; one covering spare."""
        toc = [
            _toc_entry("stale_tbl", max_date=_STALE),
            _toc_entry(spare_name, max_date=_COVERED),
        ]
        res = _res(("stale_tbl", 1.0, False), (spare_name, 0.9, True))
        facts = _facts(
            spare_name, ds=spare_scope, ds_source="human", ds_conf=None,
            pv=spare_pv,
        )
        return toc, res, facts

    def test_companywide_question_never_swaps_to_partition(self):
        """Criterion 3: the regional partition covers the window better —
        it must still NOT become the substitute for a company-wide query."""
        toc, res, facts = self._scenario(
            "region_north_data", "partition:region", spare_pv="north",
        )
        selected, swaps = reroute_uncovered(
            ["stale_tbl"], toc, res, _WINDOW,
            classification_facts=facts,
            question_scope=None,  # universal: no partition value asked
        )
        assert selected == ["stale_tbl"]
        assert swaps == []

    def test_keyless_family_also_incompatible_for_companywide(self):
        toc, res, facts = self._scenario(
            "region_north_data", "partition_family",
        )
        selected, swaps = reroute_uncovered(
            ["stale_tbl"], toc, res, _WINDOW,
            classification_facts=facts,
        )
        assert swaps == []
        assert selected == ["stale_tbl"]

    def test_explicit_regional_question_selects_matching_partition(self):
        """Criterion 4: the question NAMED the partition value."""
        toc, res, facts = self._scenario(
            "region_north_data", "partition:region", spare_pv="north",
        )
        selected, swaps = reroute_uncovered(
            ["stale_tbl"], toc, res, _WINDOW,
            classification_facts=facts,
            question_scope={"value": "north"},
        )
        assert swaps == [("stale_tbl", "region_north_data")]
        assert selected == ["region_north_data"]

    def test_regional_question_rejects_different_partition(self):
        toc, res, facts = self._scenario(
            "region_south_data", "partition:region", spare_pv="south",
        )
        selected, swaps = reroute_uncovered(
            ["stale_tbl"], toc, res, _WINDOW,
            classification_facts=facts,
            question_scope={"value": "north"},
        )
        assert swaps == []
        assert selected == ["stale_tbl"]

    def test_date_coverage_reroute_respects_scope_picks_global(self):
        """Criterion 5: with a partition AND a global spare, the universal
        question gets the global one — coverage alone doesn't decide."""
        toc = [
            _toc_entry("stale_tbl", max_date=_STALE),
            _toc_entry("region_north_data", max_date=_COVERED),
            _toc_entry("all_market_rollup", max_date=_COVERED),
        ]
        res = _res(
            ("stale_tbl", 1.0, False),
            ("region_north_data", 0.9, True),
            ("all_market_rollup", 0.8, True),
        )
        facts = {
            "region_north_data": {
                "dataset_scope": "partition:region",
                "dataset_scope_source": "human",
                "dataset_scope_confidence": 1.0,
                "partition_value": "north",
            },
            "all_market_rollup": {
                "dataset_scope": "global",
                "dataset_scope_source": "human",
                "dataset_scope_confidence": 1.0,
            },
        }
        selected, swaps = reroute_uncovered(
            ["stale_tbl"], toc, res, _WINDOW,
            classification_facts=facts,
            question_scope=None,
        )
        assert swaps == [("stale_tbl", "all_market_rollup")]

    def test_missing_scope_evidence_preserves_existing_behavior(self):
        """Criterion 6: facts=None → identical to the legacy call."""
        toc = [
            _toc_entry("stale_tbl", max_date=_STALE),
            _toc_entry("anything_at_all", max_date=_COVERED),
        ]
        res = _res(("stale_tbl", 1.0, False), ("anything_at_all", 0.9, True))
        legacy = reroute_uncovered(["stale_tbl"], toc, res, _WINDOW)
        extended = reroute_uncovered(
            ["stale_tbl"], toc, res, _WINDOW, classification_facts=None,
        )
        assert legacy == extended == (
            ["anything_at_all"], [("stale_tbl", "anything_at_all")]
        )

    def test_no_compatible_substitute_is_honest_degradation(self):
        """Criterion 7: only a partition covers the window; a company-wide
        question keeps the original selection and RECORDS the refusal."""
        toc, res, facts = self._scenario(
            "region_north_data", "partition:region", spare_pv="north",
        )
        selected, swaps = reroute_uncovered(
            ["stale_tbl"], toc, res, _WINDOW,
            classification_facts=facts,
        )
        assert selected == ["stale_tbl"]
        assert swaps == []
        assert any("scope-compatible" in n for n in res.notes), (
            "the honest degradation must be recorded, not silent"
        )

    def test_global_spare_always_compatible(self):
        toc, res, facts = self._scenario("all_market_rollup", "global")
        selected, swaps = reroute_uncovered(
            ["stale_tbl"], toc, res, _WINDOW,
            classification_facts=facts,
        )
        assert swaps == [("stale_tbl", "all_market_rollup")]


# ── 8: facts loader on a synthetic database ───────────────────────────────


class TestFactsLoaderSynthetic:
    def test_loader_reads_columns_and_overlay_wins(self):
        import os
        import uuid as _uuid

        os.environ.setdefault(
            "DATABASE_URL", "sqlite:///file::memory:?cache=shared&uri=true"
        )
        from app.database import Base, SessionLocal, engine
        import app.models  # noqa: F401

        Base.metadata.create_all(engine)

        from app.models.knowledge_base import KnowledgeBase
        from app.models.knowledge_catalog import (
            KBTableMeta,
            ProjectCatalogOverlay,
        )
        from app.services.db.table_retrieval import (
            _load_classification_facts,
        )

        kb_id = f"kb-2b2-{_uuid.uuid4().hex[:8]}"
        db = SessionLocal()
        try:
            db.add(KnowledgeBase(
                id=kb_id, name="2b2", source_kind="database", db_type="mysql",
            ))
            db.commit()
            db.add(KBTableMeta(
                id=str(_uuid.uuid4()), kb_id=kb_id, schema_name="",
                table_name="region_north_data", table_type="TABLE",
                schema_class="business", schema_class_confidence=0.7,
                dataset_scope="partition:region",
                dataset_scope_confidence=0.6,
                dataset_scope_signals_json={"partition_value": "north"},
            ))
            db.add(ProjectCatalogOverlay(
                id=str(_uuid.uuid4()), project_id=f"proj-{kb_id}",
                kb_id=kb_id, table_name="region_north_data",
                dataset_scope_override="global",
            ))
            db.commit()

            facts = _load_classification_facts(
                db, [kb_id], project_id=f"proj-{kb_id}"
            )
            f = facts["region_north_data"]
            assert f["dataset_scope"] == "global"
            assert f["dataset_scope_source"] == "human"
            assert f["dataset_scope_confidence"] == 1.0
            assert f["schema_class"] == "business"
            assert f["schema_class_source"] == "inferred"
            assert f["schema_class_confidence"] == 0.7
            assert f["partition_value"] == "north"
        finally:
            db.close()

    def test_question_scope_resolves_from_observed_values_only(self):
        from app.services.db.schema_classification import (
            resolve_question_scope_from_facts,
        )

        facts = _facts("region_north_data", pv="north", ds="partition:region")
        match = resolve_question_scope_from_facts(
            "show me the north orders", facts,
        )
        assert match is not None and match["value"] == "north"
        assert resolve_question_scope_from_facts(
            "total across the whole company", facts,
        ) is None


# ── concern 3 (2026-10-30 review): refusals reach the reader ───────────────


class TestRefusalVisibility:
    """A scope refusal recorded only in a log is invisible to the person
    reading the answer. The refusal must be a reader-usable sentence the
    caller can surface in the disclosure channel."""

    def test_reroute_refusal_populates_scope_refusals(self):
        toc, res, facts = TestScopeAwareReroute()._scenario(
            "region_north_data", "partition:region", spare_pv="north",
        )
        reroute_uncovered(
            ["stale_tbl"], toc, res, _WINDOW,
            classification_facts=facts,
        )
        assert res.scope_refusals, "the refusal must be captured structurally"
        assert any("stale_tbl" in r for r in res.scope_refusals)
        assert all("partition" in r for r in res.scope_refusals), (
            "the sentence must be readable by the user, not audit jargon"
        )

    def test_refusals_survive_serialisation(self):
        toc, res, facts = TestScopeAwareReroute()._scenario(
            "region_north_data", "partition:region", spare_pv="north",
        )
        reroute_uncovered(
            ["stale_tbl"], toc, res, _WINDOW, classification_facts=facts,
        )
        assert res.as_dict()["scope_refusals"] == res.scope_refusals

    def test_disclosure_universal_question_partition_selection(self):
        facts = {
            "region_north_data": _facts(
                "region_north_data", ds="partition:region",
                ds_source="human", pv="north",
            )["region_north_data"],
        }
        sentence = scope_disclosure_sentence(
            ["region_north_data"], facts, None,
        )
        assert "region_north_data" in sentence
        assert "partition" in sentence
        assert "north" in sentence

    def test_disclosure_explicit_scope_mismatch(self):
        facts = {
            "region_south_data": _facts(
                "region_south_data", ds="partition:region",
                ds_source="human", pv="south",
            )["region_south_data"],
        }
        sentence = scope_disclosure_sentence(
            ["region_south_data"], facts, {"value": "north"},
        )
        assert "south" in sentence and "north" in sentence

    def test_disclosure_matching_scope_is_silent(self):
        facts = {
            "region_north_data": _facts(
                "region_north_data", ds="partition:region",
                ds_source="human", pv="north",
            )["region_north_data"],
        }
        assert scope_disclosure_sentence(
            ["region_north_data"], facts, {"value": "north"},
        ) == ""

    def test_disclosure_unknown_scope_says_nothing(self):
        """Concern 1, disclosure side: missing evidence must not read as
        either safe or unsafe — no sentence is invented for unknown scope."""
        facts = {
            "mystery_tbl": _facts("mystery_tbl")["mystery_tbl"],
            "known_global": _facts(
                "known_global", ds="global", ds_source="human",
            )["known_global"],
        }
        assert scope_disclosure_sentence(
            ["mystery_tbl", "known_global"], facts, None,
        ) == ""

    def test_disclosure_partition_family_discloses_uncertainty(self):
        """Reviewer table row 3: a probable partition with the key NOT
        identified must not silently claim completeness — but it is still a
        guess, so the sentence discloses uncertainty instead of asserting
        partiality as fact."""
        facts = {
            "region_north_data": _facts(
                "region_north_data", ds="partition_family",
                ds_source="inferred", ds_conf=0.55, pv="north",
            )["region_north_data"],
        }
        sentence = scope_disclosure_sentence(
            ["region_north_data"], facts, None,
        )
        assert "region_north_data" in sentence
        assert "could not be fully verified" in sentence
        # the guess must not be stated as a verified partition
        assert "hold a single partition" not in sentence

    def test_disclosure_family_with_verified_global_is_silent(self):
        """A verified global dataset in the selection makes completeness
        achievable — no uncertainty is claimed."""
        facts = {
            "region_north_data": _facts(
                "region_north_data", ds="partition_family",
                ds_source="inferred", ds_conf=0.55,
            )["region_north_data"],
            "all_market_rollup": _facts(
                "all_market_rollup", ds="global", ds_source="human",
            )["all_market_rollup"],
        }
        assert scope_disclosure_sentence(
            ["region_north_data", "all_market_rollup"], facts, None,
        ) == ""


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
