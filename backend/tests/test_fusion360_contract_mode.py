from app.services.tool_handlers.fusion360_granular import (
    _load_contract, _contract_issues, _feature_matches_body,
)

CONTRACT = {"part": "hex_bolt", "features": [
    {"kind": "hex", "across_flats": 8, "height": 14},
    {"kind": "cylinder", "diameter": 5, "height": 20},
]}
BODIES = [
    {"index": 0, "min": [-0.462, -0.400, 0.0], "max": [0.462, 0.400, 1.4], "faces": 14},
    {"index": 1, "min": [-0.250, -0.250, 1.4], "max": [0.250, 0.250, 3.4], "faces": 3},
]


def test_feature_matches_hex():
    b = BODIES[0]
    assert _feature_matches_body({"kind": "hex", "across_flats": 8, "height": 14}, b) == []


def test_feature_mismatch_hex_height():
    b = BODIES[0]
    issues = _feature_matches_body({"kind": "hex", "across_flats": 8, "height": 12}, b)
    assert any("height" in i for i in issues)


def test_contract_issues_ok():
    assert _contract_issues(CONTRACT, BODIES) == []


def test_contract_issues_missing_feature():
    # only one body declared kind matches: hex matches body0; cylinder needs a
    # 5mm-dia 20mm-tall body — none, so a per-feature failure must be reported
    issues = _contract_issues(CONTRACT, [BODIES[0]])
    assert issues and any("cylinder" in i for i in issues)
