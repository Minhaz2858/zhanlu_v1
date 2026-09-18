"""Prompt-level regression tests for the semantic foundation.

Two defects were found LIVE on the aipdp warehouse (143 tables, MySQL). Neither
was visible to the existing suite, because both are about *what reaches the
model* rather than what a helper returns in isolation.

DEFECT 1 — the curated vocabulary was silently truncated away.
    ``build_business_context`` joins ordered parts and then caps the result at
    ``_MAX_CONTEXT_TOKENS * _APPROX_CHARS_PER_TOKEN`` characters, cutting from
    the TAIL. The curated vocabulary section used to be appended AFTER the
    domain map; four verbose domains measured 1,516 chars live, which pushed the
    curated terms past the cap. The code was correct, the log line was printed,
    and the model still never saw the term (``VOCABULARY_SECTION_RENDERS=False``
    while every unit-level assertion passed). Ordering is therefore a CONTRACT,
    not a style preference: curated (VERIFIED) meaning is emitted before the
    low-signal domain map.

DEFECT 2 — popularity presented as retrieval.
    ``rank_relations`` returned 25 tables scored purely by usage count for a
    question with no lexical match — i.e. "the most-used tables", handed to the
    model as if it were retrieval. Workload may now only RE-RANK what meaning
    already found; a relation with zero semantic evidence is never created as a
    candidate.

The truncation tests assert the failure mode alongside the fix (the cap must
actually have been hit), so a fixture that stops triggering truncation fails
loudly instead of passing vacuously.
"""

from __future__ import annotations

import logging
from datetime import datetime

import pytest

from app.config import settings
from app.services.knowledge_graph.business_context import build_business_context

# Mirrors business_context._MAX_CONTEXT_TOKENS * _APPROX_CHARS_PER_TOKEN.
CAP_CHARS = int(500 * 3.5)

KB = "kb-contract"


# ── fixtures / seeders ──────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401 — register tables before create_all
    # Terminology is NOT re-exported by app/models/__init__.py, so importing the
    # package alone leaves `terminologies` out of Base.metadata.
    from app.models.terminology import Terminology  # noqa: F401
    from app.models.nl2sql_query_log import Nl2sqlQueryLog  # noqa: F401
    from app.database import Base

    engine = create_engine(f"sqlite:///{tmp_path}/semantic.db")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _kb(db, kb_id: str, name: str, database_name: str = "", db_type: str = "mysql"):
    from app.models.knowledge_base import KnowledgeBase

    db.add(KnowledgeBase(
        id=kb_id, name=name, database_name=database_name or name, db_type=db_type,
    ))
    db.commit()


def _table(db, kb_id: str, table_name: str, description: str = "", domain: str | None = None):
    from app.models.knowledge_catalog import KBTableMeta

    db.add(KBTableMeta(
        id=f"{kb_id}:{table_name}",
        kb_id=kb_id,
        table_name=table_name,
        org_id="o1",
        app_id="a1",
        description_en=description or None,
        domain_name=domain,
        indexed_at=datetime.utcnow(),
    ))
    db.commit()


def _domain(db, kb_id: str, idx: int, tables: list[str], desc_chars: int = 440):
    """One discovered domain with a deliberately verbose description."""
    from app.models.knowledge_catalog import KBDomain, KBDomainMember

    did = f"{kb_id}-dom{idx}"
    desc = ("verbose discovered domain prose " * 40)[:desc_chars]
    db.add(KBDomain(
        id=did,
        kb_id=kb_id,
        name_en=f"Domain{idx}",
        description_en=desc,
        member_count=len(tables),
        method="derived",
        indexed_at=datetime.utcnow(),
    ))
    for t in tables:
        db.add(KBDomainMember(
            id=f"{did}:{t}",
            domain_id=did,
            kb_id=kb_id,
            table_meta_id=f"{kb_id}:{t}",
            table_name=t,
        ))
    db.commit()


