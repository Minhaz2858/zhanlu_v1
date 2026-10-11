"""Regression tests: goal-based answer delivery (2026-10-10).

Root-cause chain from a live run ("show me top 10 product for last month by
revenue" → answer itemized only the top 3 of 10 rows):

1. grounding_extractor._extract_nl2sql capped data lines at _TOP_ROWS=3 while
   the metadata lines carried sum/min/max over ALL rows — the FINALIZE
   narrator received the aggregates but only a 3-row prefix, so it honestly
   disclosed "ranks 4-10 not included in the returned extract".
2. classify_unit classified ANY ≤6-char question unit matched by BM25 as a
   metric — "last" (from "last month") became a fake unsupported metric slot,
   forcing complete=False and a false disclosure.
3. verifier._check_wrong_grain read obs.plan_node (nonexistent attribute) and
   crashed on every run, so the check never evaluated.
4. The FSM FINALIZE artifact gate suppressed a plan-deliberate sandbox
   deliverable because the user's message contained no format keyword.
"""
import types

import pytest

from app.services.db.grounding import (
    _TEMPORAL_QUALIFIER_RE,
    classify_unit,
    resolve_slots,
)
from app.services.synexia.grounding_extractor import (
    _ROW_LINE_MAX_CHARS,
    _TOP_ROWS,
    extract_grounding,
)
from app.services.synexia.verifier import _check_wrong_grain


# ── 1. grounding extractor: the narrator sees ALL rows ────────────────────


class _Obs:
    """Minimal nl2sql observation."""

    def __init__(self, rows):
        self.seq = 1
        self.success = True
        self.observation_type = "nl2sql"
        self.tool_name = "nl2sql"
        self.error_message = None
        self.artifact_ids = None
        self.result_data = {
            "sql": "SELECT material, SUM(revenue) AS revenue FROM v GROUP BY material",
            "data": rows,
            "columns": ["material", "revenue"],
        }


def _ten_rows():
    return [
        {"material": f"P{i:03d}", "revenue": 60_000_000 - i * 5_000_000}
        for i in range(10)
    ]


def test_extractor_itemizes_more_than_3_rows():
    grounding = extract_grounding([_Obs(_ten_rows())])
    for i in range(10):
        assert f"P{i:03d}" in grounding, (
            f"row P{i:03d} missing from the findings block — the FINALIZE "
            "narrator cannot itemize what it never receives"
        )


def test_extractor_row_line_fits_full_precision_figures():
    rows = [{"material": "戊烷发泡剂", "revenue": 60470530.93}]
    grounding = extract_grounding([_Obs(rows)])
    assert "60470530.93" in grounding


def test_extractor_top_rows_constant_raised():
    assert _TOP_ROWS >= 50
    assert _ROW_LINE_MAX_CHARS >= 400


def test_extractor_still_respects_global_cap():
    # A pathological 200-row dump stays bounded by SYNEXIA_GROUNDING_MAX_CHARS.
    from app.config import settings

    rows = [{"material": f"P{i:04d}", "revenue": i} for i in range(200)]
    grounding = extract_grounding([_Obs(rows)])
    assert len(grounding) <= settings.SYNEXIA_GROUNDING_MAX_CHARS


def test_extractor_aggregates_still_present():
    grounding = extract_grounding([_Obs(_ten_rows())])
    assert "sum=" in grounding and "min=" in grounding and "max=" in grounding


# ── 2. temporal qualifiers are never fake metric slots ────────────────────


@pytest.mark.parametrize(
    "word",
    ["last", "month", "top", "first", "previous", "latest", "year", "week",
     "上个月", "本月", "最近"],
)
def test_temporal_qualifiers_classified_as_temporal(word):
    assert _TEMPORAL_QUALIFIER_RE.match(word)
    assert classify_unit(word) == "temporal"


def test_business_word_still_metric():
    # ≤6 chars, identifier-shaped, NOT a temporal qualifier → still metric.
    assert classify_unit("revenue") == "metric"


