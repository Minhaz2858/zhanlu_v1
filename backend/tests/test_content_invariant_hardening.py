"""Security-hardening follow-up: content-invariant sanitizer (bare view names).

Adversarial reproduction tests for the sanitizer gap found in P2 Phase 4:
``_LIVE_EVENT_FORBIDDEN_RE`` covered ``erp_[tv]_*`` prefixed identifiers but
never bare database view names (``v_sale_order``). These tests pin the FULL
required behavior: view names scrubbed everywhere they can reach a user,
legitimate business language preserved, nested payloads handled, and the
dynamic schema-metadata supplement working.

Run: pytest tests/test_content_invariant_hardening.py -q
"""
import json

from app.services.agent_loop.sse_builders import (
    _LIVE_EVENT_SAFE_FALLBACK,
    _sanitize_live_event_params,
    register_sensitive_identifiers,
)


def _one(value):
    """Sanitize a single scalar through the real payload path."""
    return _sanitize_live_event_params({"v": value})["v"]


# ── 1. Bare view identifier ──────────────────────────────────────────────────
def test_bare_view_name_scrubbed():
    assert _one("v_sale_order") == _LIVE_EVENT_SAFE_FALLBACK


def test_bare_view_name_single_word_scrubbed():
    assert _one("v_inventory") == _LIVE_EVENT_SAFE_FALLBACK


def test_uppercase_view_name_scrubbed():
    assert _one("V_PURCHASE_ORDER") == _LIVE_EVENT_SAFE_FALLBACK


# ── 2. Schema-qualified identifier ───────────────────────────────────────────
def test_schema_qualified_view_name_scrubbed():
    assert _one("sales.v_sale_order") == _LIVE_EVENT_SAFE_FALLBACK


# ── 3. SQL fragment (keyword-free, so only the view name can trip it) ───────
def test_view_name_inside_keyword_free_fragment_scrubbed():
    assert _one("rows from v_sale_order for October") == _LIVE_EVENT_SAFE_FALLBACK


# ── 4. Nested SSE payload ────────────────────────────────────────────────────
def test_identifier_deep_in_nested_sse_payload_scrubbed():
    params = {
        "tool_label": "describe_schema",
        "meta": {"source": {"tables": ["v_production_order"], "n": 3}},
    }
    out = _sanitize_live_event_params(params)
    assert out["meta"]["source"]["tables"] == [_LIVE_EVENT_SAFE_FALLBACK]
    assert out["meta"]["source"]["n"] == 3  # structure + scalars preserved


def test_json_structure_preserved_roundtrip():
    params = {"a": {"b": [1, "v_sale_order", {"c": "ok"}]}, "d": None}
    out = _sanitize_live_event_params(params)
    # Still valid JSON with identical shape (identities, not indices).
    assert json.loads(json.dumps(out))["a"]["b"][2] == {"c": "ok"}


def test_sanitizer_is_pure_input_not_mutated():
    params = {"nested": {"v": "v_sale_order"}}
    _sanitize_live_event_params(params)
    assert params["nested"]["v"] == "v_sale_order"  # deep-copied output only


# ── 5. Chinese/English mixed payload ─────────────────────────────────────────
def test_chinese_english_mixed_with_view_name_scrubbed():
    assert _one("查询v_sale_order的十月销量数据") == _LIVE_EVENT_SAFE_FALLBACK


def test_plain_chinese_without_identifiers_untouched():
    assert _one("查询十月的销量数据") == "查询十月的销量数据"


# ── 6. Legitimate business terminology preserved ────────────────────────────
def test_ordinary_business_language_untouched():
    for s in (
        "Sales revenue grew 12% in Q4",
        "Inventory turnover improved across 3 regions",
        "Purchase orders pending approval: 7",
        "v2 launch plan approved",
    ):
        assert _one(s) == s, s


def test_no_substring_corruption_of_words_containing_v():
    # \b anchoring: 'revenue', 'over_value', 'CV_sale' must not match.
    for s in ("revenue", "over_value", "CV_sale", "delivery_van"):
        assert _one(s) == s, s


# ── 7. Approved metric definition preserved ─────────────────────────────────
def test_metric_definition_name_untouched():
    assert _one("gross_margin_rate") == "gross_margin_rate"
    assert _one("net_revenue") == "net_revenue"


# ── 8+10. Error-message sanitization (execution-tree response path) ─────────
# Covered in tests/test_p3_execution_tree.py (needs the AgentInvocation DB
# fixtures defined there):
#   test_invocation_error_message_sanitized_in_tree
#   test_block_reason_sanitized_in_tree


# ── 9. Evidence REST response ────────────────────────────────────────────────
def test_evidence_rest_rows_excluded_and_no_credentials():
    from app.services.agent_collaboration import shared_evidence as se
    from app.routers.executions import _serialize_evidence

    eid = se.provide_evidence(
        "conv-sec-ev", "QA Agent", source_id="ds-1",
        query_provenance={"sql": "SELECT 1", "rows": [{"secret": 1}]},
        findings={"on_hand": 42},
    )
    d = _serialize_evidence(se.get_evidence("conv-sec-ev", eid))
    assert "rows" not in d["query_provenance"]
    # No credential-bearing fields exist on the wire contract at all.
    forbidden = {"password", "api_key", "secret", "credential", "token",
                 "connection_string", "dsn"}
    assert not (forbidden & set(d.keys()))
    assert not any(f in json.dumps(d).lower() for f in forbidden)


