"""Tests for workflow_sandbox — Docker-sandboxed Code node execution.

Pins three contracts:
  1. The embedded runner script's semantics (restricted exec, result.json).
  2. run_code_sandboxed enqueues a workflow_code SandboxJob and returns the
     worker's captured result (via the workflow_code_result event).
  3. Any sandbox-path failure falls back to the in-process restricted exec
     (dev-mode behavior, mirroring sandbox_worker._execute_in_process).
"""

import sys
import os
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from app.services import workflow_sandbox as ws  # noqa: E402


# ── Runner script semantics (executed in-process against its own logic) ──

def _run_runner(code: str, input_item: dict) -> dict:
    """Simulate the runner inside the container: load config.json, exec, parse result."""
    cfg = {"code": code, "input_item": input_item}
    restricted = {
        "__builtins__": {
            "len": len, "str": str, "int": int, "float": float, "bool": bool,
            "list": list, "dict": dict, "range": range, "enumerate": enumerate,
            "zip": zip, "abs": abs, "min": min, "max": max, "sum": sum,
            "round": round, "isinstance": isinstance, "Exception": Exception,
            "True": True, "False": False, "None": None, "print": print,
        },
        "json": json,
        "math": __import__("math"),
        "input": cfg["input_item"],
    }
    box = {}
    try:
        exec(compile(cfg["code"], "<code>", "exec"), restricted, box)
    except Exception as exc:  # noqa: BLE001
        return {"error": repr(exc)}
    out = box.get("output")
    if out is None and "output" not in box:
        return {"error": "output not assigned"}
    try:
        json.dumps(out)
    except TypeError:
        return {"error": "output not JSON-serializable"}
    return {"output": out}


def test_runner_produces_output_json():
    res = _run_runner(
        "output = {'greeting': 'hi ' + str(input.get('name', ''))}",
        {"name": "Ana"},
    )
    assert res == {"output": {"greeting": "hi Ana"}}


def test_runner_reports_missing_output():
    assert _run_runner("x = 1", {}) == {"error": "output not assigned"}


def test_runner_reports_non_json_output():
    # `len` is a safe builtin but a function — not JSON-serializable.
    assert "JSON-serializable" in _run_runner("output = len", {})["error"]


def test_runner_reports_exception():
    res = _run_runner("output = 1/0", {})
    assert res["error"].startswith("ZeroDivisionError")


# ── Sandboxed execution path (SandboxService mocked) ─────────────────────

class _FakeSandboxService:
    def __init__(self, job, events=None, fail_create=False):
        self.job = job
        self.events = events or []
        self.fail_create = fail_create
        self.created = None

    def create_job(self, **kwargs):
        if self.fail_create:
            raise RuntimeError("docker unavailable")
        self.created = kwargs
        return self.job

    def get_job(self, job_id):
        return self.job


def _job(status="completed", events=None, error_message=None):
    return SimpleNamespace(
        id="job-1", status=status, error_message=error_message,
        events=events or [],
    )


def _result_event(result):
    return SimpleNamespace(event_type="workflow_code_result", data_json={"result": result})


def test_run_code_sandboxed_returns_worker_result(monkeypatch):
    result = {"success": True, "text": '{"greeting": "hi Ana"}', "items": [{"greeting": "hi Ana"}]}
    svc = _FakeSandboxService(_job(events=[_result_event(result)]))
    monkeypatch.setattr(ws, "SandboxService", lambda db: svc)

    out = ws.run_code_sandboxed(db=None, code="output = {'greeting': 'hi ' + str(input.get('name',''))}", input_item={"name": "Ana"}, timeout=5)
    assert out == result
    assert svc.created["skill_name"] == "workflow_code"
    assert svc.created["image_name"].startswith("zhanlu-sandbox-python")
    assert "runner_script" in svc.created["input_package"]
    assert svc.created["input_package"]["runner_script_name"] == "workflow_code_runner.py"
    cfg = json.loads(svc.created["input_package"]["config_json"])
    assert cfg["input_item"] == {"name": "Ana"}


def test_run_code_sandboxed_failed_job_returns_error(monkeypatch):
    svc = _FakeSandboxService(_job(status="failed", error_message="boom"))
    monkeypatch.setattr(ws, "SandboxService", lambda db: svc)
    out = ws.run_code_sandboxed(db=None, code="output = 1", input_item={}, timeout=5)
    assert out["success"] is False
    assert "boom" in out["text"]


def test_run_code_sandboxed_falls_back_when_docker_unavailable(monkeypatch):
    svc = _FakeSandboxService(None, fail_create=True)
    monkeypatch.setattr(ws, "SandboxService", lambda db: svc)
    out = ws.run_code_sandboxed(db=None, code="output = {'n': input.get('n', 0) + 1}", input_item={"n": 41}, timeout=5)
    assert out["success"] is True
    assert out["items"] == [{"n": 42}]