def _fake_concept(table="erp_v_sale_orderentry", column="famount", desc="revenue amount"):
    return {"table": table, "column": column, "description": desc}


class _Term:
    def __init__(self, term):
        self.term = term
        self.definition = ""


def test_resolve_slots_no_unsupported_slot_for_last_month_question():
    """The live failure: 'last' from 'last month by revenue' matched a catalog
    concept and became an unsupported metric slot → complete=False."""
    question = "show me top 10 product for last month by revenue"
    concepts = [_fake_concept(desc="revenue amount")]
    evidence, slots, metrics = resolve_slots(
        question,
        concepts=concepts,
        selected_tables={"erp_v_sale_orderentry"},
    )
    unsupported_labels = {s["slot"] for s in slots}
    assert "last" not in unsupported_labels
    assert "month" not in unsupported_labels


def test_entity_word_matching_table_token_is_not_a_metric():
    """Live follow-up run: 'product' (a token of report_product_catalog)
    became the NEXT fake unsupported metric slot after 'last' was fixed.
    Entity words derived from the source's own table names are not
    measurements."""
    assert classify_unit("product", table_names={"report_product_catalog"}) == "table"
    assert classify_unit("customer", table_names={"crm_customer_master"}) == "table"


# ── 3. verifier wrong_grain: no crash, resolves grain from plan nodes ─────


class _ObsNoPlanNode:
    def __init__(self, rows, node_id="node-1"):
        self.id = "obs-1"
        self.success = True
        self.result_data = {"rows": rows}
        self.plan_node_id = node_id
        # NOTE: deliberately NO `plan_node` attribute — that is the live crash.


def test_wrong_grain_does_not_crash_without_plan_node_attr():
    obs = _ObsNoPlanNode([{"a": i} for i in range(300)])
    check = _check_wrong_grain([obs])  # must not raise
    assert check["check"] == "wrong_grain"
    assert check["ok"] is False  # 300 raw rows, no aggregate expectation


def test_wrong_grain_uses_expected_grain_from_plan_nodes():
    node = types.SimpleNamespace(id="node-1", expected_grain="aggregate")
    obs = _ObsNoPlanNode([{"a": i} for i in range(100)])
    check = _check_wrong_grain([obs], [node])
    assert check["ok"] is False  # aggregate expected, 100 rows returned


def test_wrong_grain_ok_for_raw_grain():
    node = types.SimpleNamespace(id="node-1", expected_grain="raw")
    obs = _ObsNoPlanNode([{"a": i} for i in range(100)])
    check = _check_wrong_grain([obs], [node])
    assert check["ok"] is True


# ── 4. FSM finalize gate: plan-deliberate deliverables ship ───────────────


class _Plan:
    def __init__(self, nodes):
        self.nodes = nodes


class _Node:
    def __init__(self, node_type):
        self.node_type = node_type


class _SandboxObs:
    observation_type = "sandbox"
    success = True


class _FailedSandboxObs:
    observation_type = "sandbox"
    success = False


def test_plan_sandbox_node_counts_as_deliberate_deliverable():
    from app.services.synexia.fsm import _plan_deliberate_deliverable

    assert _plan_deliberate_deliverable(_Plan([_Node("nl2sql"), _Node("sandbox")]), [])


def test_successful_sandbox_observation_counts():
    from app.services.synexia.fsm import _plan_deliberate_deliverable

    assert _plan_deliberate_deliverable(None, [_SandboxObs()])


def test_failed_sandbox_observation_does_not_count():
    from app.services.synexia.fsm import _plan_deliberate_deliverable

    assert not _plan_deliberate_deliverable(None, [_FailedSandboxObs()])


def test_no_sandbox_anywhere_does_not_count():
    from app.services.synexia.fsm import _plan_deliberate_deliverable

    assert not _plan_deliberate_deliverable(_Plan([_Node("nl2sql")]), [])


# ── 5. ReAct path: condensed summary carries the full itemization ─────────