def _term(db, word: str, description: str = "", datasource_ids=None):
    """A human-authored glossary entry (the VERIFIED tier)."""
    from app.models.terminology import Terminology

    db.add(Terminology(
        id=f"term-{word}",
        word=word,
        description=description,
        datasource_ids=datasource_ids,
        enabled=True,
    ))
    db.commit()


def _query_log(db, sql: str, n: int = 1, outcome: str = "success"):
    """Persisted execution telemetry — the LEARNED_FROM_WORKLOAD input."""
    from app.models.nl2sql_query_log import Nl2sqlQueryLog

    for i in range(n):
        db.add(Nl2sqlQueryLog(
            id=f"log-{i}-{abs(hash(sql))}",
            question="unrelated question",
            generated_sql=sql,
            outcome=outcome,
        ))
    db.commit()


def _verbose_catalog(db, kb_id: str, n_domains: int = 4, n_tables: int = 12):
    """A catalog whose domain map alone busts the context cap."""
    _kb(db, kb_id, "aipdp", database_name="aipdp")
    tables = [f"erp_t_relation_{i:02d}" for i in range(n_tables)]
    for t in tables:
        _table(db, kb_id, t)
    for i in range(n_domains):
        _domain(db, kb_id, i, tables)


def _enable(monkeypatch, **overrides):
    monkeypatch.setattr(settings, "KG_BUSINESS_CONTEXT_ENABLED", True)
    for key, value in overrides.items():
        monkeypatch.setattr(settings, key, value)


# ── DEFECT 1: curated vocabulary vs the token cap ───────────────────────────

class TestCuratedVocabularySurvivesTheTokenCap:
    """The curated block must be emitted before the long, low-signal domain map."""

    def _seed(self, db):
        _verbose_catalog(db, KB)
        _term(db, "Contract Amount", "Total monetary value of sales contracts.")

    def test_the_cap_is_actually_exercised(self, db, monkeypatch):
        """Guard against a vacuous suite: with the glossary off, the domain map
        alone must overflow the cap. If this stops failing, defect 1 is no
        longer being reproduced by this fixture."""
        _enable(monkeypatch, SEMANTIC_STORE_GLOSSARY_ENABLED=False)
        _verbose_catalog(db, KB)

        block = build_business_context(db, "proj1", KB, "total contract amount")

        assert block.endswith("(truncated)"), block[-120:]
        assert len(block) <= CAP_CHARS + len("\n(truncated)")

    def test_curated_term_outlives_the_truncation(self, db, monkeypatch):
        """The live failure: 1,516 chars of domains ate the budget and the
        curated term appended after them never reached the model."""
        _enable(monkeypatch)
        self._seed(db)

        block = build_business_context(db, "proj1", KB, "total contract amount")

        # The cap was hit (so the term had to earn its place early)...
        assert block.endswith("(truncated)")
        # ...and the curated meaning is still there.
        assert "Contract Amount" in block
        assert "Total monetary value of sales contracts." in block

    def test_curated_meaning_precedes_the_domain_map(self, db, monkeypatch):
        """Pin the ORDER, not just the presence: this is what makes the fix
        survive any future edit that appends a section."""
        _enable(monkeypatch)
        self._seed(db)

        block = build_business_context(db, "proj1", KB, "total contract amount")

        assert "## Business vocabulary (user-defined)" in block
        assert "## Business domains" in block
        assert block.index("Business vocabulary") < block.index("Business domains")

    def test_vocabulary_is_progressive_disclosure_not_a_dump(self, db, monkeypatch):
        """A curated term the question does not mention is not injected."""
        _enable(monkeypatch)
        _verbose_catalog(db, KB)
        _term(db, "Freight Surcharge", "Fuel surcharge billed on outbound freight.")

        block = build_business_context(db, "proj1", KB, "total contract amount")

        assert "Freight Surcharge" not in block
        assert "## Business domains" in block  # the rest of the block still renders

    def test_vocabulary_flag_off_keeps_the_rest_of_the_block(self, db, monkeypatch):
        _enable(monkeypatch, SEMANTIC_STORE_GLOSSARY_ENABLED=False)
        _verbose_catalog(db, KB)
        _term(db, "Contract Amount", "Total monetary value of sales contracts.")

        block = build_business_context(db, "proj1", KB, "total contract amount")

        assert "Business vocabulary" not in block
        assert "## Business domains" in block

    def test_store_failure_cannot_break_the_block(self, db, monkeypatch, caplog):
        """A semantic-store outage degrades one section, never the prompt."""
        import app.services.db.semantic_store as semantic_store_module

        _enable(monkeypatch)
        _verbose_catalog(db, KB)

        def boom(*_args, **_kwargs):
            raise RuntimeError("semantic store unavailable")

        monkeypatch.setattr(semantic_store_module, "get_semantic_store", boom)

        with caplog.at_level(logging.WARNING):
            block = build_business_context(db, "proj1", KB, "total contract amount")

        assert "## Business domains" in block
        assert "semantic store unavailable" in caplog.text


