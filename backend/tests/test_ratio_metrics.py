"""Ratio metrics: a ratio is only published when both its sides share a basis.

The measured failure: a period-to-date figure came back ≈17,957% because the
service level was computed as ``SUM(quantity) / COUNT(DISTINCT id)`` — a summed
QUANTITY divided by a count of DOCUMENTS. The SQL executed, the column existed,
the value was non-zero, so every guard passed and the number was published as a
percentage.

What is pinned here is the decision logic on generic shapes: grain, units,
window, denominator, range, grouping — plus the two rules that matter most to a
reader: an unprovable ratio is never published as a confident figure, and a
large ratio is never silently clamped (it may be entirely legitimate, and the
metric contract is the only thing that can licence it).

Everything below uses neutral fixtures (``order_qty``/``order_id``/``created_at``)
so the checks are proven to come from the SHAPE of the SQL, not from any table,
ERP, customer, or source name.
"""

from __future__ import annotations

import ast
import pathlib
import re
import sqlite3

from app.services.db import grounding as g
from app.services.db import ratio_contract as rc
from app.services.db.contracts import CanonicalResultSet, GroundedPlan

TABLE = "vw_order_fulfilment"
WINDOW = "WHERE created_at >= '2026-08-01' AND created_at < '2026-09-01'"


# ── fixtures ───────────────────────────────────────────────────────────────


def _plan(**kw) -> GroundedPlan:
    base = dict(
        intent="this month's fulfilment",
        question="2026年8月履约情况",
        source_ids=["kb-1"],
        tables=[TABLE],
        authorized_tables=[TABLE],
        dialect="mysql",
        catalog_version="shape:abc",
        capability_version="mysql:shape:abc",
    )
    base.update(kw)
    return GroundedPlan(**base)


def _evidence(slot, columns) -> dict:
    return {
        "slot": slot,
        "label": slot,
        "matched": [],
        "score": 1.0,
        "tables": [TABLE],
        "columns": columns,
        "source": "catalog",
    }


def _crs_for(plan, rows, sql, schema_info=None, contract=None):
    return g.build_canonical_result_set(
        plan=plan,
        exec_result={"rows": rows, "truncated": False, "sql": sql},
        sql=sql,
        schema_info=schema_info,
        contract=contract,
        provenance={"tables": [TABLE]},
    )


def _term(**kw):
    """A stand-in for the metric-contract term the vocabulary hands over."""

    class _Term:
        def __init__(self, payload):
            self.__dict__.update(payload)

    return _Term(kw)


SAME_GRAIN_SQL = (
    "SELECT SUM(shipped_qty) / SUM(order_qty) AS fulfilment_rate "
    f"FROM {TABLE} {WINDOW}"
)
QUANTITY_OVER_COUNT_SQL = (
    "SELECT SUM(shipped_qty) / COUNT(DISTINCT order_id) AS fulfilment_rate "
    f"FROM {TABLE} {WINDOW}"
)


# ── 1. a valid percentage at one grain ─────────────────────────────────────


def test_a_ratio_at_the_same_grain_is_valid():
    contract = rc.contract_from_expressions("fulfilment_rate", "SUM(shipped_qty)", "SUM(order_qty)")
    assert contract.numerator_grain == rc.GRAIN_QUANTITY
    assert contract.denominator_grain == rc.GRAIN_QUANTITY
    assert contract.compatible_grain is True
    verdict = rc.validate_ratio(
        contract, value=0.8908, sql=SAME_GRAIN_SQL, numerator_value=890.8, denominator_value=1000.0
    )
    assert verdict["ok"] is True, verdict["reasons"]
    assert verdict["reasons"] == []
    assert verdict["checks"]["grain"] == "pass"

    plan = _plan(evidence=[_evidence("fulfilment rate", [f"{TABLE}.shipped_qty"])])
    crs = _crs_for(plan, [{"fulfilment_rate": 0.8908}], SAME_GRAIN_SQL)
    assert [v["ok"] for v in crs.ratio_validations] == [True]
    assert crs.unsupported_slots == []
    assert not any("could not be verified" in w for w in crs.warnings)


# ── 2. a quantity divided by a document count ──────────────────────────────