def test_condensed_row_text_beyond_5_rows():
    from app.services.tool_handlers.delegation_tools import _condensed_row_text

    rows = [{"code": f"2025000{i}", "name": f"产品{i}", "revenue": 60_000_000 - i * 5_000_000} for i in range(10)]
    text = _condensed_row_text(rows)
    for i in range(10):
        assert f"产品{i}" in text


# ── 6. No deterministic veto of correct concise answers ───────────────────


def test_short_numeric_answer_is_not_replaced_by_fallback():
    """Live failure: '¥165.03M, up 5.7%' was <100 chars and got REPLACED by
    the canned auto-generated table. A short answer carrying a figure is a
    real data answer — the goal-based rule keeps it."""
    from app.routers.agents import _should_trigger_fallback

    assert not _should_trigger_fallback("¥165.03M, up 5.7%.", [{"v": 1}])
    assert not _should_trigger_fallback("Total: 42 records, 3.2M CNY.", [{"v": 1}])


def test_short_figure_free_filler_still_falls_back():
    from app.routers.agents import _should_trigger_fallback

    assert _should_trigger_fallback("Done.", [{"v": 1}])
    assert _should_trigger_fallback("Here you go.", [{"v": 1}])


def test_apology_and_empty_still_fall_back():
    from app.routers.agents import _should_trigger_fallback

    assert _should_trigger_fallback("", [{"v": 1}])
    assert _should_trigger_fallback("I apologize, but I couldn't find it.", [{"v": 1}])
    assert not _should_trigger_fallback("anything", [])  # no data → no fallback


def test_deterministic_fallback_has_no_hardcoded_currency():
    from app.routers.agents import _build_deterministic_fallback

    text = _build_deterministic_fallback(
        [{"product": "A", "revenue": 100.0}, {"product": "B", "revenue": 200.0}],
        ["product", "revenue"],
    )
    assert "¥" not in text
    assert "300.00" in text  # total still computed


# ── 7. No user file signal → no file deliverable (user goal is authoritative)


def test_sandbox_stripped_without_file_intent():
    """2026-10-10 round 2 trusted data-fed sandbox steps as 'the planner's
    deliberate deliverable' — the SAME DAY 'show last week sales data'
    (TaskSpec artifacts=[], zero file intent) got an unrequested xlsx because
    the LLM planner habitually over-produces format steps. The USER's file
    signal is the goal: no file intent → ALL sandbox steps stripped."""
    from app.services.synexia.plan_dag import _strip_sandbox_without_file_intent

    steps = [
        {"node_type": "nl2sql", "name": "q", "dependencies": []},
        {"node_type": "sandbox", "name": "xlsx", "dependencies": [0]},
    ]
    out = _strip_sandbox_without_file_intent(steps, {})
    assert [s["node_type"] for s in out] == ["nl2sql"]


def test_explicit_file_intent_keeps_sandbox():
    from app.services.synexia.plan_dag import _strip_sandbox_without_file_intent

    steps = [
        {"node_type": "nl2sql", "name": "q", "dependencies": []},
        {"node_type": "sandbox", "name": "xlsx", "dependencies": [0]},
    ]
    out = _strip_sandbox_without_file_intent(
        steps, {"artifact_intents": ["xlsx"]}
    )
    assert [s["node_type"] for s in out] == ["nl2sql", "sandbox"]


def test_generate_plan_creates_nodes_after_strip():
    """CRITICAL ordering bug (live: 'show last week sales data' logged
    'stripped 1 sandbox' yet the xlsx was still generated): PlanNode records
    were created BEFORE the sanitize/strip/decomposition ran, so the DB held
    the stripped steps anyway. The strip call must precede PlanNode creation
    in generate_plan's source."""
    import inspect

    from app.services.synexia import plan_dag

    src = inspect.getsource(plan_dag.generate_plan)
    assert src.index("_strip_sandbox_without_file_intent(plan_steps") < src.index(
        "for i, step in enumerate(plan_steps):"
    ), "PlanNode creation must run AFTER the sandbox strip"


# ── 8. Garbage classifier is fail-open for unknown domains ────────────────