# ── DEFECT 2: retrieval evidence, and the documented recall gap ─────────────

def _store(db, kb_ids, project_id: str | None = "proj1"):
    from app.services.db.semantic_store import SemanticStore

    return SemanticStore(db, kb_ids, project_id=project_id)


class TestRetrievalEvidenceAndFallback:
    def _seed(self, db):
        _kb(db, KB, "aipdp")
        _table(db, KB, "erp_t_crm_contract", "Sales contract headers and their terms")
        _table(db, KB, "erp_t_freight_bill", "Outbound freight bills")

    def test_named_relation_is_retrieved_with_its_evidence(self, db):
        self._seed(db)

        hits = _store(db, [KB]).rank_relations("how many contract headers", k=12)

        names = [h.table_name for h in hits]
        assert "erp_t_crm_contract" in names
        assert "erp_t_freight_bill" not in names
        top = hits[0]
        assert top.table_name == "erp_t_crm_contract"
        assert any(e.startswith("name:") for e in top.evidence)
        assert top.score > 0

    def test_popularity_alone_never_creates_a_candidate(self, db):
        """The live regression, reproduced: a question with no lexical match came
        back with 25 tables scored purely by usage count — "the most-used
        tables", handed to the model as retrieval. Usage may only RE-RANK what
        meaning already found."""
        self._seed(db)
        _table(db, KB, "zzz_unrelated_ledger")
        _query_log(db, "SELECT * FROM zzz_unrelated_ledger", n=40)

        store = _store(db, [KB])
        # The usage IS known — this is not a test of empty telemetry.
        assert store.workload()["zzz_unrelated_ledger"].uses == 40

        hits = store.rank_relations(
            "how many contract headers", k=50, use_workload=True
        )
        assert "zzz_unrelated_ledger" not in [h.table_name for h in hits]

        # ...and a zero-lexical-match question stays EMPTY instead of degrading
        # into a popularity ranking.
        assert store.rank_relations("合同金额是多少", k=50, use_workload=True) == []

    def test_usage_reranks_but_never_outranks_meaning(self, db):
        """A popular relation may float up among semantic hits, never above an
        exact identifier match."""
        self._seed(db)
        _table(db, KB, "erp_t_contract_summary")
        _query_log(db, "SELECT * FROM erp_t_contract_summary", n=40)

        hits = _store(db, [KB]).rank_relations(
            "how many contract headers", k=25, use_workload=True
        )

        assert hits[0].table_name == "erp_t_crm_contract"
        popular = next(h for h in hits if h.table_name == "erp_t_contract_summary")
        assert any(e.startswith("uses:") for e in popular.evidence)
        assert popular.score < hits[0].score

    def test_chinese_question_against_english_catalog_is_the_known_gap(self, db):
        """DOCUMENTED GAP (cross-lingual recall).

        Retrieval is lexical-only by design. A pure-Chinese question against an
        English-named warehouse has zero lexical overlap, so it yields ZERO
        candidates and the caller keeps the full table-of-contents. That is the
        SAFE failure — no wrong narrowing — but it is a real recall gap.
        When an embedder lands in rank_relations this test MUST be rewritten;
        its second half is the part that may never regress.
        """
        from app.services.db.semantic_store import format_retrieval_hint

        self._seed(db)

        hits = _store(db, [KB]).rank_relations("合同金额是多少", k=12)

        assert hits == []
        # The load-bearing invariant: no candidates → no hint → full TOC kept.
        assert format_retrieval_hint(hits) == ""
        toc_text = "## Available tables\n- erp_t_crm_contract\n- erp_t_freight_bill"
        hint = format_retrieval_hint(hits)
        assert (f"{hint}\n\n{toc_text}" if hint else toc_text) == toc_text


