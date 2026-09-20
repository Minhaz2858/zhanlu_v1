"""Chinese table-retrieval ranking — the fused, explainable ordering.

The defect pinned here was measured live on 2026-09-19 against the 143-table
aipdp warehouse, driving the real ``NLAnswerService.answer()``:

  * ``这个月的合同执行情况`` retrieved with ``method=none`` — the ENTIRE question
    came back as a single token (``_tokens`` splits on non-alphanumerics, and a
    Chinese run has no separators inside it), so no table could match it. The
    top-8 candidates were eight ``auth_*`` tables, every one of them carrying a
    1.5-point "domain match" from a domain ranker that had scoped the question
    into "User Authentication and Permissions".
  * all 143 tables had a ``description_zh`` that nothing ever read: the store
    picked ``description_en or description_zh`` and the catalog was bilingual.
  * the ordering was a sum of boosts nothing could explain, so a wrong top-1
    could not be traced to the signal that produced it.

Each test asserts the fix AND the failure shape that motivates it, so a fixture
that stops reproducing the defect fails loudly instead of passing vacuously.
"""

from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

import pytest

from app.services.db.lexical import Bm25Index, cosine, has_cjk, identifier_terms, token_list, tokens

# ── fixtures ────────────────────────────────────────────────────────────────


def _entry(name: str, *, zh="", en="", domain="", role="fact", columns=(), **kw) -> dict:
    entry = {
        "table_name": name,
        "description": en or zh,
        "description_zh": zh,
        "description_en": en,
        "row_count": 1000,
        "date_column": "created_at",
        "max_date": "2026-09-15",
        "min_date": "2018-01-01",
        "table_role": role,
        "domain_name": domain,
        "columns": [dict(c) for c in columns],
    }
    entry.update(kw)
    return entry


def _col(name, semantic_type, description, unit=""):
    return {"name": name, "data_type": "decimal", "semantic_type": semantic_type,
            "unit": unit, "description": description}


AUTH_DOMAIN = "User Authentication and Permissions"
SALES_DOMAIN = "Sales and Inventory Management"


@pytest.fixture
def catalog() -> list[dict]:
    """A bilingual catalog shaped like the live warehouse.

    Eight ``auth_*`` tables that hold nothing a business question can use, the
    contract/delivery/sales tables that hold the answers, and alphabetical noise
    in between — the exact conditions under which the live ranking produced
    ``auth_*`` for a contract question.
    """
    entries = [
        _entry("auth_group", zh="用户组，认证框架中的用户组表", en="User group", domain=AUTH_DOMAIN),
        _entry("auth_permission", zh="权限表，定义系统权限项", en="Permission", domain=AUTH_DOMAIN),
        _entry("auth_user", zh="用户表，存储系统用户的账号、密码、权限等认证信息",
               en="User account", domain=AUTH_DOMAIN),
        _entry("auth_user_groups", zh="用户与用户组的关联表", en="User group link", domain=AUTH_DOMAIN),
        _entry("auth_tokens", zh="认证令牌表，存储访问令牌", en="Access token", domain=AUTH_DOMAIN),
        _entry("users", zh="用户账号表", en="Users", domain=AUTH_DOMAIN),
        _entry("actual_price", zh="行情价格表，记录每日市场价格", en="Market price",
               domain="Market Price Data"),
        _entry("erp_t_crm_contract",
               zh="销售合同主表，记录合同编号、客户、合同日期、合同金额、签订组织等信息",
               en="Sales contract header", domain=SALES_DOMAIN,
               columns=[
                   _col("FBILLNO", "dimension", "合同编号"),
                   _col("FCUSTID", "identifier", "客户ID，关联客户主数据"),
                   _col("FDATE", "temporal", "合同日期"),
                   _col("FAMOUNT", "measure", "合同金额，价税合计", "元"),
               ]),
        _entry("erp_t_crm_contractentry",
               zh="销售合同明细表，记录合同的物料明细行，包括数量、单价、金额、税率",
               en="Sales contract line", domain=SALES_DOMAIN,
               columns=[
                   _col("FQTY", "measure", "数量，以计价单位计量", "吨"),
                   _col("FPRICE", "measure", "单价", "元/吨"),
                   _col("FAMOUNT", "measure", "金额", "元"),
               ]),
        _entry("erp_v_contract_execution",
               zh="合同执行情况视图，统计合同签订数量、实际发货数量、履约率、合同金额与发货金额",
               en="Contract execution overview", domain=SALES_DOMAIN,
               columns=[
                   _col("contract_fqty", "measure", "合同数量", "吨"),
                   _col("out_notoutqty", "measure", "未发货数量", "吨"),
                   _col("contract_amount", "measure", "合同金额", "元"),
               ]),
        _entry("erp_t_sal_deliverynotice",
               zh="销售发货通知单表，记录发货单据编号、客户、发货日期、结算信息",
               en="Delivery notice", domain=SALES_DOMAIN,
               columns=[_col("FDELIVERYNO", "dimension", "发货单号"),
                        _col("FDELIVERYDATE", "temporal", "发货日期")]),
        _entry("erp_t_sal_deliverynoticeentry",
               zh="销售发货通知单明细表，记录每个物料的发货数量、单价与金额",
               en="Delivery notice line", domain=SALES_DOMAIN,
               columns=[_col("FOUTQTY", "measure", "发货数量", "吨")]),
        _entry("erp_v_sale_huizhou",
               zh="惠州地区销售订单视图，包含物料规格、编码、单价、数量、金额",
               en="Huizhou sales order view", domain=SALES_DOMAIN,
               columns=[_col("FPRICE", "measure", "单价", "元"),
                        _col("FQTY", "measure", "数量", "吨")]),
    ]
    entries += [_entry(f"aaa_noise_{i:03d}", zh=f"系统配置表{i}", en=f"config {i}")
                for i in range(40)]
    return sorted(entries, key=lambda t: t["table_name"])


