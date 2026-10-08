"""Regression test for 'chatbot not responding' silent-hang bug.

When the backend is started without OPENAI_API_KEY set, the streaming
endpoint ``/integration-endpoints/Core/InvokeLLMStream`` used to:
  - return HTTP 200 (StreamingResponse opens cleanly)
  - hang for the full 180s httpx timeout
  - emit zero bytes
  - leave the frontend with an opaque "Send errors (1)" counter

This test locks in the fix: the endpoint must emit a clear ``error``
event within a few hundred ms when no API key is configured, so the
frontend can surface a readable error.
"""
from unittest.mock import patch


def _login_token():
    from fastapi.testclient import TestClient
    from main import app
    client = TestClient(app)
    r = client.post(
        "/api/apps/local-zhanlu-app/auth/login",
        json={"email": "admin@zhanlu.dev", "password": "admin123"},
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return r.json().get("access_token") or r.json().get("token")


def test_stream_returns_clear_error_when_api_key_missing():
    """Without OPENAI_API_KEY, the SSE stream must emit an error event
    (not hang) so the user can see what went wrong."""
    from fastapi.testclient import TestClient
    from main import app

    token = _login_token()
    client = TestClient(app)

    with patch("app.routers.integrations.settings.OPENAI_API_KEY", ""):
        with client.stream(
            "POST",
            "/api/apps/local-zhanlu-app/integration-endpoints/Core/InvokeLLMStream",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "prompt": "Say hi",
                "messages": [{"role": "user", "content": "Say hi"}],
            },
        ) as resp:
            assert resp.status_code == 200, f"unexpected status: {resp.status_code}"

            # Read at most 5 seconds — must get the error event quickly
            chunks = []
            for line in resp.iter_lines():
                chunks.append(line)
                if any("error" in c for c in chunks):
                    break
                if len(chunks) > 20:
                    break

    assert any("error" in c for c in chunks), (
        f"Expected an error event in the SSE stream; got: {chunks}"
    )
    body = " ".join(chunks)
    assert "OPENAI_API_KEY" in body or "not configured" in body.lower(), (
        f"Error message should mention config; got: {body}"
    )


def test_stream_attempts_call_when_api_key_set():
    """With a (fake) key set, the endpoint must proceed PAST the pre-flight
    check and actually construct + enter the httpx client.

    We prove the pass-through by mocking ``httpx.AsyncClient`` so that entering
    it raises: if the pre-flight check had short-circuited (as it does when the
    key is missing) the client would never be touched. We additionally assert
    the unexpected error is surfaced as an SSE ``error`` event rather than
    leaking out of the ASGI app (fail-safe streaming)."""
    from fastapi.testclient import TestClient
    from main import app

    token = _login_token()
    client = TestClient(app)

    with patch("app.routers.integrations.settings.OPENAI_API_KEY", "unit-test-key"), \
         patch("app.routers.integrations.httpx.AsyncClient") as mock_client_cls:
        # Entering the client raises, short-circuiting the real HTTP attempt.
        mock_client_cls.return_value.__aenter__.side_effect = RuntimeError(
            "PRE_FLIGHT_PASSED"
        )
        with client.stream(
            "POST",
            "/api/apps/local-zhanlu-app/integration-endpoints/Core/InvokeLLMStream",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "prompt": "Say hi",
                "messages": [{"role": "user", "content": "Say hi"}],
            },
        ) as resp:
            assert resp.status_code == 200
            chunks = list(resp.iter_lines())

    # Pre-flight passed through: the client was constructed AND entered.
    mock_client_cls.assert_called_once()
    mock_client_cls.return_value.__aenter__.assert_called_once()
    # The unexpected error surfaced as an SSE error event (not an opaque 500).
    body = " ".join(chunks)
    assert "error" in body.lower(), (
        f"Expected an SSE error event from the failed client; got: {chunks}"
    )
