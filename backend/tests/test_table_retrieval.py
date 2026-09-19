"""Regression tests for deterministic candidate retrieval + the selection surface.

Every defect pinned here was measured LIVE on the aipdp warehouse (143 tables,
MySQL) on 2026-09-18, driving the real ``NLAnswerService.answer()``:

  * ``format_toc_text(toc, token_budget=1200)`` rendered 14 of 143 tables — the
    ALPHABETICAL head — so the model selected from ``_ref_material_mapping``,
    ``access_model_config``, ``actual_price``, ``auth_*`` … and never saw a
    business table. Asked for a sales report it picked ``actual_price`` (data
    ends 2025-07-31) and ``chain_product_kind`` (0 rows) → 0 rows → "no data".
  * ``_select_tables_from_toc`` returned the model's raw list when no name
    matched the catalog, so ``['contract_execution', 'contract', …]`` — three
    nonexistent names — reached SQL generation ("table 'contracts' doesn't exist").
  * Coverage: ``erp_product_sales_details`` ends 2025-12-31 and still appeared as
    a valid candidate for a 2026-08 question, because nothing consumed the
    catalog's own ``min_date``/``max_date``.
  * The re-selection coverage probe hardcoded MySQL backticks and asked
    ``COUNT(*)`` (17.7 s on a view), so it returned nothing on all five
    PostgreSQL sources in the stack.

The failure mode is asserted alongside each fix, so a fixture that stops
reproducing the defect fails loudly instead of passing vacuously.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services.db.table_retrieval import (
    RetrievalResult,
    TableCandidate,
    candidate_names,
    format_candidates_block,
    retrieve_for_question,
)
from app.services.knowledge_graph.schema_linker import (
    format_toc_for_selection,
    format_toc_text,
)

# The question's window in the live failure ("这个月" resolved to September).
WINDOW = (date(2026, 9, 1), date(2026, 10, 1))


# ── fixtures ────────────────────────────────────────────────────────────────

def _toc_entry(name: str, **kw) -> dict:
    entry = {
        "table_name": name,
        "description": f"{name} description",
        "row_count": 1000,
        "date_column": "created_at",
        "max_date": "2026-09-15",
        "min_date": "2020-01-01",
        "table_role": "fact",
        "domain_name": "Sales",
        "columns": [],
    }
    entry.update(kw)
    return entry


@pytest.fixture
def catalog() -> list[dict]:
    """A synthetic 143-table catalog shaped like the live one.

    Sorted by table name, like ``build_full_toc`` returns it, and padded so the
    alphabetical head is dominated by non-business tables — the exact condition
    that made the selector pick from ``auth_*`` / ``actual_price``.
    """
    entries = [
        _toc_entry("_ref_material_mapping", row_count=26544),
        _toc_entry("access_model_config", row_count=1),
        _toc_entry("actual_price", max_date="2025-07-31", min_date="0000-00-00"),
        _toc_entry("alert_rules", row_count=0),
        _toc_entry("auth_group", row_count=0),
        _toc_entry("auth_user", row_count=0),
        _toc_entry("auth_tokens", row_count=1183),
        _toc_entry("chain_product_kind", row_count=0),
        _toc_entry("cockpit_definitions", row_count=0),
    ]
    entries += [_toc_entry(f"aaa_noise_{i:03d}") for i in range(60)]
    # The table that ACTUALLY holds the answer, far down the alphabet.
    entries.append(_toc_entry("erp_t_crm_contract", row_count=12098))
    entries += [_toc_entry(f"zzz_tail_{i:03d}") for i in range(70)]
    return sorted(entries, key=lambda t: t["table_name"])


class _FakeStore:
    def __init__(self, hits):  # noqa: D107
        self._hits = hits

    def rank_relations(self, question, k=25):  # noqa: D102
        return self._hits


class _Hit:
    def __init__(self, table_name, score, evidence):  # noqa: D107
        self.table_name = table_name
        self.score = score
        self.evidence = evidence


def _patch_retrieval_deps(monkeypatch, hits, plan=None, scoped=None):
    """Keep the test hermetic: no DB, no Chroma, no domain discovery."""
    import app.services.db.semantic_store as ss
    import app.services.db.plan_memory as pm
    import app.services.knowledge_graph.domain_discovery as dd

    monkeypatch.setattr(ss, "get_semantic_store", lambda *a, **k: _FakeStore(hits))
    monkeypatch.setattr(pm, "load_plan", lambda *a, **k: plan)

    class _Matched:
        def __init__(self, n):  # noqa: D107
            self._n = n

        def get(self, key, default=None):  # noqa: D102
            return self._n if key in ("name_en", "name_zh") else default

    monkeypatch.setattr(
        dd,
        "scoped_tables_for_question",
        lambda *a, **k: (scoped or [], [_Matched("Sales")] if scoped else []),
    )


# ── the render defect ───────────────────────────────────────────────────────

def test_the_legacy_renderer_reproduces_the_starvation(catalog):
    """Guard against a vacuous suite: the old path MUST starve on this fixture."""
    legacy = format_toc_text(catalog, token_budget=1200)
    assert "erp_t_crm_contract" not in legacy, (
        "fixture no longer reproduces the live defect — the alphabetical head "
        "must be large enough to hide the business table"
    )
    assert "not shown due to budget" in legacy


def test_the_selection_surface_never_hides_a_candidate(catalog):
    """The whole point: a ranked candidate is rendered in FULL, in rank order."""
    text = format_toc_for_selection(catalog, ["erp_t_crm_contract"])
    assert "erp_t_crm_contract" in text
    assert "Ranked candidates:" in text
    # It must be in the ranked block, i.e. before the rest of the catalog.
    assert text.index("erp_t_crm_contract") < text.index("Rest of the catalog:")
    # Nothing is dropped at this size.
    assert "dropped by the render cap" not in text


def test_the_whole_catalog_still_fits_compact(catalog):
    """143 tables must be representable — the live catalog is only ~23 KB."""
    text = format_toc_for_selection(catalog, [])
    for name in ("aaa_noise_000", "zzz_tail_069", "erp_t_crm_contract"):
        assert name in text


def test_the_render_cap_reports_what_it_dropped():
    big = [_toc_entry(f"t{i:04d}", description="d" * 60) for i in range(200)]
    text = format_toc_for_selection(big, [], hard_char_cap=4000)
    assert "dropped by the render cap" in text
    # The count must be the tables actually dropped, not len(lines).
    dropped = int(text.split("... and ")[1].split(" more table")[0])
    assert dropped > 0
    assert len([ln for ln in text.splitlines() if ln.startswith("  t")]) + dropped == len(big)


# ── ranking ─────────────────────────────────────────────────────────────────

def test_ranking_puts_evidenced_tables_first(monkeypatch, catalog):
    _patch_retrieval_deps(monkeypatch, [_Hit("erp_t_crm_contract", 1.0, ["name match"])])
    res = retrieve_for_question(None, ["kb"], "这个月的合同执行情况", catalog, window=WINDOW)
    assert res.names[0] == "erp_t_crm_contract"
    assert res.method == "semantic"
    assert res.candidates[0].evidence


def test_a_verified_plan_outranks_lexical_evidence(monkeypatch, catalog):
    _patch_retrieval_deps(
        monkeypatch,
        [_Hit("aaa_noise_001", 2.0, ["weak"])],
        plan={"tables": ["erp_t_crm_contract"]},
    )
    res = retrieve_for_question(None, ["kb"], "q", catalog, window=WINDOW)
    assert res.names[0] == "erp_t_crm_contract"
    assert "verified plan" in " ".join(res.candidates[0].evidence)


def test_retrieval_never_offers_a_table_outside_the_catalog(monkeypatch, catalog):
    """A name the model invents is not a table; neither is one outside scope."""
    _patch_retrieval_deps(
        monkeypatch,
        [_Hit("contract_execution", 9.0, ["invented"]), _Hit("auth_user", 1.0, ["in"])],
    )
    res = retrieve_for_question(None, ["kb"], "q", catalog, window=WINDOW)
    assert "contract_execution" not in res.names
    assert "auth_user" in res.names


def test_candidate_names_is_the_deterministic_fallback(monkeypatch, catalog):
    _patch_retrieval_deps(
        monkeypatch,
        [_Hit("erp_t_crm_contract", 1.0, ["a"]), _Hit("actual_price", 0.5, ["b"])],
    )
    res = retrieve_for_question(None, ["kb"], "q", catalog, window=WINDOW)
    assert candidate_names(res, k=4)[:2] == ["erp_t_crm_contract", "actual_price"]


# ── coverage: constrain and disclose, never refuse ─────────────────────────

def test_a_table_without_data_in_the_window_is_labelled_not_dropped(monkeypatch, catalog):
    _patch_retrieval_deps(monkeypatch, [_Hit("actual_price", 1.0, ["price"])])
    res = retrieve_for_question(None, ["kb"], "2026年9月价格", catalog, window=WINDOW)
    cand = res.candidates[0]
    assert cand.covers_window is False
    assert cand.table_name in res.names, "coverage must constrain, not filter"
    block = format_candidates_block(res)
    assert "NO DATA IN REQUESTED WINDOW" in block


def test_all_candidates_stale_discloses_instead_of_reporting_zero(monkeypatch, catalog):
    """Oracle's rule: reroute or disclose — never answer the period as zero."""
    _patch_retrieval_deps(monkeypatch, [_Hit("actual_price", 1.0, ["price"])])
    res = retrieve_for_question(None, ["kb"], "2026年9月价格", catalog, window=WINDOW)
    assert res.uncovered is True
    assert res.latest_available == "2025-07-31"
    block = format_candidates_block(res)
    assert "NONE of the ranked candidates" in block
    assert "do not report the requested period as zero" in block