def test_a_quantity_divided_by_a_count_is_invalid():
    plan = _plan(
        evidence=[
            _evidence("fulfilment rate", [f"{TABLE}.shipped_qty"]),
            _evidence("shipped quantity", [f"{TABLE}.shipped_qty"]),
        ],
        ratio_contracts=[
            rc.contract_from_expressions(
                "fulfilment rate", "SUM(shipped_qty)", "SUM(order_qty)"
            ).to_dict()
        ],
    )
    rows = [{"shipped_qty": 3160537.6, "fulfilment_rate": 17957.6}]
    crs = _crs_for(plan, rows, QUANTITY_OVER_COUNT_SQL)

    verdict = crs.ratio_validations[0]
    assert verdict["ok"] is False
    assert "quantity_over_count" in verdict["reasons"]
    assert verdict["checks"]["grain"] == "fail"
    assert verdict["contract"]["numerator_grain"] == rc.GRAIN_QUANTITY
    assert verdict["contract"]["denominator_grain"] == rc.GRAIN_ENTITY_COUNT
    assert verdict["contract"]["required_grain"] == rc.GRAIN_QUANTITY, (
        "the declared basis is carried over, so the departure from it is visible"
    )
    assert any("defines it as" in e for e in verdict["evidence"])
    # the ratio is not published as a verified figure …
    ratio_slots = [s for s in crs.unsupported_slots if s["reason"] == "ratio_unverified"]
    assert [s["slot"] for s in ratio_slots] == ["fulfilment rate"]
    assert crs.complete is False
    # … the warning says so in business language …
    assert any("not measured on the same basis" in w for w in crs.warnings)
    assert crs.disclosure
    # … and the numerator stands on its own: only the ratio is withheld
    assert "shipped quantity" not in {s["slot"] for s in crs.unsupported_slots}
    assert rows[0]["shipped_qty"] == 3160537.6


# ── 3. an unknown grain ────────────────────────────────────────────────────


def test_an_unknown_grain_makes_a_ratio_unverified():
    contract = rc.contract_from_expressions("ratio", "shipped_qty", "order_qty")
    assert contract.numerator_grain == rc.GRAIN_UNKNOWN
    verdict = rc.validate_ratio(contract, value=0.9)
    assert verdict["ok"] is False
    assert "unknown_grain" in verdict["reasons"]
    assert "could not be verified" in rc.ratio_disclosure(verdict)


# ── 4. mismatched date scopes ──────────────────────────────────────────────


def test_the_two_sides_must_cover_the_same_period():
    sql = (
        "SELECT (SELECT SUM(shipped_qty) FROM " + TABLE + " "
        "WHERE created_at >= '2026-08-01' AND created_at < '2026-09-01') / "
        "(SELECT SUM(order_qty) FROM " + TABLE + " "
        "WHERE created_at >= '2026-07-01' AND created_at < '2026-08-01') AS fulfilment_rate"
    )
    contract = rc.contract_from_expressions("fulfilment_rate", "SUM(shipped_qty)", "SUM(order_qty)")
    verdict = rc.validate_ratio(contract, value=0.9, sql=sql)
    assert verdict["ok"] is False
    assert "date_scope_mismatch" in verdict["reasons"], verdict
    assert "same period" in rc.ratio_disclosure(verdict)


def test_a_missing_period_is_flagged_when_the_question_asked_for_one():
    sql = f"SELECT SUM(shipped_qty) / SUM(order_qty) AS fulfilment_rate FROM {TABLE}"
    contract = rc.contract_from_expressions("fulfilment_rate", "SUM(shipped_qty)", "SUM(order_qty)")
    verdict = rc.validate_ratio(contract, value=0.9, sql=sql, requested_window=True)
    assert "date_scope_missing" in verdict["reasons"], verdict


# ── 5. mismatched units ────────────────────────────────────────────────────


def test_incompatible_units_make_a_ratio_unverified():
    term = _term(
        term="unit yield",
        definition="shipped quantity / order quantity",
        sql_expression="SUM(shipped_qty) / SUM(order_qty)",
        unit="%",
        bindings={
            "numerator": "SUM(shipped_qty)",
            "denominator": "SUM(order_qty)",
            "numerator_unit": "件",
            "denominator_unit": "吨",
        },
    )
    contract = rc.contract_from_term(term)
    assert contract is not None and contract.declared()
    assert contract.numerator_unit == "件" and contract.denominator_unit == "吨"
    verdict = rc.validate_ratio(contract, value=0.9)
    assert verdict["ok"] is False
    assert "unit_mismatch" in verdict["reasons"]
    assert "different units" in rc.ratio_disclosure(verdict)