def _patch_off(monkeypatch):
    """Prove the ranking stands on the CATALOG alone.

    No semantic store, no plan memory, no domain map, no embeddings: what is
    left is the fusion over the table-of-contents, which is the part that must
    work on a database nobody has ever configured.
    """
    import app.services.db.semantic_store as ss
    import app.services.db.plan_memory as pm
    import app.services.knowledge_graph.domain_discovery as dd

    def _boom(*_a, **_k):
        raise RuntimeError("not available in this test")

    monkeypatch.setattr(ss, "get_semantic_store", _boom)
    monkeypatch.setattr(pm, "load_plan", lambda *a, **k: None)
    monkeypatch.setattr(dd, "scoped_tables_for_question", lambda *a, **k: (set(), []))


def _retrieve(catalog, question, monkeypatch=None, **kw):
    from app.services.db.table_retrieval import retrieve_for_question

    kw.setdefault("use_embeddings", False)
    return retrieve_for_question(None, ["kb"], question, catalog, **kw)


# ── the tokenizer: no whitespace to split on ────────────────────────────────

def test_a_chinese_question_is_no_longer_one_giant_token():
    """The root cause of ``method=none`` on every pure-CJK question."""
    question = "这个月的合同执行情况"
    assert "合同" in tokens(question)
    assert "执行" in tokens(question)
    # The old shape: the whole run came back as a single token, which nothing in
    # any catalog can match.
    assert question not in tokens(question)
    assert tokens(question) != {question}


def test_latin_identifiers_still_tokenise_as_words():
    assert identifier_terms("erp_t_crm_contract") == ["erp", "crm", "contract"]
    assert "contract" in tokens("how many contract rows")


def test_a_mixed_script_run_keeps_its_latin_word():
    """``2026年8月合同`` must not shred "2026" into digit bigrams."""
    terms = token_list("2026年8月合同签订数量")
    assert "2026" in terms
    assert "合同" in terms
    assert "02" not in terms and "26" not in terms


def test_has_cjk_separates_the_scripts():
    assert has_cjk("合同") and not has_cjk("contract")


# ── BM25: rarity comes from the catalog, not from a stop-word list ──────────