def test_a_covered_candidate_means_no_disclosure(monkeypatch, catalog):
    _patch_retrieval_deps(monkeypatch, [_Hit("erp_t_crm_contract", 1.0, ["c"])])
    res = retrieve_for_question(None, ["kb"], "这个月", catalog, window=WINDOW)
    assert res.uncovered is False
    assert res.candidates[0].covers_window is True


def test_missing_coverage_is_unknown_never_covered(monkeypatch, catalog):
    """A missing measurement must be reported as missing."""
    cat = catalog + [_toc_entry("no_window_table", min_date="", max_date="")]
    _patch_retrieval_deps(monkeypatch, [_Hit("no_window_table", 1.0, ["x"])])
    res = retrieve_for_question(None, ["kb"], "2026年9月", cat, window=WINDOW)
    assert res.candidates[0].covers_window is None
    assert "no_window_table" in res.unknown_coverage
    assert "window unknown" in format_candidates_block(res)
    assert "UNKNOWN, not empty" in format_candidates_block(res)


def test_a_mysql_zero_date_is_not_a_window(monkeypatch, catalog):
    """``actual_price`` stores min_date '0000-00-00' — it must not parse."""
    _patch_retrieval_deps(monkeypatch, [_Hit("actual_price", 1.0, ["x"])])
    res = retrieve_for_question(None, ["kb"], "q", catalog, window=WINDOW)
    assert res.candidates[0].covers_window is False  # not "covered by year 0"
    assert res.candidates[0].coverage_start == "0000-00-00"


