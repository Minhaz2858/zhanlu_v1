"""Phase 10 / W1 — sandbox host-execution fallbacks must FAIL CLOSED.

Three confirmed host-execution fallback paths executed LLM-generated or
user-authored code on the host with backend privileges:

  1. sandbox_worker._execute_in_process — on Docker outage the worker ran
     the job's skill in-process (audit S1.1).
  2. workflow_sandbox._restricted_exec — workflow Code nodes fell back to
     in-process restricted exec on enqueue/poll failure or db=None
     (audit S1.2). Restricted __builtins__ is not a CPython boundary.
  3. sandbox/runner._run_fallback — host subprocess fallback gated only by
     SANDBOX_ENABLED (default False → host exec BY DEFAULT) (audit S1.3).

Contract: host-side execution requires an EXPLICIT
``SANDBOX_ALLOW_HOST_FALLBACK=true`` deployment decision. Without it every
one of these paths refuses with an honest failure — never silently
downgrades isolation.

Also pins the extra_mounts allowlist (audit S1.4): only paths under
SANDBOX_TMP_ROOT may enter the container.
"""
import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.fixture
def worker(monkeypatch):
    sys.path.insert(0, ".")
    from sandbox_worker import main as worker_main
    return worker_main


@pytest.fixture
def deny_host_fallback(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "SANDBOX_ALLOW_HOST_FALLBACK", False,
                        raising=False)


@pytest.fixture
def allow_host_fallback(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "SANDBOX_ALLOW_HOST_FALLBACK", True,
                        raising=False)


# ── S1.1: the WORKER must refuse in-process execution by default ────────

def test_worker_refuses_in_process_when_flag_off(worker, deny_host_fallback):
    svc = SimpleNamespace(
        record_event=lambda *a, **k: None,
        update_job_status=lambda *a, **k: None,
    )
    job = SimpleNamespace(id="j1", skill_name="anything", input_package={})
    executed = {}
    with patch.object(worker, "_execute_in_process",
                      lambda s, j: executed.setdefault("ran", True)):
        worker._execute_in_process_guarded(svc, job)
    assert not executed, "worker ran the job in-process with the flag off"
    assert worker._LAST_HOST_FALLBACK_REFUSAL["job_id"] == "j1"


def test_worker_allows_in_process_when_flag_explicitly_on(worker, allow_host_fallback):
    svc = SimpleNamespace(
        record_event=lambda *a, **k: None,
        update_job_status=lambda *a, **k: None,
    )
    job = SimpleNamespace(id="j2", skill_name="anything", input_package={})
    ran = {}
    with patch.object(worker, "_execute_in_process",
                      lambda s, j: ran.setdefault("ok", True)):
        worker._execute_in_process_guarded(svc, job)
    assert ran.get("ok"), "explicit opt-in must still reach in-process execution"


# ── S1.2: workflow Code nodes must not exec user code in-process ────────

def test_workflow_code_refuses_in_process_without_db(deny_host_fallback):
    from app.services import workflow_sandbox as ws
    out = ws.run_code_sandboxed(
        db=None, code="import os; output = os.listdir('/')",
        input_item={}, timeout=5)
    assert out["success"] is False
    assert "refused" in out["text"].lower()


def test_workflow_code_refuses_on_sandbox_failure(deny_host_fallback):
    from app.services import workflow_sandbox as ws

    def boom(*a, **k):
        raise RuntimeError("docker down")
    monkey_ws = ws
    orig_service = monkey_ws.SandboxService
    monkey_ws.SandboxService = boom
    try:
        out = ws.run_code_sandboxed(
            db=object(), code="output = 1", input_item={}, timeout=5)
    finally:
        monkey_ws.SandboxService = orig_service
    assert out["success"] is False
    assert "refused" in out["text"].lower()


def test_workflow_code_fallback_still_works_when_flag_on(allow_host_fallback):
    from app.services import workflow_sandbox as ws
    out = ws.run_code_sandboxed(
        db=None, code="output = {'n': input.get('n', 0) + 1}",
        input_item={"n": 41}, timeout=5)
    assert out["success"] is True
    assert out["items"] == [{"n": 42}]


# ── S1.3: runner host-subprocess fallback requires the explicit flag ────

def test_runner_refuses_host_fallback_by_default(deny_host_fallback):
    from app.services.sandbox import runner
    with patch.object(runner, "is_docker_available", return_value=False):
        result = asyncio.get_event_loop().run_until_complete(
            runner.execute_in_sandbox(code="print('hi')", runtime="python",
                                      timeout=10))
    assert result["success"] is False
    assert result["execution_mode"] == "refused"


def test_runner_fallback_works_when_flag_on(allow_host_fallback):
    from app.services.sandbox import runner
    with patch.object(runner, "is_docker_available", return_value=False):
        result = asyncio.get_event_loop().run_until_complete(
            runner.execute_in_sandbox(code="print('hi')", runtime="python",
                                      timeout=10))
    assert result["execution_mode"] == "fallback"


# ── S1.4: extra_mounts allowlist — only SANDBOX_TMP_ROOT paths ──────────

def test_extra_mounts_outside_tmp_root_are_refused(tmp_path, monkeypatch):
    from app.services.sandbox import container_manager as cm
    from app.config import settings
    monkeypatch.setattr(settings, "SANDBOX_TMP_ROOT", str(tmp_path),
                        raising=False)
    monkeypatch.setattr(settings, "SANDBOX_LLM_PROXY_SOCKET",
                        "/var/run/zhanlu-llm-proxy.sock", raising=False)
    kept, refused = cm.filter_extra_mounts([
        {"source": f"{tmp_path}/job1", "target": "/input"},    # inside → kept
        {"source": "/etc", "target": "/host-etc"},             # refused
        {"source": "/var/run/docker.sock",
         "target": "/var/run/docker.sock"},                    # refused
        {"source": "/var/run/zhanlu-llm-proxy.sock",
         "target": "/var/run/llm-proxy.sock", "read_only": True},  # kept
    ])
    assert kept == [(f"{tmp_path}/job1", "/input", True),
                    ("/var/run/zhanlu-llm-proxy.sock",
                     "/var/run/llm-proxy.sock", True)]
    assert len(refused) == 2