def test_idf_comes_from_the_catalog_so_a_common_gram_is_worthless():
    docs = {"a": "系统配置表 情况", "b": "系统配置表 情况", "c": "系统配置表 情况", "d": "销售合同情况"}
    index = Bm25Index(docs)
    assert index.idf("配置") < index.idf("合同")


def test_a_term_in_every_document_carries_no_discrimination():
    """IDF is measured from the catalog, so a filler gram cannot reorder it."""
    docs = {f"t{i}": "共享 词汇 数据" for i in range(5)}
    uniform = Bm25Index(docs).score(["共享"])
    assert len(set(uniform.values())) == 1, "a term in every document must score alike"

    docs["t0"] = "共享 词汇 数据 合同"
    rare = Bm25Index(docs).score(["合同"])
    assert max(rare.values()) > 10 * max(uniform.values())


def test_bm25_rewards_the_rare_match_over_the_common_one():
    docs = {"common": "数据 系统 情况", "rare": "合同 执行 数量", "other": "数据 报表"}
    scores = Bm25Index(docs).score(["合同", "数据"])
    assert scores["rare"] > scores["common"]


def test_bm25_is_deterministic():
    docs = {"a": "合同 执行", "b": "发货 数量"}
    first = Bm25Index(docs).score(["合同", "发货"])
    second = Bm25Index(dict(reversed(list(docs.items())))).score(["合同", "发货"])
    assert first == second


def test_cosine_is_safe_on_degenerate_input():
    assert cosine([], [1.0]) == 0.0
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


# ── the live failure: a Chinese contract question against auth_* ────────────

def test_a_chinese_contract_question_ranks_contract_tables_first(catalog, monkeypatch):
    _patch_off(monkeypatch)
    res = _retrieve(catalog, "这个月的合同执行情况", monkeypatch, window=None)

    assert res.names, "retrieval produced nothing without a semantic store"
    assert res.names[0] == "erp_v_contract_execution", res.names[:6]
    for expected in ("erp_t_crm_contract", "erp_t_crm_contractentry"):
        assert expected in res.names[:6], (expected, res.names[:6])


def test_auth_tables_do_not_outrank_contract_tables(catalog, monkeypatch):
    """The exact live regression: top-8 used to be eight ``auth_*`` tables."""
    _patch_off(monkeypatch)
    res = _retrieve(catalog, "这个月的合同执行情况", monkeypatch)

    ranked = res.names
    contract_rank = min(ranked.index(n) for n in
                        ("erp_t_crm_contract", "erp_v_contract_execution") if n in ranked)
    auth_ranks = [ranked.index(n) for n in ranked if n.startswith("auth")]
    assert not auth_ranks or min(auth_ranks) > contract_rank
    assert not any(n.startswith("auth") for n in ranked[:8]), ranked[:8]


def test_auth_tables_hold_no_evidence_for_a_contract_question(catalog, monkeypatch):
    """They are not merely ranked lower — nothing in them matches the question."""
    _patch_off(monkeypatch)
    res = _retrieve(catalog, "这个月的合同执行情况", monkeypatch)
    assert not any(c.table_name.startswith("auth") for c in res.candidates)


def test_the_other_live_questions_rank_their_business_tables(catalog, monkeypatch):
    _patch_off(monkeypatch)
    overview = _retrieve(
        catalog,
        "2026年8月合同执行情况总览：合同签订数量、实际发货数量、履约率(发货/合同)、合同金额、发货金额",
        monkeypatch,
    )
    assert {"erp_v_contract_execution", "erp_t_crm_contract"} & set(overview.names[:4])
    assert "erp_t_sal_deliverynotice" in overview.names[:8]

    grouped = _retrieve(catalog, "按客户分组统计 2026 年 9 月的合同签订数量", monkeypatch)
    # Two tables legitimately answer this — the contract header (holds 客户) and
    # the contract-execution view (whose own description is 合同签订数量) — so
    # the property to pin is that the CONTRACT tables lead and that the auth
    # block that used to occupy the whole top-8 cannot reappear.
    assert set(grouped.names[:4]) & {"erp_t_crm_contract", "erp_v_contract_execution"}
    assert "erp_t_crm_contract" in grouped.names[:3], grouped.names[:5]
    assert not any(n.startswith("auth") for n in grouped.names[:8]), grouped.names[:8]