def test_unknown_domain_data_is_not_garbage():
    """The old noun list (_BUSINESS_COL_FRAGMENTS: revenue, amount, …)
    convicted legitimate data from any domain outside it (healthcare,
    education, IoT). Fail-open now."""
    from app.services.tool_handlers.delegation_tools import (
        _is_garbage_business_data,
        _rows_have_business_data,
    )

    healthcare = [{"patient_name": "Zhang", "diagnosis": "flu", "bp": "120/80"}]
    assert not _is_garbage_business_data(healthcare)
    assert _rows_have_business_data(healthcare)

    cjk = [{"产品名称": "戊烷发泡剂", "收入": 100.0}]
    assert not _is_garbage_business_data(cjk)
    assert _rows_have_business_data(cjk)


def test_all_id_rows_are_still_garbage():
    from app.services.tool_handlers.delegation_tools import (
        _is_garbage_business_data,
        _rows_have_business_data,
    )

    ids = [{"FID": 1, "FENTRYID": 2}]
    assert _is_garbage_business_data(ids)
    assert not _rows_have_business_data(ids)


# ── 9. Fast mode is opt-in, not a silent default governor ─────────────────


def test_fast_mode_disabled_by_default():
    """Fast mode ON by default silently capped every turn at 10 tool
    iterations and disabled figure-grounding verify nudges — a hard-coded
    speed-over-quality governor. It must be opt-in."""
    from app.config import settings

    assert settings.AGENT_FAST_MODE_ENABLED is False


# ── 10. Verifier round 2: rows/data key mismatch, question-blind checks ───


def test_verifier_accepts_nl2sql_data_key():
    """CRITICAL live bug: the FSM nl2sql writer stores rows under `data` while
    the verifier read only `rows` — all three row-based checks silently
    skipped EVERY nl2sql observation, and coverage flagged every data node
    'uncovered' (critical) on healthy runs."""
    from app.services.synexia.verifier import _obs_rows

    assert _obs_rows({"data": [{"a": 1}]}) == [{"a": 1}]
    assert _obs_rows({"rows": [{"a": 1}]}) == [{"a": 1}]
    assert _obs_rows({"sql": "SELECT 1"}) is None


class _DataObs:
    def __init__(self, rd, node_id="n1", success=True):
        self.id = "obs-1"
        self.success = success
        self.result_data = rd
        self.plan_node_id = node_id


def test_degenerate_check_not_fired_for_count_intent_question():
    """A count-intent answer ({'total_lot_count': 16143} for 'how many...')
    was flagged degenerate (critical) because is_metadata_only_rows was
    called question-blind."""
    from app.services.synexia.verifier import _check_degenerate_result

    obs = _DataObs({"data": [{"total_lot_count": 16143}]})
    check = _check_degenerate_result([obs], question="how many lots do we have")
    assert check["ok"] is True


def test_degenerate_check_still_fires_for_metadata_only():
    from app.services.synexia.verifier import _check_degenerate_result

    obs = _DataObs({"data": [{
        "MIN_DATE": "2026-01-01", "MAX_DATE": "2026-09-30", "ENTRY_COUNT": 5,
    }]})
    check = _check_degenerate_result([obs], question="show me top products")
    assert check["ok"] is False


def test_coverage_only_expects_data_nodes():
    """synthesize/sandbox nodes were expected to produce rows (phantom
    produces_data default True) and coverage flagged them 'uncovered'."""
    from app.services.synexia.verifier import _check_coverage

    nodes = [
        types.SimpleNamespace(id="n1", node_type="nl2sql"),
        types.SimpleNamespace(id="n2", node_type="synthesize"),
        types.SimpleNamespace(id="n3", node_type="sandbox"),
    ]
    obs = [_DataObs({"data": [{"a": 1}]}, node_id="n1")]
    check = _check_coverage(obs, nodes)
    assert check["ok"] is True


# ── 11. Confidence: skipped nodes don't drag the score down ───────────────


