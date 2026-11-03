"""Phase 8 T8-7 — MCP context propagation (verified, optional, fail-honest).

External MCP clients (no session context) keep today's behavior: tools
run with ``context=None`` and the data-tool fallback scopes honestly.
INTERNAL consumers (workflows, harness bridges) may pass a structured
``_zhanlu_context`` inside the arguments dict — the FastMCP wrapper strips
it from tool arguments and forwards it as the execution context so
org/binding scoping applies. Unverifiable/garbage context values are
dropped (never half-trusted).
"""

import pytest

from app.services.mcp_server.fastmcp_app import extract_zhanlu_context


def test_extract_returns_verified_context():
    args = {"a": 1, "_zhanlu_context": {"org_id": "o1", "agent_app_id": "a1"}}
    ctx, rest = extract_zhanlu_context(args)
    assert ctx == {"org_id": "o1", "agent_app_id": "a1"}
    assert rest == {"a": 1}


def test_extract_without_context_returns_none():
    ctx, rest = extract_zhanlu_context({"a": 1})
    assert ctx is None and rest == {"a": 1}


def test_extract_drops_non_dict_context():
    ctx, rest = extract_zhanlu_context({"_zhanlu_context": "org-one"})
    assert ctx is None and rest == {}


def test_extract_allows_only_known_context_keys():
    ctx, rest = extract_zhanlu_context({
        "_zhanlu_context": {"org_id": "o1", "user_id": "u1",
                            "agent_app_id": "a1", "run_id": "r1",
                            "conversation_id": "c1",
                            "is_superuser": True,  # never forwarded
                            "data_read": True},   # never forwarded
    })
    assert ctx == {"org_id": "o1", "user_id": "u1", "agent_app_id": "a1",
                   "run_id": "r1", "conversation_id": "c1"}