def test_a_sales_report_question_ranks_the_sales_view(catalog, monkeypatch):
    _patch_off(monkeypatch)
    res = _retrieve(
        catalog,
        "本周销售情况，生成包含规格、编码、单价、数量、金额的多维度分析报表",
        monkeypatch,
    )
    assert res.names[0] == "erp_v_sale_huizhou", res.names[:5]


# ── ordering only: nothing is ever filtered away ────────────────────────────

def test_the_ranked_block_never_becomes_a_filter(catalog, monkeypatch):
    """The full catalog is still rendered behind the ranked block."""
    from app.services.knowledge_graph.schema_linker import format_toc_for_selection

    _patch_off(monkeypatch)
    res = _retrieve(catalog, "这个月的合同执行情况", monkeypatch)
    text = format_toc_for_selection(catalog, res.names)
    # auth_user is not a candidate — and is still visible to the selector.
    assert "auth_user" not in res.names
    assert "auth_user" in text


def test_every_catalog_table_can_still_be_chosen(catalog, monkeypatch):
    from app.services.db.table_retrieval import candidate_names

    _patch_off(monkeypatch)
    res = _retrieve(catalog, "这个月的合同执行情况", monkeypatch)
    assert set(candidate_names(res, k=4)) <= {t["table_name"] for t in catalog}
    assert len(candidate_names(res, k=4)) == 4


# ── explainability: every point has a named source ──────────────────────────

def test_the_breakdown_sums_to_the_score(catalog, monkeypatch):
    _patch_off(monkeypatch)
    res = _retrieve(catalog, "这个月的合同执行情况", monkeypatch)
    for c in res.candidates:
        assert sum(c.breakdown.values()) == pytest.approx(c.score, abs=0.01), c.table_name
        assert c.breakdown, f"{c.table_name} scored {c.score} with no recorded component"


def test_evidence_names_the_component_that_produced_it(catalog, monkeypatch):
    _patch_off(monkeypatch)
    res = _retrieve(catalog, "这个月的合同执行情况", monkeypatch)
    top = res.candidates[0]
    assert any(e.startswith("text[") for e in top.evidence), top.evidence
    block = __import__(
        "app.services.db.table_retrieval", fromlist=["format_candidates_block"]
    ).format_candidates_block(res)
    assert "evidence used:" in block
    assert "text[" in block


def test_the_prompt_reports_the_components_it_used(catalog, monkeypatch):
    _patch_off(monkeypatch)
    res = _retrieve(catalog, "这个月的合同执行情况", monkeypatch)
    assert "text" in res.component_counts


def test_retrieval_is_deterministic(catalog, monkeypatch):
    _patch_off(monkeypatch)
    first = _retrieve(catalog, "这个月的合同执行情况", monkeypatch)
    second = _retrieve(catalog, "这个月的合同执行情况", monkeypatch)
    assert [c.table_name for c in first.candidates] == [c.table_name for c in second.candidates]
    assert [c.score for c in first.candidates] == [c.score for c in second.candidates]


# ── columns / metrics: the rest of the catalog's meaning ────────────────────

def test_a_question_term_reaching_a_column_scores_the_table(catalog, monkeypatch):
    _patch_off(monkeypatch)
    res = _retrieve(catalog, "统计发货数量", monkeypatch)
    entry = [c for c in res.candidates if c.table_name == "erp_t_sal_deliverynoticeentry"]
    assert entry, res.names[:8]
    assert "column" in entry[0].breakdown
    assert any(e.startswith("column[") for e in entry[0].evidence)


def test_a_measure_column_outweighs_an_unknown_one(catalog, monkeypatch):
    """``semantic_type`` is what makes a column match mean something."""
    _patch_off(monkeypatch)
    cat = catalog + [
        _entry("measured", zh="发货数量统计表",
               columns=[_col("qty", "measure", "发货数量", "吨")]),
        _entry("untyped", zh="发货数量统计表",
               columns=[_col("qty", "unknown", "发货数量")]),
    ]
    res = _retrieve(cat, "发货数量", monkeypatch)
    by_name = {c.table_name: c for c in res.candidates}
    assert "measured" in by_name and "untyped" in by_name
    assert by_name["measured"].score > by_name["untyped"].score