# ── 6. zero denominator ────────────────────────────────────────────────────


def test_a_zero_denominator_is_disclosed_and_never_reported_as_zero():
    plan = _plan(evidence=[_evidence("fulfilment rate", [f"{TABLE}.shipped_qty"])])
    rows = [{"shipped_qty": 0.0, "fulfilment_rate": None}]
    crs = _crs_for(plan, rows, SAME_GRAIN_SQL)
    verdict = crs.ratio_validations[0]
    assert "zero_denominator" in verdict["reasons"]
    assert verdict["value"] is None, "a null ratio is not a zero"
    assert any("measured against" in w or "cannot be computed" in w for w in crs.warnings)
    assert all(s["slot"] != "fulfilment_rate" for s in crs.unsupported_slots), (
        "a zero denominator is reported as a caveat on the metric, not as a missing metric"
    )


def test_a_zero_denominator_is_a_real_answer_when_the_contract_says_so():
    contract = rc.contract_from_expressions("share", "SUM(shipped_qty)", "SUM(order_qty)")
    contract = rc.RatioContract(**{**contract.to_dict(), "zero_denominator": "zero"})
    verdict = rc.validate_ratio(contract, value=None, denominator_value=0.0)
    assert "zero_denominator" not in verdict["reasons"]
    assert verdict["checks"]["denominator_nonzero"] == "pass"


# ── 7. a ratio above 100%, when the contract permits it ────────────────────


def test_a_ratio_above_100_percent_is_valid_when_the_contract_permits_it():
    term = _term(
        term="coverage index",
        definition="covered quantity / ordered quantity",
        sql_expression="SUM(covered_qty) / SUM(order_qty)",
        unit="index",
        bindings={
            "numerator": "SUM(covered_qty)",
            "denominator": "SUM(order_qty)",
            "valid_range": [0, 100000],
        },
    )
    contract = rc.contract_from_term(term)
    assert contract.valid_range == (0, 100000)
    verdict = rc.validate_ratio(contract, value=17957.6)
    assert verdict["ok"] is True, verdict["reasons"]
    assert "within the declared range" in " ".join(verdict["evidence"])

    plan = _plan(
        evidence=[_evidence("coverage index", [f"{TABLE}.order_qty"])],
        ratio_contracts=[contract.to_dict()],
    )
    sql = f"SELECT SUM(order_qty) / SUM(covered_qty) AS coverage_index FROM {TABLE} {WINDOW}"
    crs = _crs_for(plan, [{"coverage_index": 17957.6}], sql)
    assert crs.unsupported_slots == []
    assert not any("over 100%" in w for w in crs.warnings), crs.warnings


def test_a_percentage_outside_0_100_without_a_contract_range_is_warned_not_clamped():
    term = _term(
        term="fulfilment rate",
        definition="shipped quantity / order quantity",
        sql_expression="SUM(shipped_qty) / SUM(order_qty)",
        unit="%",
    )
    contract = rc.contract_from_term(term)
    assert contract.valid_range is None
    verdict = rc.validate_ratio(contract, value=17957.6)
    assert verdict["ok"] is False and verdict["reasons"] == ["range_unproven"]
    assert verdict["value"] == 17957.6, "the figure is suspect, never trimmed"
    assert "far outside the range" in rc.ratio_disclosure(verdict)


# ── 8. unsupported-ratio disclosure ────────────────────────────────────────


def test_an_unverified_ratio_is_disclosed_in_business_language():
    plan = _plan(evidence=[_evidence("fulfilment rate", [f"{TABLE}.shipped_qty"])])
    crs = _crs_for(plan, [{"shipped_qty": 100.0, "fulfilment_rate": 17.0}], QUANTITY_OVER_COUNT_SQL)
    slot = next(s for s in crs.unsupported_slots if s["reason"] == "ratio_unverified")
    assert slot["kind"] == "metric"
    assert slot["checks"]["grain"] == "fail", "the verdict carries the checks that ran"
    assert "fulfilment_rate" in slot["columns"], "the column the SQL produced is named"
    assert slot["slot"] == "fulfilment rate", (
        "the reader is told in the plan's business wording, not the raw alias"
    )
    assert crs.disclosure and "fulfilment rate" in crs.disclosure
    directive = g.narrate_directive(crs)
    assert "fulfilment rate" in directive, "the narrator is told the ratio is not available"
    assert "not measured on the same basis" in directive
    assert "never write 0, 0%, none or a null-derived value" in directive
    # and the deterministic narrative check catches prose that claims it anyway
    assert g.check_narrative_against_result("The fulfilment_rate was 17.0.", crs) != []