# ── Dynamic schema-metadata supplement (not a hardcoded list alone) ──────────
def test_dynamic_identifier_registered_then_scrubbed():
    # Before registration: a snake_case table name is indistinguishable from
    # prose and must survive (no corruption without evidence).
    assert _one("sales_summary rows look healthy") == "sales_summary rows look healthy"
    added = register_sensitive_identifiers(["sales_summary"])
    assert added >= 1
    assert _one("sales_summary rows look healthy") == _LIVE_EVENT_SAFE_FALLBACK
    # Substring safety still holds after registration.
    assert _one("resales_summaryx") == "resales_summaryx"


def test_dynamic_registration_requires_snake_case_evidence():
    # Plain English words must NOT become scrub targets even if they appear
    # as schema names — otherwise prose like "3 users found" is corrupted.
    register_sensitive_identifiers(["users", "orders"])
    assert _one("3 users found") == "3 users found"
    assert _one("purchase orders pending") == "purchase orders pending"


def test_dynamic_registration_ignores_junk():
    assert register_sensitive_identifiers([None, 123, "ab", "", "  "]) == 0


# ── Plan-step SSE frames: LLM-authored titles are model text, not template
#    metadata — they must pass the same content invariant (closure audit) ────
def _step(title):
    from app.services.turn_planner import TurnStep

    return TurnStep(step_index=0, key="act", phase="act",
                    title_en=title, title_zh=title)


def test_plan_step_added_frame_scrubs_llm_authored_identifier():
    from app.services.turn_planner import plan_step_added_frame
    import json as _json

    frame = plan_step_added_frame(_step("Summarize v_sale_order by region"))
    payload = _json.loads(frame[len("data: "):].strip())
    assert payload["title"] == _LIVE_EVENT_SAFE_FALLBACK


def test_plan_step_completed_frame_scrubs_llm_authored_identifier():
    from app.services.turn_planner import plan_step_completed_frame
    import json as _json

    frame = plan_step_completed_frame(_step("查询erp_t_sal_outstock的数据"))
    payload = _json.loads(frame[len("data: "):].strip())
    assert payload["title"] == _LIVE_EVENT_SAFE_FALLBACK


def test_plan_step_frames_preserve_ordinary_titles():
    from app.services.turn_planner import plan_step_added_frame
    import json as _json

    frame = plan_step_added_frame(_step("Query the bound data source"))
    payload = _json.loads(frame[len("data: "):].strip())
    assert payload["title"] == "Query the bound data source"


def test_plan_step_sites_do_not_bypass_scrubber():
    """Source contract: the persisted plan-step pushes must use the default
    sanitize path (the old sanitize=False premise treated LLM-authored titles
    as template metadata — they are not)."""
    import os

    agents_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "app", "routers", "agents.py",
    )
    with open(agents_path, "r", encoding="utf-8") as f:
        src = f.read()
    for site in ('"plan_step_added", "plan_step_added"',):
        idx = src.find(site)
        assert idx != -1
        block = src[idx: idx + 300]
        assert "sanitize=False" not in block, \
            "plan-step persisted events must go through the scrubber"


# ── Dynamic registration: normalization ──────────────────────────────────────
def test_registration_normalizes_quoted_and_qualified_names():
    added = register_sensitive_identifiers(
        ["`sales_summary`", '"Sales_Summary"', "sales.sales_summary"]
    )
    # Every variant must resolve to the same scrub target; duplicates in the
    # store mean only the first added.
    assert added >= 1
    assert _one("sales_summary rows look healthy") == _LIVE_EVENT_SAFE_FALLBACK


def test_registration_strips_backticks_from_qualified_name():
    register_sensitive_identifiers(["`sales`.`inventory_snapshot`"])
    assert _one("inventory_snapshot trend") == _LIVE_EVENT_SAFE_FALLBACK


# ── Dynamic registration: isolation (safe direction proof) ──────────────────
def test_registration_is_global_over_blocking_not_per_tenant_leaky():
    """The registry is deliberately process-global: a name registered from
    datasource A's describe also scrubs datasource B's / tenant B's events.
    This is OVER-BLOCKING (safe direction) — there is no per-tenant allowance
    that could ever under-block a cross-tenant leak."""
    register_sensitive_identifiers(["warehouse_a_summary"])
    # "Tenant B" payload with the same name is also scrubbed — fail-safe.
    assert _one("warehouse_a_summary totals") == _LIVE_EVENT_SAFE_FALLBACK


# ── Dynamic registration: cap enforcement + observability ────────────────────
def test_registration_cap_enforced_and_observable(caplog):
    import logging

    names = [f"auto_table_{i:05d}_col" for i in range(2100)]
    with caplog.at_level(logging.WARNING, logger="app.services.agent_loop.sse_builders"):
        register_sensitive_identifiers(names)
    # Cap reached → skipped names are logged, not silently dropped.
    assert any("cap" in r.getMessage().lower() for r in caplog.records)
    # Registered names still scrub; a name beyond the cap is NOT registered
    # (documented limitation — the static patterns still apply to it).
    assert _one("auto_table_00001_col totals") == _LIVE_EVENT_SAFE_FALLBACK


# ── Dynamic registration: concurrency ────────────────────────────────────────
def test_concurrent_registration_and_sanitization_thread_safe():
    import threading

    errs: list[Exception] = []

    def _worker(base: int) -> None:
        try:
            for i in range(60):
                register_sensitive_identifiers([f"thr{base}_table_{i:03d}_x"])
                _one(f"thr{base}_table_{i:03d}_x totals")
        except Exception as exc:  # noqa: BLE001
            errs.append(exc)

    threads = [threading.Thread(target=_worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errs == []