def test_skipped_nodes_excluded_from_plan_completion():
    from types import SimpleNamespace as NS

    from app.services.synexia.confidence_scorer import compute_confidence

    plan = NS(nodes=[
        NS(status="completed"), NS(status="completed"),
        NS(status="skipped"), NS(status="skipped"),
    ])
    execution = NS(observations=[], artifact_ids=[], policy_decision={},
                   confidence_factors={})
    score, factors = compute_confidence(execution, plan)
    assert factors["plan_completion"]["score"] == 1.0  # 2/2, not 2/4=0.5


# ── 12. CSV file intent is detected everywhere ────────────────────────────


def test_csv_intent_detected():
    from app.services.synexia.intent_router import detect_file_intent

    assert detect_file_intent("export this as csv") == "csv"
    assert detect_file_intent("给我一个csv文件") == "csv"


def test_file_intent_keywords_cover_csv():
    from app.services.synexia.fsm import _contains_file_intent

    assert _contains_file_intent("give me a csv of the top products")
    assert _contains_file_intent("导出表格")


# ── 13. Trivial-probe no longer convicts full-table answers ───────────────


def test_full_table_dump_is_not_a_probe():
    from app.services.tool_handlers.delegation_tools import _is_trivial_probe

    assert not _is_trivial_probe("SELECT * FROM departments")
    assert _is_trivial_probe("SELECT * FROM products LIMIT 5")
    assert not _is_trivial_probe("SELECT material, SUM(FAMOUNT) FROM v GROUP BY material")


# ── 14. Display regexes no longer eat ordinary words ──────────────────────


def test_sql_hint_pattern_keeps_ordinary_words():
    from app.routers.agents import _strip_sql_hints

    out = _strip_sql_hints("Show Ford F-150 sales by FENTRYID")
    assert "Ford" in out          # ordinary word kept (IGNORECASE removed)
    assert "F-150" in out
    assert "FENTRYID" not in out  # ERP internal ID still stripped


# ── 15. Quality markers no longer flag correct answers ────────────────────


def test_bounce_markers_not_fired_by_substantive_answer():
    from app.services.synexia.quality_eval import evaluate_quality

    text = (
        "I retrieved 42 rows and the top driver was 戊烷发泡剂. "
        "Let me know if you want a breakdown by region."
    )
    result = evaluate_quality(
        user_message="top products", assistant_text=text, task_spec={},
    )
    assert result.verdict != "revise"


# ── 16. FSM tool_calls include sandbox + nl2sql observations ──────────────


class _SandboxObs:
    def __init__(self):
        self.observation_type = "sandbox"
        self.tool_name = "build_top_products_html"
        self.success = True
        self.request_args = {"artifact_type": "html"}
        self.result_data = {
            "artifact_id": "art-123",
            "format": "html",
            "sandbox_result": {
                "artifact_id": "art-123",
                "title": "Top 10 Products",
                "type": "html",
                "file_url": "/files/art-123.html",
                "preview_url": "/preview/art-123",
                "file_name": "top10.html",
                "mime_type": "text/html",
                "has_preview": True,
            },
        }
        self.artifact_ids = ["art-123"]


class _Nl2SqlObs:
    def __init__(self):
        self.observation_type = "nl2sql"
        self.tool_name = "nl2sql"
        self.success = True
        self.request_args = {"question": "top 10"}
        self.result_data = {
            "sql": "SELECT ...",
            "data": [{"a": 1}],
            "source_id": "kb-1",
            "source_name": "ERP Warehouse",
        }
        self.artifact_ids = None


def test_fsm_tool_calls_include_sandbox_observation():
    """Live: 'give me in html file' created the artifact but the FSM
    tool_calls collector dropped sandbox observations, so the chat loop
    could never collect artifact payloads — no inline card rendered."""
    from app.services.synexia.fsm import _collect_frontend_tool_calls

    tcs = _collect_frontend_tool_calls([_SandboxObs()])
    assert len(tcs) == 1
    tc = tcs[0]
    assert tc["name"] == "run_sandbox_skill"  # canonical collector name
    assert tc["success"] is True
    assert tc["results"]["artifact_id"] == "art-123"
    assert tc["results"]["title"] == "Top 10 Products"
    assert tc["results"]["file_url"] == "/files/art-123.html"


