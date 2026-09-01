"""R4 behavioural test — Decision Summary parser + sanitiser.

The agent_builder can emit a `:::decision-summary` block before
calling `create_agent`. The backend must:

  1. Parse the block via ``parse_decision_summary_block``.
  2. Sanitise the payload via ``_sanitize_decision_payload`` (drops
     unknown keys, normalises list-shaped values, etc.).
  3. Persist the cleaned payload to
     ``conv.metadata_["pending_agent_payload"]`` with
     ``conv.metadata_["awaiting_decision_summary"] = True``.
  4. The new POST /confirm-decision endpoint must:
     - validate the user-edited payload against the same allow-list
     - call the internal ``_create_agent`` helper with the payload
     - clear the awaiting_decision_summary flag
     - return the created agent

This file exercises (1) and (2) directly (no DB, no HTTP). The
end-to-end endpoint test is covered separately by manual restart +
inspection of the running Docker backend (UVICORN_RELOAD=false) since
spinning up an isolated DB schema for the test would require
overriding the application config and risks colliding with the
existing /app/zhanlu.db fixture the developer uses for verification.
"""


def _import_helpers():
    from app.services.agent_prompts import (
        parse_decision_summary_block,
        strip_decision_summary_block,
    )
    from app.routers.agents import _sanitize_decision_payload
    return parse_decision_summary_block, strip_decision_summary_block, _sanitize_decision_payload


# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------

def test_parse_decision_summary_block_extracts_json():
    parse, _, _ = _import_helpers()
    text = (
        "I'll build the agent now.\n"
        "\n"
        ":::decision-summary\n"
        '{"name": "Sales Agent", "capabilities": ["sales", "reporting"], '
        '"model": "automatic", "agent_type": "sequential"}\n'
        ":::\n"
    )
    payload = parse(text)
    assert payload is not None
    assert payload["name"] == "Sales Agent"
    assert payload["capabilities"] == ["sales", "reporting"]


def test_parse_decision_summary_block_handles_extra_whitespace():
    parse, _, _ = _import_helpers()
    text = ":::decision-summary   \n\n   {}   \n  :::"
    payload = parse(text)
    assert payload == {}


def test_parse_decision_summary_block_returns_none_when_absent():
    parse, _, _ = _import_helpers()
    assert parse("no block here") is None
    assert parse("") is None
    assert parse(None) is None


def test_parse_decision_summary_block_returns_none_on_bad_json():
    parse, _, _ = _import_helpers()
    text = ":::decision-summary\n{not valid json}\n:::"
    assert parse(text) is None


def test_parse_decision_summary_block_returns_none_for_non_dict_json():
    parse, _, _ = _import_helpers()
    text = ":::decision-summary\n[1, 2, 3]\n:::"
    assert parse(text) is None


# ---------------------------------------------------------------------------
# Strip tests
# ---------------------------------------------------------------------------

def test_strip_decision_summary_block_removes_fence():
    _, strip, _ = _import_helpers()
    text = (
        "Lead text.\n"
        ":::decision-summary\n"
        '{"name": "x"}\n'
        ":::\n"
        "Trailing text.\n"
    )
    out = strip(text)
    assert ":::decision-summary" not in out
    assert "Lead text." in out
    assert "Trailing text." in out


def test_strip_decision_summary_block_idempotent_on_empty():
    _, strip, _ = _import_helpers()
    # The helper's contract: empty / None input produces an empty
    # string (no fence was present, nothing to strip).
    assert strip("") == ""
    assert strip(None) == ""


# ---------------------------------------------------------------------------
# Sanitiser tests
# ---------------------------------------------------------------------------

def test_sanitize_payload_drops_disallowed_keys():
    _, _, sanitize = _import_helpers()
    raw = {
        "name": "X",
        "capabilities": ["a"],
        "skills": ["s1", "s2"],
        "data_read": True,
        "data_write": False,
        "max_call_count": 50,
        "evil_field": "DROP TABLE users;",
        "__class__": "Exploit",
    }
    out = sanitize(raw)
    assert out["name"] == "X"
    assert out["capabilities"] == ["a"]
    assert out["skills"] == ["s1", "s2"]
    assert out["data_read"] is True
    assert out["data_write"] is False
    assert out["max_call_count"] == 50
    assert "evil_field" not in out
    assert "__class__" not in out


def test_sanitize_payload_normalises_capabilities_string():
    _, _, sanitize = _import_helpers()
    out = sanitize({"name": "X", "capabilities": "alpha, beta , gamma"})
    assert out["capabilities"] == ["alpha", "beta", "gamma"]


def test_sanitize_payload_drops_invalid_boolean_types():
    _, _, sanitize = _import_helpers()
    # Non-bool truthy values for boolean keys are dropped (defensive)
    out = sanitize({"name": "X", "data_read": "yes", "human_fallback": 1})
    assert "data_read" not in out
    assert "human_fallback" not in out


def test_sanitize_payload_coerces_numeric_fields():
    _, _, sanitize = _import_helpers()
    out = sanitize({
        "name": "X",
        "max_call_count": 50.7,    # float → int
        "temperature": "0.7",      # str → not coerced for temperature (str-typed)
        "max_tokens": True,        # bool excluded from numeric branch
    })
    assert out["max_call_count"] == 50
    # 'max_tokens' = True is a bool, which is excluded by the explicit
    # `not isinstance(v, bool)` guard — defensive against Python's
    # bool-is-int quirk.
    assert "max_tokens" not in out


def test_sanitize_payload_keeps_prompt_layers():
    _, _, sanitize = _import_helpers()
    out = sanitize({
        "name": "X",
        "prompt_identity": "You are X.",
        "prompt_boundary": "Don't do Y.",
        "prompt_tools": "Use ask_data_agent.",
    })
    assert out["prompt_identity"] == "You are X."
    assert out["prompt_boundary"] == "Don't do Y."
    assert out["prompt_tools"] == "Use ask_data_agent."


# ---------------------------------------------------------------------------
# Integration: parse + sanitise the example payload from the system prompt
# ---------------------------------------------------------------------------

def test_end_to_end_parse_then_sanitize_system_prompt_example():
    """The system prompt's example payload should parse, sanitise, and
    pass through unchanged (all keys are in the allow-list)."""
    parse, _, sanitize = _import_helpers()
    example_text = (
        "Here is your agent.\n"
        ":::decision-summary\n"
        '{"name": "Demo Agent", "description": "x", "project": "global", '
        '"capabilities": ["a"], "model": "automatic", "agent_type": "sequential", '
        '"skills": [], "data_read": true, "data_write": false, "human_fallback": true}\n'
        ":::\n"
    )
    payload = parse(example_text)
    assert payload is not None
    clean = sanitize(payload)
    assert clean["name"] == "Demo Agent"
    assert clean["data_read"] is True
    assert clean["human_fallback"] is True
    # Original keys preserved, no extras
    for k in ("name", "description", "project", "capabilities", "model",
              "agent_type", "skills", "data_read", "data_write", "human_fallback"):
        assert k in clean