# ── 9. the zero-shape is preserved ─────────────────────────────────────────


def test_a_zero_shape_keeps_the_requested_slots_and_the_zero_disclosure():
    plan = _plan(
        evidence=[
            _evidence("fulfilment rate", [f"{TABLE}.shipped_qty", f"{TABLE}.order_qty"]),
            _evidence("order quantity", [f"{TABLE}.order_qty"]),
        ]
    )
    rows = [{"order_qty": 0.0, "shipped_qty": 0.0, "fulfilment_rate": 0.0}]
    crs = _crs_for(plan, rows, SAME_GRAIN_SQL)
    assert [v["ok"] for v in crs.ratio_validations] == [True], crs.ratio_validations[0]["reasons"]
    assert not [s for s in crs.unsupported_slots if s["reason"] == "ratio_unverified"], (
        "the zero shape is not a grain defect: the ratio itself stays out of it"
    )
    zero_slots = [s for s in crs.unsupported_slots if s["reason"] == "zero_unproven"]
    assert zero_slots, "the zero-shape disclosure is preserved, not replaced"
    assert rows[0]["order_qty"] == 0.0, "a real zero is still a real zero"


# ── 10. a different source schema gets the same checks ─────────────────────


def test_a_different_schema_gets_the_same_grain_checks():
    """No table name is involved: the aggregation shape is the whole input."""
    other = "SELECT AVG(unit_price) / COUNT(*) AS price_per_row FROM some_other_source"
    candidates = rc.ratios_in_sql(other)
    assert candidates and candidates[0]["column"] == "price_per_row"
    contract = rc.contract_from_expressions(
        candidates[0]["column"], candidates[0]["numerator"], candidates[0]["denominator"]
    )
    assert contract.numerator_grain == rc.GRAIN_QUANTITY
    assert contract.denominator_grain == rc.GRAIN_ROW_COUNT
    verdict = rc.validate_ratio(contract, value=12.5, sql=other)
    assert verdict["ok"] is False and "quantity_over_count" in verdict["reasons"]

    # …and a ratio whose two sides are the same kind of count is fine
    ok_sql = "SELECT COUNT(DISTINCT id) / COUNT(DISTINCT order_id) AS id_share FROM some_other_source"
    same = rc.contract_from_expressions("id_share", "COUNT(DISTINCT id)", "COUNT(DISTINCT order_id)")
    assert same.numerator_grain == same.denominator_grain == rc.GRAIN_ENTITY_COUNT
    assert rc.validate_ratio(same, value=0.98, sql=ok_sql)["ok"] is True

    # …but rows over entities is a different basis, and says so
    mixed = rc.contract_from_expressions("rows_per_entity", "COUNT(*)", "COUNT(DISTINCT id)")
    verdict = rc.validate_ratio(mixed, value=0.98, sql=ok_sql)
    assert verdict["ok"] is False and "grain_mismatch" in verdict["reasons"]


# ── 11. no deployment-specific identifier in the rule ─────────────────────


def test_the_ratio_rules_carry_no_deployment_specific_identifier():
    source = pathlib.Path(rc.__file__).read_text()
    docstrings = [
        node.value.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    ]
    code = source
    for text in docstrings:
        code = code.replace(text, "")
    for forbidden in ("erp_", "FID", "Ecisco", "zhanlu", "10.10.10", "customer", "out_fqty"):
        assert forbidden not in code, (
            f"{forbidden!r} in the rules: detection must follow the SQL's shape, "
            "never a source-specific name"
        )


# ── 12. the measured case, reproduced on a generic fixture ─────────────────


def _measured_rows(value: float, count: int = 176) -> list[dict]:
    """The measured shape: one monthly row whose rate is the bad division."""
    return [{"shipped_qty": value * count, "fulfilment_rate": value}]