class _Term:
    def __init__(self, term, definition, aliases=()):  # noqa: D107
        self.term, self.definition, self.aliases = term, definition, aliases


class _GlossaryStore:
    """The only thing the fusion asks a store for, plus the two lookups."""

    def __init__(self, terms, aliases=None, domains=None):  # noqa: D107
        self._terms, self._aliases, self._domains = terms, aliases or {}, domains or []

    def rank_relations(self, question, k=25):  # noqa: D102
        return []

    def glossary(self, question, limit=8):  # noqa: D102
        return self._terms

    def relations(self):  # noqa: D102
        return []

    def domains(self):  # noqa: D102
        return self._domains


def test_a_verified_metric_definition_scores_the_table_it_names(catalog, monkeypatch):
    """VERIFIED vocabulary (a person wrote it) is fused into retrieval."""
    _patch_off(monkeypatch)
    store = _GlossaryStore([_Term("履约率", "履约率 = 实际发货数量 / 合同签订数量")])
    res = _retrieve(catalog, "本月的履约率是多少", monkeypatch, store=store)
    hit = [c for c in res.candidates if c.table_name == "erp_v_contract_execution"]
    assert hit, res.names[:8]
    assert "metric" in hit[0].breakdown
    assert any(e.startswith("metric:") for e in hit[0].evidence)


def test_overlay_aliases_are_part_of_the_text_evidence(catalog, monkeypatch):
    _patch_off(monkeypatch)
    from app.services.db.retrieval_evidence import build_documents

    docs = build_documents(catalog, aliases={"erp_t_crm_contract": ["合同台账"]}, domains=[])
    assert "合同台账" in docs["erp_t_crm_contract"]


def test_the_chinese_domain_name_is_used_as_vocabulary(catalog, monkeypatch):
    _patch_off(monkeypatch)
    from app.services.db.retrieval_evidence import build_documents

    domains = [{"name_en": SALES_DOMAIN, "name_zh": "销售与库存管理", "description": "sales"}]
    docs = build_documents(catalog, domains=domains)
    assert "销售与库存管理" in docs["erp_t_crm_contract"]


# ── the domain boost must be EARNED ────────────────────────────────────────

def test_a_domain_boost_needs_the_questions_vocabulary():
    from app.services.db.table_retrieval import domain_support

    auth = {"name_en": "User Authentication and Permissions", "name_zh": "用户认证与权限"}
    sales = {"name_en": "Product Sales and Pricing", "name_zh": "产品销售与定价"}
    assert domain_support("这个月的合同执行情况", auth) == []
    assert "销售" in domain_support("本周销售情况", sales)


def test_a_misscoped_domain_does_not_boost_auth_tables(catalog, monkeypatch):
    """Live, this produced the eight-table ``auth_*`` top-8."""
    import app.services.db.semantic_store as ss
    import app.services.db.plan_memory as pm
    import app.services.knowledge_graph.domain_discovery as dd

    auth_tables = {t["table_name"] for t in catalog if t["table_name"].startswith("auth")}

    def _boom(*_a, **_k):
        raise RuntimeError("no store")

    monkeypatch.setattr(ss, "get_semantic_store", _boom)
    monkeypatch.setattr(pm, "load_plan", lambda *a, **k: None)
    monkeypatch.setattr(
        dd, "scoped_tables_for_question",
        lambda *a, **k: (auth_tables, [{"name_en": "User Authentication and Permissions",
                                        "name_zh": "用户认证与权限"}]),
    )
    res = _retrieve(catalog, "这个月的合同执行情况", monkeypatch)
    assert not any("domain" in c.breakdown for c in res.candidates), [
        (c.table_name, c.breakdown) for c in res.candidates[:3]
    ]
    assert any("not scored" in n for n in res.notes), res.notes


