"""Keyless LLM endpoints must not produce an illegal ``Bearer ''`` header.

Root cause (2026-10-31, user screenshot conv=b924e093): the
``qwen3.6-27b`` model row (provider=Custom, a self-hosted vLLM endpoint)
has a NULL ``api_key``. Every LLM call site built
``Authorization: Bearer {endpoint.api_key}`` unconditionally, so the
request died client-side with
``RuntimeError("LLM request failed: Illegal header value b'Bearer '")``
before any bytes left the process — the user only saw
"Sorry, I hit an error while responding."

The endpoint itself accepts unauthenticated requests (verified live:
/v1/chat/completions returns 200 without an Authorization header), so
the correct fix is to OMIT the Authorization header when the key is
empty — never send an empty bearer token.
"""
from __future__ import annotations

from app.services.llm_service import llm_endpoint_headers


def test_empty_api_key_omits_authorization_header():
    headers = llm_endpoint_headers("")
    assert "Authorization" not in headers, (
        "an empty api_key must not produce an Authorization header"
    )
    assert headers["Content-Type"] == "application/json"


def test_none_api_key_omits_authorization_header():
    headers = llm_endpoint_headers(None)
    assert "Authorization" not in headers


def test_whitespace_api_key_omits_authorization_header():
    headers = llm_endpoint_headers("   ")
    assert "Authorization" not in headers


def test_real_api_key_still_bearer():
    headers = llm_endpoint_headers("sk-test-123")
    assert headers["Authorization"] == "Bearer sk-test-123"
    assert headers["Content-Type"] == "application/json"


def test_no_live_call_site_builds_empty_bearer():
    """Guard the four live call sites against regressing to the
    unconditional f-string that caused the outage."""
    import ast
    from pathlib import Path

    offenders = []
    for rel in (
        "app/routers/agents.py",
        "app/services/sub_agent_reliability.py",
    ):
        src = (Path(__file__).resolve().parents[1] / rel).read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for k in node.keys:
                    if (
                        isinstance(k, ast.Constant)
                        and k.value == "Authorization"
                        and isinstance(node.values[0] if node.values else None, ast.JoinedStr)
                        and "Bearer {" in ast.dump(node)
                    ):
                        offenders.append(rel)
    assert not offenders, (
        f"unconditional Bearer f-strings are back in: {offenders}"
    )