def test_no_window_means_no_coverage_claim(monkeypatch, catalog):
    _patch_retrieval_deps(monkeypatch, [_Hit("erp_t_crm_contract", 1.0, ["x"])])
    res = retrieve_for_question(None, ["kb"], "how many contracts", catalog)
    assert res.window is None
    assert res.candidates[0].covers_window is None
    assert res.uncovered is False


def test_a_table_that_starts_later_is_still_offered(monkeypatch, catalog):
    """Overlap, not containment: a future-dated table is not a stale one."""
    cat = catalog + [_toc_entry("new_table", min_date="2026-09-20", max_date="2026-12-01")]
    _patch_retrieval_deps(monkeypatch, [_Hit("new_table", 1.0, ["x"])])
    res = retrieve_for_question(None, ["kb"], "2026年9月", cat, window=WINDOW)
    assert res.candidates[0].covers_window is True


# ── retrieval must never break answering ───────────────────────────────────

def test_retrieval_failure_degrades_to_no_candidates(catalog):
    """No semantic store, no plan memory, no domain map → empty, not an error."""
    res = retrieve_for_question(None, ["kb"], "q", catalog, window=WINDOW)
    assert isinstance(res, RetrievalResult)
    assert res.candidates == []
    assert format_candidates_block(res) == ""
    assert candidate_names(res) == []


