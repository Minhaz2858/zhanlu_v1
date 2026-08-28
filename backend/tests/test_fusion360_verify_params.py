from app.services.tool_handlers.fusion360_granular import _parse_vparam_values, _check_expected_params


def test_parse_vparam_values():
    out = "VPARAMS: a, b\nVPARAM a 0.8\nVPARAM b 1.4\n"
    assert _parse_vparam_values(out) == {"a": 8.0, "b": 14.0}  # cm -> mm


def test_check_expected_params_str_backward_compat():
    # legacy string entries still mean "name must exist"
    assert _check_expected_params(["a"], {"a": 8.0}) == []


def test_check_expected_params_value_match():
    assert _check_expected_params([{"name": "a", "value": 8}], {"a": 8.0}) == []


def test_check_expected_params_value_mismatch():
    issues = _check_expected_params([{"name": "a", "value": 10}], {"a": 8.0})
    assert issues and "a" in issues[0] and "10" in issues[0]