def test_fsm_tool_calls_include_nl2sql_citations():
    from app.services.synexia.fsm import _collect_frontend_tool_calls

    tcs = _collect_frontend_tool_calls([_Nl2SqlObs()])
    assert tcs[0]["name"] == "nl2sql"
    assert tcs[0]["results"]["source_name"] == "ERP Warehouse"


def test_artifact_collector_picks_up_fsm_sandbox_entry():
    """The chat loop's _collect_artifact_results must recognize the FSM
    sandbox entry shape — this is what produces message.artifacts."""
    from app.routers.agents import _collect_artifact_results
    from app.services.synexia.fsm import _collect_frontend_tool_calls

    tcs = _collect_frontend_tool_calls([_SandboxObs()])
    arts = _collect_artifact_results(tcs, None, "conv-1", None)
    assert len(arts) == 1
    assert arts[0]["artifact_id"] == "art-123"
    assert arts[0]["type"] == "html"
    assert arts[0]["title"] == "Top 10 Products"


def test_coverage_counts_artifact_producing_tool_node():
    """A tool node that produced a file (no rows) must not be 'uncovered'."""
    from app.services.synexia.verifier import _check_coverage

    nodes = [
        types.SimpleNamespace(id="n1", node_type="nl2sql"),
        types.SimpleNamespace(id="n2", node_type="tool"),
    ]
    obs = [
        _DataObs({"data": [{"a": 1}]}, node_id="n1"),
        _DataObs({"artifact_id": "art-9"}, node_id="n2"),
    ]
    check = _check_coverage(obs, nodes)
    assert check["ok"] is True


# ── 17. De-hardcoded turn/planner budgets (2026-10-10) ────────────────────


def test_turn_budget_settings_exist_with_new_defaults():
    from app.config import settings

    assert settings.TURN_WALL_CLOCK_CAP_S == 600.0
    assert settings.REPORT_TURN_WALL_CLOCK_CAP_S == 900.0
    assert settings.SYNTHESIS_TIMEOUT_S == 180.0
    assert settings.NL2SQL_PLANNER_TIMEOUT_S == 120.0
    assert settings.NL2SQL_PLANNER_MAX_TOKENS == 2000
    assert settings.LLM_RETRY_MAX_ATTEMPTS == 5
    assert settings.DELEGATE_ITERATIONS_HARD_CLAMP == 25
    # container env may raise it further; assert the new floor
    assert settings.LLM_MAX_TOKENS_HARD_CAP >= 16384
    assert settings.TOOL_MAX_OUTPUT_CHARS == 50000
    assert settings.DEEP_RESEARCH_TIMEOUT_S == 300
    assert settings.LLM_CONNECT_TIMEOUT_S == 10.0
    assert settings.LLM_READ_TIMEOUT_S == 300.0


def test_agents_loop_cap_no_longer_a_60s_literal():
    """The #1 turn-killer: a plain 60.0 literal capped the main v3 loop —
    less than one LLM call. It must read from settings now."""
    import inspect

    from app.routers import agents

    src = inspect.getsource(agents)
    # the old literals must be gone from the wall-clock selection block
    assert "else 60.0" not in src
    assert "settings.TURN_WALL_CLOCK_CAP_S" in src
    assert "settings.REPORT_TURN_WALL_CLOCK_CAP_S" in src


def test_llm_retry_respects_settings_budget():
    from types import SimpleNamespace

    from app.services.llm_retry import max_attempts_for

    ce = SimpleNamespace(max_retries=9)
    assert max_attempts_for(ce) == 5  # settings cap, was literal 3


def test_delegate_clamp_settings_backed():
    from types import SimpleNamespace

    from app.config import settings

    assert settings.DELEGATE_ITERATIONS_HARD_CLAMP == 25
    # the clamp constant is honored (smoke: min() semantics)
    assert min(50, settings.DELEGATE_ITERATIONS_HARD_CLAMP) == 25