def test_an_earned_domain_boost_is_recorded_as_evidence(catalog, monkeypatch):
    import app.services.db.semantic_store as ss
    import app.services.db.plan_memory as pm
    import app.services.knowledge_graph.domain_discovery as dd

    sales_tables = {"erp_v_sale_huizhou"}

    def _boom(*_a, **_k):
        raise RuntimeError("no store")

    monkeypatch.setattr(ss, "get_semantic_store", _boom)
    monkeypatch.setattr(pm, "load_plan", lambda *a, **k: None)
    monkeypatch.setattr(
        dd, "scoped_tables_for_question",
        lambda *a, **k: (sales_tables, [{"name_en": "Product Sales and Pricing",
                                         "name_zh": "产品销售与定价"}]),
    )
    res = _retrieve(catalog, "本周销售情况", monkeypatch)
    cand = [c for c in res.candidates if c.table_name == "erp_v_sale_huizhou"][0]
    assert "domain" in cand.breakdown
    assert any("matches 销售" in e for e in cand.evidence), cand.evidence


# ── the embedding component: bounded, gated, never the decider ─────────────

class _Embedder:
    """Deterministic stub: maps whole documents to fixed vectors by table name."""

    _DEFAULT = [0.1, 1.0, 0.0]

    def __init__(self, question, table_vectors, question_vector):  # noqa: D107
        self._q, self._tv, self._qv = question, table_vectors, question_vector

    def name(self):  # noqa: D102
        return "stub-embedder"

    def __call__(self, texts):  # noqa: D102
        out = []
        for t in texts:
            if t == self._q:
                out.append(self._qv)
                continue
            for key, vec in self._tv.items():
                if t.startswith(key):
                    out.append(vec)
                    break
            else:
                out.append(list(self._DEFAULT))
        return out


def _stub_embedder(question, top_table, top_sim=0.9):
    """One table is similar to the question; every other table is identical."""
    return _Embedder(
        question,
        {top_table: [top_sim, 1.0 - top_sim, 0.0]},
        [1.0, 0.0, 0.0],
    )


def test_embedding_similarity_is_bounded_and_reported(catalog, monkeypatch):
    _patch_off(monkeypatch)
    from app.services.db.retrieval_evidence import _W_EMBED

    emb = _stub_embedder("这个月的合同执行情况", "erp_t_crm_contract")
    res = _retrieve(catalog, "这个月的合同执行情况", monkeypatch,
                    embedder=emb, use_embeddings=True)
    scored = [c for c in res.candidates if "embedding" in c.breakdown]
    assert scored, [c.breakdown for c in res.candidates[:5]]
    for c in scored:
        assert c.breakdown["embedding"] <= _W_EMBED
        assert any(e.startswith("similarity ") for e in c.evidence)


def test_a_flat_similarity_distribution_is_declared_not_used(catalog, monkeypatch):
    """If the model has nothing to say, a normalised value would invent it."""
    _patch_off(monkeypatch)

    class _Flat(_Embedder):
        def __call__(self, texts):  # noqa: D102
            return [[0.5, 0.5, 0.0] for _t in texts]

    flat = _Flat("这个月的合同执行情况", {}, [0.5, 0.5, 0.0])
    res = _retrieve(catalog, "这个月的合同执行情况", monkeypatch,
                    embedder=flat, use_embeddings=True)
    assert not any("embedding" in c.breakdown for c in res.candidates)
    assert any("flat distribution" in n for n in res.notes), res.notes


def test_similarity_alone_never_creates_a_candidate(catalog, monkeypatch):
    """The embedding re-ranks; it does not discover. Measured: it is a tie-breaker."""
    _patch_off(monkeypatch)
    emb = _stub_embedder("这个月的合同执行情况", "aaa_noise_000")
    res = _retrieve(catalog, "这个月的合同执行情况", monkeypatch,
                    embedder=emb, use_embeddings=True)
    assert "aaa_noise_000" not in res.names