def test_the_measured_ratio_is_reproduced_and_not_published_as_verified():
    """≈17,957% from ``SUM(quantity) / COUNT(DISTINCT id)``.

    Three things must hold at once: the engine really does return the number, the
    platform refuses to verify it, and a contract that DOES licence a large ratio
    still passes — so the guard is a check, not a blanket refusal of big numbers.
    """
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE vw_order_fulfilment (order_id TEXT, shipped_qty REAL)")
    db.executemany(
        "INSERT INTO vw_order_fulfilment VALUES (?, ?)",
        [(f"DOC-{i}", 17957.6) for i in range(176)],
    )
    sql = QUANTITY_OVER_COUNT_SQL.replace("created_at >= '2026-08-01' AND created_at < '2026-09-01'", "1=1")
    raw = db.execute(
        f"SELECT SUM(shipped_qty) / COUNT(DISTINCT order_id) FROM {TABLE}"
    ).fetchone()[0]
    assert round(raw, 1) == 17957.6, "the same arithmetic, on a generic fixture"
    assert "SUM(shipped_qty)" in sql and "COUNT(DISTINCT order_id)" in sql

    plan = _plan(
        evidence=[
            _evidence("fulfilment rate", [f"{TABLE}.shipped_qty"]),
            _evidence("shipped quantity", [f"{TABLE}.shipped_qty"]),
        ],
        ratio_contracts=[
            rc.contract_from_expressions(
                "fulfilment rate", "SUM(shipped_qty)", "SUM(order_qty)"
            ).to_dict()
        ],
    )
    crs = _crs_for(plan, _measured_rows(17957.6), sql)

    verdict = next(v for v in crs.ratio_validations if v["column"] == "fulfilment_rate")
    assert verdict["value"] == 17957.6, "the value that was measured is the value examined"
    assert verdict["ok"] is False and "quantity_over_count" in verdict["reasons"]
    assert verdict["checks"] == {
        "numerator_supported": "pass",
        "denominator_supported": "pass",
        "source_scope": "pass",
        # this fixture's SQL carries no period, so the window check is unknown —
        # reported as unknown, never as a pass
        "date_scope": "unknown",
        "grain": "fail",
        "units": "pass",
        "denominator_nonzero": "unknown",
        "range": "fail",
        "dimensions": "unknown",
    }, verdict["checks"]
    assert verdict["contract"]["tier"] == "declared"

    # not published as a verified figure …
    ratio_slots = [s for s in crs.unsupported_slots if s["reason"] == "ratio_unverified"]
    assert ratio_slots, crs.unsupported_slots
    assert ratio_slots[0]["slot"] == "fulfilment rate"
    # … the warning and the provenance are in the canonical result set …
    assert any("not measured on the same basis" in w for w in crs.warnings)
    assert "fulfilment rate" in crs.disclosure
    assert crs.complete is False
    assert crs.to_dict()["ratio_validations"][0]["checks"]["grain"] == "fail"
    assert crs.validate() == []
    # … the narration is told the ratio is unverified …
    directive = g.narrate_directive(crs)
    assert "fulfilment rate" in directive
    assert "unverified" in directive or "not measured on the same basis" in directive
    # … and a contract that licences a large value is still honoured.
    licensed = rc.contract_from_term(
        _term(
            term="coverage index",
            definition="covered quantity / ordered quantity",
            sql_expression="SUM(covered_qty) / SUM(order_qty)",
            unit="index",
            bindings={"valid_range": [0, 1000000]},
        )
    )
    assert rc.validate_ratio(licensed, value=17957.6)["ok"] is True


def test_the_canonical_result_set_refuses_a_ratio_verdict_without_checks():
    crs = CanonicalResultSet(
        complete=True,
        ratio_validations=[{"metric": "r", "ok": False, "checks": {}}],
    )
    assert any("records no checks" in v for v in crs.validate())
    crs = CanonicalResultSet(complete=True, ratio_validations=[{"metric": "r", "checks": {"grain": "pass"}}])
    assert any("carries no verdict" in v for v in crs.validate())


def test_every_ratio_reason_has_a_disclosure_sentence():
    contract = rc.contract_from_expressions("r", "SUM(a)", "COUNT(DISTINCT b)")
    for reason in rc.RATIO_REASONS:
        verdict = {
            "metric": "the ratio",
            "ok": False,
            "reasons": [reason],
            "checks": {},
            "evidence": [],
            "contract": contract.to_dict(),
            "value": 1.0,
        }
        text = rc.ratio_disclosure(verdict)
        assert text and "the ratio" in text, reason
        assert re.search(r"[.!]", text), reason