def test_empty_catalog_or_question_is_a_no_op(catalog):
    assert retrieve_for_question(None, ["kb"], "", catalog).candidates == []
    assert retrieve_for_question(None, ["kb"], "q", []).candidates == []


# ── the selection contract: only names that exist ──────────────────────────

class _FakeLLM:
    def __init__(self, payload):  # noqa: D107
        self.payload = payload
        self.seen: list = []

    async def __call__(self, messages, **kw):  # noqa: D102
        self.seen.append(messages)
        return self.payload


@pytest.mark.asyncio
async def test_selection_rejects_names_that_do_not_exist(monkeypatch):
    """The passthrough that produced "table 'contracts' doesn't exist"."""
    import app.services.db.nl_answer_service as nls

    fake = _FakeLLM('["contract_execution", "contract", "erp_t_crm_contract"]')
    monkeypatch.setattr(nls, "_chat", fake)
    svc = nls.NLAnswerService(None)
    picked = await svc._select_tables_from_toc(
        "这个月合同", "-- toc\n", 3,
        known_names={"erp_t_crm_contract", "erp_t_crm_contractentry"},
    )
    assert picked == ["erp_t_crm_contract"]


@pytest.mark.asyncio
async def test_selection_of_only_invented_names_is_empty(monkeypatch):
    """Empty, so the caller can fall back — the old code returned all three."""
    import app.services.db.nl_answer_service as nls

    monkeypatch.setattr(
        nls, "_chat",
        _FakeLLM('["contract_execution", "contract", "contract_execution_detail"]'),
    )
    svc = nls.NLAnswerService(None)
    picked = await svc._select_tables_from_toc(
        "这个月合同", "-- toc\n", 3, known_names={"erp_t_crm_contract"},
    )
    assert picked == []


@pytest.mark.asyncio
async def test_selection_is_validated_against_the_catalog_not_the_render(monkeypatch):
    """A table absent from a truncated render but present in the catalog is valid."""
    import app.services.db.nl_answer_service as nls

    monkeypatch.setattr(nls, "_chat", _FakeLLM('["erp_t_crm_contract"]'))
    svc = nls.NLAnswerService(None)
    picked = await svc._select_tables_from_toc(
        "q", "-- nothing rendered here --\n", 143,
        known_names={"erp_t_crm_contract"},
    )
    assert picked == ["erp_t_crm_contract"]


# ── the coverage probe: dialect-correct, and not a COUNT(*) ────────────────