def test_an_unavailable_embedder_is_reported_not_faked(catalog, monkeypatch):
    _patch_off(monkeypatch)

    class _Broken:
        def name(self):  # noqa: D102
            return "broken"

        def __call__(self, texts):  # noqa: D102
            raise RuntimeError("no model")

    res = _retrieve(catalog, "这个月的合同执行情况", monkeypatch,
                    embedder=_Broken(), use_embeddings=True)
    assert not any("embedding" in c.breakdown for c in res.candidates)
    assert any("unavailable" in n for n in res.notes), res.notes


def test_document_vectors_are_reused_across_questions(catalog, monkeypatch):
    """Encoding 143 catalog descriptions per question would cost seconds."""
    from app.services.db import retrieval_evidence as re

    calls: list[int] = []

    class _Counting(_Embedder):
        def __call__(self, texts):  # noqa: D102
            calls.append(len(texts))
            return super().__call__(texts)

    re._EMBED_CACHE.clear()
    emb = _Counting("这个月的合同执行情况", {"erp_t_crm_contract": [1.0, 0.0, 0.0]}, [1.0, 0.0, 0.0])
    _patch_off(monkeypatch)
    _retrieve(catalog, "这个月的合同执行情况", monkeypatch, embedder=emb, use_embeddings=True)
    _retrieve(catalog, "本周销售情况", monkeypatch, embedder=emb, use_embeddings=True)
    doc_calls = [n for n in calls if n > 1]
    assert len(doc_calls) == 1, f"documents were encoded {len(doc_calls)} times: {calls}"


# ── the hard constraint: no business vocabulary in the ranker ──────────────

def _code_string_literals(path: Path) -> list[str]:
    """String literals in CODE, excluding docstrings.

    Docstrings are allowed (and required) to carry the measured examples; what
    must never appear is a Chinese table name, alias or business word in the
    logic itself — that is what makes the ranker work on a database nobody has
    configured.
    """
    tree = ast.parse(path.read_text())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    docstrings.add(id(body[0].value))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                out.append(node.value)
    return out


@pytest.mark.parametrize(
    "module",
    [
        "app/services/db/lexical.py",
        "app/services/db/retrieval_evidence.py",
        "app/services/db/table_retrieval.py",
    ],
)
def test_the_ranker_has_no_business_vocabulary(module):
    path = Path(__file__).resolve().parent.parent / module
    offenders = [s for s in _code_string_literals(path) if has_cjk(s)]
    assert not offenders, (
        f"{module} contains CJK vocabulary in code: {offenders[:5]} — Chinese must "
        f"be handled as n-grams with catalog-derived frequency, never as a word list"
    )


# ── coverage reroute: the verdict the pipeline measured but never acted on ──
#
# Measured live on 2026-09-20, question 惠州伊斯科本周销售情况，用图表展示:
#   rank 1  erp_v_sale_huizhou    score 5.73  max_date 2026-09-16  covers TRUE
#   chosen  forecast_accuracy_log              max_date 2026-09-05  covers FALSE
# The selector was free to pick any catalog table, and the ONLY check applied to
# its answer was "does this name exist". So a table the catalog itself had
# already convicted of ending 9 days before the asked-for week went to SQL,
# returned 0 rows, and the turn wrote an articulate "no data" answer instead of
# the sales figures. These pin the swap AND the bounds that keep it from
# becoming the filter that rule 1 forbids.


def _last_week() -> tuple[date, date]:
    return (date(2026, 9, 14), date(2026, 9, 20))


def _sales_catalog(**overrides) -> list[dict]:
    """The two tables from the live defect, plus the window that split them."""
    forecast = _entry(
        "forecast_accuracy_log",
        zh="预测准确率日志表，记录产品价格预测的准确率评估数据",
        en="Forecast accuracy log table, records price forecast accuracy",
        domain="Price Forecasting",
        min_date="2026-04-06", max_date="2026-09-05",
    )
    forecast.update(overrides.get("forecast", {}))
    sales = _entry(
        "erp_v_sale_huizhou",
        zh="ERP销售视图（惠州），展示惠州地区的销售数据。",
        en="ERP sales view (Huizhou), displays sales",
        domain="Product Sales and Pricing",
        min_date="2018-06-01", max_date="2026-09-16",
    )
    sales.update(overrides.get("sales", {}))
    return [sales, forecast]


