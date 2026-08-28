from app.services.tool_handlers.fusion360_parametric import (
    _params_list_code, _validate_spec, _contract_prefix,
)


def test_params_list_code_has_name_value_loop():
    code = _params_list_code()
    assert "userParameters" in code
    assert "PARAM_NAME" in code and "PARAM_VALUE" in code


def test_validate_spec_rejects_bad():
    assert _validate_spec({"part": "", "features": []}) is not None          # missing part
    assert _validate_spec({"part": "x", "features": []}) is not None         # empty features
    assert _validate_spec({"part": "x", "features": [{"kind": "hex"}]}) is None  # ok


def test_contract_prefix_stable_and_short():
    p1 = _contract_prefix("12345678-abcd-4ef0-9aab-000000000001")
    assert len(p1) == 9 and p1.startswith("c")  # c<8 hex>
    assert _contract_prefix("12345678-abcd-4ef0-9aab-000000000001") == p1
