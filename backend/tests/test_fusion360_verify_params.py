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


def test_check_expected_params_tolerance_boundary():
    # exactly 5% above expected must PASS (float-noise safe: 8*1.05 = 8.4)
    assert _check_expected_params([{"name": "a", "value": 8}], {"a": 8.4}) == []


def test_check_expected_params_just_outside_tolerance():
    # 8.5 is > 5% of 8 (0.4), so it must FAIL
    issues = _check_expected_params([{"name": "a", "value": 8}], {"a": 8.5})
    assert issues and "a" in issues[0]


def test_check_expected_params_zero_value_floor_tolerance():
    # expected 0 -> floor tolerance 0.01mm, so 0.005 passes
    assert _check_expected_params([{"name": "a", "value": 0}], {"a": 0.005}) == []


def test_check_expected_params_negative_value():
    assert _check_expected_params([{"name": "a", "value": -8}], {"a": -8.0}) == []


def test_check_expected_params_none_value_skips_check():
    # None expected value means "only existence matters"
    assert _check_expected_params([{"name": "a", "value": None}], {"a": 8.0}) == []


def test_check_expected_params_missing_name():
    issues = _check_expected_params([{"value": 8}], {"a": 8.0})
    assert issues and "name" in issues[0]
    assert "missing parameter: None" not in issues[0]


def test_check_expected_params_non_numeric_value_no_raise():
    issues = _check_expected_params([{"name": "a", "value": "8mm"}], {"a": 8.0})
    assert issues and "non-numeric" in issues[0]


def test_parse_vparam_values_malformed():
    assert _parse_vparam_values("VPARAM a\n") == {}       # no value -> skipped
    assert _parse_vparam_values("VPARAM a xyz\n") == {}   # non-numeric -> skipped
    assert _parse_vparam_values("") == {}
    assert _parse_vparam_values(None) == {}