def test_a_table_ending_before_the_window_is_swapped_for_the_ranked_coverer(monkeypatch):
    from app.services.db.table_retrieval import reroute_uncovered

    _patch_off(monkeypatch)
    toc = _sales_catalog()
    res = _retrieve(toc, "惠州伊斯科本周销售情况，用图表展示", monkeypatch,
                    window=_last_week())

    # The ranking was never the defect: the sales view leads, and the table the
    # selector actually chose carries no evidence at all — it was never a
    # candidate, it was picked out of the full catalog render because it EXISTS.
    assert res.names[0] == "erp_v_sale_huizhou", res.names[:4]
    assert "forecast_accuracy_log" not in res.names, res.names[:4]
    # …and the catalog's own window says it cannot hold the asked-for week.
    forecast_toc = [t for t in toc if t["table_name"] == "forecast_accuracy_log"][0]
    assert forecast_toc["max_date"] == "2026-09-05"

    selected, swaps = reroute_uncovered(["forecast_accuracy_log"], toc, res, _last_week())
    assert selected == ["erp_v_sale_huizhou"], selected
    assert swaps == [("forecast_accuracy_log", "erp_v_sale_huizhou")], swaps


def test_a_covering_selection_is_left_alone(monkeypatch):
    """The reroute is a REROUTE, not a filter — a good selection is untouched."""
    from app.services.db.table_retrieval import reroute_uncovered

    _patch_off(monkeypatch)
    toc = _sales_catalog()
    res = _retrieve(toc, "惠州伊斯科本周销售情况，用图表展示", monkeypatch,
                    window=_last_week())
    assert reroute_uncovered(["erp_v_sale_huizhou"], toc, res, _last_week()) == (
        ["erp_v_sale_huizhou"], [],
    )


def test_an_unknown_window_is_never_convicted(monkeypatch):
    """A missing measurement is reported as missing, never as 'empty'."""
    from app.services.db.table_retrieval import reroute_uncovered

    _patch_off(monkeypatch)
    toc = _sales_catalog(forecast={"max_date": None, "min_date": None})
    res = _retrieve(toc, "惠州伊斯科本周销售情况，用图表展示", monkeypatch,
                    window=_last_week())

    # No window on the question at all → nothing to convict.
    assert reroute_uncovered(["forecast_accuracy_log"], toc, res, None) == (
        ["forecast_accuracy_log"], [],
    )
    # Window asked for, catalog has no measurement for this table → also untouched.
    assert reroute_uncovered(["forecast_accuracy_log"], toc, res, _last_week()) == (
        ["forecast_accuracy_log"], [],
    )


def test_the_reroute_never_empties_the_selection(monkeypatch):
    """With nothing to swap in, the original selection stands.

    Dropping the convicted table would leave the stage with no schema at all —
    an empty selection is a worse failure than a query that honestly returns
    nothing, and the coverage disclosure already covers that case.
    """
    from app.services.db.table_retrieval import reroute_uncovered

    _patch_off(monkeypatch)
    toc = [_sales_catalog()[1]]  # the convicted table is the ONLY table
    res = _retrieve(toc, "惠州伊斯科本周销售情况，用图表展示", monkeypatch,
                    window=_last_week())

    selected, swaps = reroute_uncovered(["forecast_accuracy_log"], toc, res, _last_week())
    assert selected == ["forecast_accuracy_log"], selected
    assert swaps == []


def test_the_reroute_can_only_swap_in_a_ranked_candidate(monkeypatch):
    """The replacement is drawn from the candidates, so no invented table."""
    from app.services.db.table_retrieval import reroute_uncovered

    _patch_off(monkeypatch)
    toc = _sales_catalog()
    res = _retrieve(toc, "惠州伊斯科本周销售情况，用图表展示", monkeypatch,
                    window=_last_week())
    selected, swaps = reroute_uncovered(["forecast_accuracy_log"], toc, res, _last_week())
    for _dropped, added in swaps:
        assert added in res.names, (added, res.names)
    assert len(selected) == 1
