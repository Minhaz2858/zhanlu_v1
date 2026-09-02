"""Tests for the workflow node registry (workflow_node_types.py).

Pure registry assertions — no DB, no TestClient. Pins the non-IT-first
db_query contract: Natural-language mode by default, conditional
Question/SQL fields, advanced collapse for Limit + universal
error-handling props.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import workflow_node_types as wnt  # noqa: E402


def _db_query():
    return wnt.get_node_type("db_query")


def test_db_query_defaults_to_natural_language():
    mode = next(p for p in _db_query()["properties"] if p["name"] == "mode")
    assert mode["default"] == "nl"
    assert mode["options"][0]["value"] == "nl"  # NL listed first


def test_db_query_question_and_sql_are_mode_conditional():
    props = {p["name"]: p for p in _db_query()["properties"]}
    assert props["question"]["visible_when"] == {"mode": "nl"}
    assert props["sql"]["visible_when"] == {"mode": "sql"}


def test_db_query_limit_is_advanced():
    limit = next(p for p in _db_query()["properties"] if p["name"] == "limit")
    assert limit["advanced"] is True


def test_kb_selector_is_named_database_for_non_it_users():
    kb = next(p for p in _db_query()["properties"] if p["name"] == "kb_id")
    assert kb["displayName"] == "Database"
    assert kb["required"] is True


def test_error_handling_props_are_advanced_on_every_node_type():
    for t in wnt.list_node_types():
        for name in ("retryOnFail", "continueOnFail"):
            p = next(x for x in t["properties"] if x["name"] == name)
            assert p["advanced"] is True, f"{t['name']}.{name} not advanced"


def test_required_config_fields_unchanged_for_db_query():
    # Only kb_id is required — question/sql stay optional (conditional).
    assert wnt.required_config_fields("db_query") == ["kb_id"]