class TestRetrievalHintFormatting:
    def _hits(self):
        from app.services.db.semantic_store import RankedRelation

        return [
            RankedRelation(table_name="erp_t_crm_contract", kb_id=KB, score=6.0,
                           evidence=("name:contract", "description:2")),
            RankedRelation(table_name="erp_t_sales_order", kb_id=KB, score=3.0,
                           evidence=("name:order",)),
        ]

    def test_empty_hits_render_no_block(self):
        from app.services.db.semantic_store import format_retrieval_hint

        assert format_retrieval_hint([]) == ""

    def test_evidence_is_carried_so_the_model_can_judge(self):
        from app.services.db.semantic_store import format_retrieval_hint

        text = format_retrieval_hint(self._hits())

        assert text.startswith("## Candidate tables for this question")
        assert "erp_t_crm_contract" in text
        assert "name:contract" in text
        # Candidates, not a verdict: no instruction to use exactly these.
        assert "must" not in text.lower()

    def test_limit_is_respected(self):
        from app.services.db.semantic_store import RankedRelation, format_retrieval_hint

        hits = [
            RankedRelation(table_name=f"t_{i}", kb_id=KB, score=1.0)
            for i in range(20)
        ]
        text = format_retrieval_hint(hits, limit=5)
        assert text.count("\n- ") == 5


# ── cross-source ranking (more than one datasource bound) ───────────────────

class TestCrossSourceRanking:
    def _seed(self, db):
        _kb(db, "kb-logistics", "Logistics Warehouse", database_name="logistics")
        _kb(db, KB, "aipdp", database_name="aipdp")
        _table(db, KB, "erp_t_crm_contract", "Sales contract headers")
        _table(db, "kb-logistics", "erp_t_freight_bill", "Outbound freight bills")

    def test_source_without_a_match_is_excluded(self, db):
        """One candidate is the MOST useful case: it means the other bound
        sources had no match at all."""
        from app.services.db.semantic_store import format_source_ranking

        self._seed(db)
        ranked = _store(db, [KB, "kb-logistics"]).rank_sources("contract amount")

        assert [c.kb_id for c in ranked] == [KB]
        rendered = format_source_ranking(ranked)
        assert "aipdp" in rendered
        assert "Logistics Warehouse" not in rendered

    def test_all_zero_ranking_is_suppressed(self, db):
        from app.services.db.semantic_store import SourceCandidate, format_source_ranking

        zeroed = [
            SourceCandidate(kb_id="kb-a", name="A", score=0.0),
            SourceCandidate(kb_id="kb-b", name="B", score=0.0),
        ]
        assert format_source_ranking(zeroed) == ""
        assert format_source_ranking([]) == ""

    def test_out_of_scope_source_is_never_ranked(self, db):
        """CROSS_ORG_SEMANTIC_LEAK = 0: the store cannot report a datasource it
        was not handed, even when that datasource holds a matching table."""
        self._seed(db)
        _table(db, "kb-logistics", "erp_t_crm_contract", "A different contract table")

        hits = _store(db, [KB]).rank_relations("how many contract headers", k=25)

        assert hits, "expected an in-scope hit"
        assert {h.kb_id for h in hits} == {KB}
        assert "kb-logistics" not in {h.kb_id for h in hits}