def test_dead_delegate_timeout_config_removed():
    from app.config import settings

    # the dead constant itself is gone (a comment mentioning the removal is fine)
    assert not hasattr(settings, "DELEGATE_TIMEOUT")


# ── 18. Export without a synthesize node still delivers the file ──────────


class _FakeObservation:
    def __init__(self, otype, rd):
        self.observation_type = otype
        self.success = True
        self.result_data = rd


def test_export_without_synthesize_node_builds_from_data_rows(monkeypatch):
    """Live: 'give me in html file' planned nl2sql only — no synthesize
    observation — and fsm_finalize_into_artifact returned None, so the user's
    EXPLICIT file request produced nothing while the log claimed success."""
    from app.services.synexia import finalize as fin

    captured = {}

    def _fake_finalize(db, **kwargs):
        captured["payload"] = kwargs.get("payload")
        return (types.SimpleNamespace(id="art-1"), {"html": {"artifact_id": "art-1"}})

    monkeypatch.setattr(fin, "finalize_into_artifact", _fake_finalize)

    obs = [_FakeObservation("nl2sql", {
        "data": [{"product": "A", "revenue": 100}],
        "sql": "SELECT 1",
        "source_name": "ERP",
    })]
    result = fin.fsm_finalize_into_artifact(
        db=None,
        conversation_id="conv",
        agent_name="general_assistant",
        user_message="show top 5 product and give me in html file",
        observations=obs,
        task_spec={"user_signal": "export_html", "entities": {}},
    )
    assert result is not None
    artifact, fe, payload = result
    assert artifact.id == "art-1"
    assert fe["html"]["artifact_id"] == "art-1"
    assert payload.title  # built from the user message


def test_export_with_no_data_at_all_returns_none():
    from app.services.synexia import finalize as fin

    obs = [_FakeObservation("tool_call", {"result": "nothing"})]
    result = fin.fsm_finalize_into_artifact(
        db=None,
        conversation_id="conv",
        agent_name="general_assistant",
        user_message="export",
        observations=obs,
        task_spec={"user_signal": "export_html"},
    )
    assert result is None  # honest: nothing to build from


# ── 19. OCI round 2: aggregation restructuring + mixed-mode planning ──────


def test_having_pushdown_moves_non_aggregate_conjunct():
    """OCI optimization stage: a HAVING conjunct with no aggregate that only
    references grouped columns filters BEFORE grouping — deterministic and
    semantics-preserving."""
    from app.services.db.sql_rewriter import rewrite_sql

    sql = (
        "SELECT dep, SUM(FAMOUNT) FROM orders GROUP BY dep "
        "HAVING dep = 'guangdong' AND SUM(FAMOUNT) > 100"
    )
    r = rewrite_sql(sql)
    up = r.sql.upper()
    assert "WHERE DEP = 'guangdong'" in up or "where dep = 'guangdong'" in r.sql.lower()
    assert "HAVING SUM(FAMOUNT) > 100" in up or "having SUM(FAMOUNT) > 100" in r.sql
    assert any("having_pushdown" in n for n in r.notes)


def test_having_pushdown_keeps_aggregate_conjuncts():
    from app.services.db.sql_rewriter import rewrite_sql

    sql = "SELECT dep, SUM(a) FROM t GROUP BY dep HAVING SUM(a) > 5"
    r = rewrite_sql(sql)
    assert "HAVING" in r.sql.upper()
    assert not any("having_pushdown" in n for n in r.notes)


def test_glossary_definitional_hit_helper():
    """Mixed-mode planning: a definitional question that the VERIFIED
    glossary can answer routes as a data turn (semantic lookup instead of a
    web search)."""
    from app.routers.agents import _glossary_definitional_hit

    # The authored metric 'net_revenue'/'net amount' (test 18 wrote it) or
    # terminologies may or may not exist in this DB — assert shape only.
    result = _glossary_definitional_hit("what is net revenue", [])
    assert isinstance(result, bool)
    assert _glossary_definitional_hit("show top 5 products", []) is False