class _FakeKB:
    def __init__(self, db_type):  # noqa: D107
        self.db_type = db_type


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dialect,expected_fragment",
    [
        ("mysql", "FROM `customers`"),
        ("postgresql", 'FROM "customers"'),
        ("mssql", "FROM [customers]"),
        ("oracle", 'FROM "customers"'),
    ],
)
async def test_coverage_probe_quotes_for_every_dialect(
    monkeypatch, dialect, expected_fragment
):
    """The probe was MySQL-only: backticks on Postgres are a syntax error."""
    import app.services.db.nl_answer_service as nls
    import app.services.db.query_service as qs
    from app.models.knowledge_base import KnowledgeBase

    seen: list[str] = []

    class _Q:
        def __init__(self, db):  # noqa: D107
            pass

        def execute(self, kb_id, sql, *a, **k):  # noqa: D102
            seen.append(sql)
            return {"success": True, "rows": [{"x": 1}]}

    monkeypatch.setattr(qs, "QueryService", _Q)

    class _Query:
        def filter(self, *a, **k):  # noqa: D102
            return self

        def first(self):  # noqa: D102
            return _FakeKB(dialect)

        def all(self):  # noqa: D102
            return []

    class _DB:
        def query(self, model):  # noqa: D102
            return _Query()

    out = await nls._probe_table_coverage(
        _DB(), "kb", ["customers"], window=(date(2026, 9, 1), date(2026, 10, 1)),
    )
    assert seen, "the probe issued no statement"
    assert expected_fragment in seen[0]
    assert "COUNT(*)" not in seen[0].upper(), (
        "COUNT(*) on a view measured 17.7s against a bounded budget — "
        "the probe must stay a LIMIT 1 existence test"
    )
    assert "LIMIT 1" in seen[0]
    assert out.get("customers") == "has rows"


@pytest.mark.asyncio
async def test_coverage_probe_skips_unsafe_identifiers(monkeypatch):
    """Prompt-influenced names never reach SQL."""
    import app.services.db.nl_answer_service as nls
    import app.services.db.query_service as qs

    seen: list[str] = []

    class _Q:
        def __init__(self, db):  # noqa: D107
            pass

        def execute(self, kb_id, sql, *a, **k):  # noqa: D102
            seen.append(sql)
            return {"success": True, "rows": []}

    monkeypatch.setattr(qs, "QueryService", _Q)

    class _Query:
        def filter(self, *a, **k):  # noqa: D102
            return self

        def first(self):  # noqa: D102
            return _FakeKB("mysql")

        def all(self):  # noqa: D102
            return []

    class _DB:
        def query(self, model):  # noqa: D102
            return _Query()

    out = await nls._probe_table_coverage(
        _DB(), "kb", ["users; DROP TABLE x", "safe_table"],
    )
    assert seen == ["SELECT 1 FROM `safe_table` LIMIT 1"]
    assert out == {"safe_table": "empty"}


# ── provenance ─────────────────────────────────────────────────────────────

def test_provenance_pairs_the_requested_window_with_the_table_window():
    """The distinction the live failure turned on: asked 2026-09, table ends 2025."""
    import app.services.db.nl_answer_service as nls

    prov = nls._build_provenance(
        kb_id="kb1",
        source_name="warehouse",
        sql="SELECT COUNT(*)  FROM t",
        tables=["t"],
        window=WINDOW,
        catalog=[_toc_entry("t", min_date="2025-01-01", max_date="2025-12-31")],
        retrieval={"method": "semantic", "candidate_count": 7},
    )
    assert prov["requested_window"] == ["2026-09-01", "2026-10-01"]
    assert prov["table_coverage"]["t"]["max"] == "2025-12-31"
    assert prov["sql_fingerprint"] and len(prov["sql_fingerprint"]) == 16
    assert prov["retrieval_method"] == "semantic"
    assert prov["candidates_considered"] == 7


def test_provenance_fingerprint_ignores_whitespace():
    import app.services.db.nl_answer_service as nls

    a = nls._build_provenance(
        kb_id="k", source_name=None, sql="SELECT 1\n  FROM t", tables=[],
        window=None, catalog=None, retrieval=None,
    )
    b = nls._build_provenance(
        kb_id="k", source_name=None, sql="SELECT 1 FROM t", tables=[],
        window=None, catalog=None, retrieval=None,
    )
    assert a["sql_fingerprint"] == b["sql_fingerprint"]


def test_candidate_as_dict_is_serialisable():
    import json

    c = TableCandidate(table_name="t", score=1.0, evidence=["e"], covers_window=False)
    res = RetrievalResult(candidates=[c], window=WINDOW)
    json.dumps(res.as_dict())
    json.dumps(c.as_dict())
