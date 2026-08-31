"""query_router static-route tests — contract performance must pin erp_v_contract_execution."""
from app.services.db.query_router import resolve_static_route


def test_contract_performance_routes_to_contract_execution():
    r = resolve_static_route("give me Contract Performance for last month report in docx file")
    assert r is not None
    assert r["table"] == "erp_v_contract_execution"


def test_contract_plain_routes_to_contract_execution():
    r = resolve_static_route("show me my contracts")
    assert r is not None
    assert r["table"] == "erp_v_contract_execution"


def test_chinese_contract_routes_to_contract_execution():
    r = resolve_static_route("给我上个月的合同履约报告")
    assert r is not None
    assert r["table"] == "erp_v_contract_execution"


def test_sales_still_routes_to_sales():
    r = resolve_static_route("give me top customer for last month")
    assert r is not None
    assert r["table"] == "erp_v_sale_orderentry"


def test_contract_route_carries_date_hint():
    r = resolve_static_route("contract performance last month")
    assert r is not None
    assert "erp_v_contract_execution" in (r.get("date_hint") or "")
    assert "NEVER use erp_v_sale_orderentry" in (r.get("date_hint") or "")


def test_sales_route_does_not_shadow_contract():
    # "sales contract" mentions both; contract is more specific and listed first.
    r = resolve_static_route("sales contract volume")
    assert r is not None
    assert r["table"] == "erp_v_contract_execution"
