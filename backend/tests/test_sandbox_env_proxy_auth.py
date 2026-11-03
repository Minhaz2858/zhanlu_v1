"""Phase 11 / S2 — sandbox env allowlist + LLM proxy authentication.

Contract:

* Environment variables entering a sandbox container pass a NAME
  allowlist (no credential-ish keys, no proxy/env-manipulation keys) and
  value sanity (no newlines) — refusals are logged WITHOUT values.
* The LLM proxy requires per-job HMAC authentication by default: a peer
  on the Unix socket that does not present the worker-minted token gets
  an ``unauthorized`` error and the upstream API key is never touched.
* Secrets (proxy auth failures, refused env vars) never reach logs.
"""
import asyncio
import json
import os
from types import SimpleNamespace

import pytest


# ── env allowlist ────────────────────────────────────────────────────────

def test_env_allowlist_blocks_credentials_and_env_manipulation():
    from app.services.sandbox.container_manager import filter_env_vars

    safe, refused = filter_env_vars({
        "ZHL_TASK_NAME": "ok",                    # kept
        "OPENAI_API_KEY": "sk-real",              # refused: credential
        "AWS_SECRET_ACCESS_KEY": "x",             # refused: credential
        "PATH": "/attacker/bin",                  # refused: env manipulation
        "LD_PRELOAD": "/evil.so",                 # refused: env manipulation
        "HTTP_PROXY": "http://evil:8080",         # refused: env manipulation
        "NOTE": "line1\nline2",                   # refused: newline value
    })
    assert safe == {"ZHL_TASK_NAME": "ok"}
    refused_keys = {k for k, _ in refused}
    assert refused_keys == {"OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY",
                            "PATH", "LD_PRELOAD", "HTTP_PROXY", "NOTE"}


def test_env_refusals_do_not_log_values(caplog):
    from app.services.sandbox.container_manager import filter_env_vars
    import logging
    with caplog.at_level(logging.WARNING):
        filter_env_vars({"OPENAI_API_KEY": "sk-super-secret-value"})
    assert "sk-super-secret-value" not in caplog.text


def test_run_container_filters_env(monkeypatch):
    """The docker -e construction uses the allowlist, not raw env_vars."""
    from app.services.sandbox import container_manager as cm

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    import subprocess as _sp
    monkeypatch.setattr(_sp, "run", fake_run)
    monkeypatch.setattr(cm, "_DOCKER_PATH", "/usr/bin/docker")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        cm.run_sandbox_container(
            image_name="img", input_dir=td, output_dir=td,
            command=["python", "-c", "x"], timeout=10,
            env_vars={"OK_VAR": "1", "OPENAI_API_KEY": "sk-x"},
        )
    cmd = captured["cmd"]
    joined = " ".join(cmd)
    assert "-e OK_VAR=1" in joined
    assert "sk-x" not in joined


# ── LLM proxy authentication ─────────────────────────────────────────────

@pytest.fixture()
def proxy(tmp_path, monkeypatch):
    from app.services.sandbox import llm_proxy as lp

    monkeypatch.setattr(lp.settings, "SANDBOX_LLM_PROXY_AUTH", True,
                        raising=False)
    monkeypatch.setattr(lp.settings, "SANDBOX_LLM_PROXY_SECRET",
                        "test-secret-32-bytes-aaaaaaaaaaaa", raising=False)
    import uuid as _uuid
    p = lp.LLMProxy(
        socket_path=f"/tmp/llmtest-{_uuid.uuid4().hex[:8]}.sock",
        api_key="sk-real-upstream",
        api_base="http://upstream.invalid",
        allowed_models=("test-model",),
    )
    # upstream never called in these tests — fake client
    class _FakeHTTP:
        async def post(self, *a, **k):  # pragma: no cover
            raise AssertionError("upstream called without valid auth")

    p._http = _FakeHTTP()
    return p


async def _roundtrip(proxy, payload):
    """Send one line-JSON request over the socket, read one response."""
    reader, writer = await asyncio.open_unix_connection(proxy.socket_path)
    writer.write((json.dumps(payload) + "\n").encode())
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout=5)
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:  # noqa: BLE001
        pass
    return json.loads(line)


def test_mint_verify_roundtrip():
    from app.services.sandbox.llm_proxy import mint_proxy_token, verify_proxy_token
    token = mint_proxy_token("job-1")
    assert verify_proxy_token("job-1", token) is True
    assert verify_proxy_token("job-1", "forged") is False
    assert verify_proxy_token("job-2", token) is False, "token bound to job"


@pytest.mark.asyncio
async def test_proxy_rejects_missing_auth(proxy):
    await proxy.start()
    try:
        resp = await _roundtrip(proxy, {
            "id": "r1", "model": "test-model", "messages": []})
        assert resp["success"] is False
        assert "unauthorized" in resp["error"].lower()
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_proxy_rejects_forged_token(proxy):
    from app.services.sandbox.llm_proxy import mint_proxy_token
    await proxy.start()
    try:
        resp = await _roundtrip(proxy, {
            "id": "r2", "model": "test-model", "messages": [],
            "job_id": "job-9", "auth_token": mint_proxy_token("other-job")})
        assert resp["success"] is False
        assert "unauthorized" in resp["error"].lower()
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_proxy_accepts_valid_job_token(proxy):
    from app.services.sandbox.llm_proxy import mint_proxy_token
    await proxy.start()
    try:
        resp = await _roundtrip(proxy, {
            "id": "r3", "model": "test-model", "messages": [],
            "job_id": "job-7", "auth_token": mint_proxy_token("job-7")})
        # auth passes → request proceeds upstream (our fake asserts it never
        # happens without auth, so reaching a non-unauthorized error means
        # the auth wall was passed)
        assert "unauthorized" not in resp.get("error", "").lower()
    finally:
        await proxy.stop()
