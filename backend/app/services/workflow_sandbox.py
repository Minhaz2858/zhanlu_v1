"""Workflow Code node — Docker-sandboxed execution (agent-sandbox pattern).

The Code node executes user-authored Python, so it must run in the real
Docker sandbox like agent skills do (``SandboxService → SandboxJob →
sandbox-worker → container --network none → destroy``) rather than in a
thread inside the backend process.

``run_code_sandboxed`` enqueues a ``workflow_code`` SandboxJob carrying the
code + upstream item, polls the job to completion, and returns the result
captured by the worker (``workflow_code_result`` event). If the sandbox
path cannot run (Docker unavailable / enqueue failure / poll failure) it
falls back to the in-process restricted exec — mirroring
``sandbox_worker._execute_in_process`` (dev-mode only, no isolation).
"""

import base64
import json
import logging
import threading
import time

from app.config import settings
from app.services.sandbox.sandbox_service import SandboxService

logger = logging.getLogger(__name__)

WORKFLOW_CODE_RUNNER = '''#!/usr/bin/env python
"""Workflow Code node runner — executes user code inside the sandbox.

Reads /input/config.json ({code, input_item}); executes the code with
restricted builtins (no imports, no filesystem, no network); writes
/output/result.json as {"output": <json>} or {"error": "<repr>"}.
Exit code 0 on success, 1 on error.
"""
import json
import math
import os
import sys

cfg = json.load(open("/input/config.json", "r", encoding="utf-8"))
code = cfg.get("code") or ""
input_item = cfg.get("input_item") or {}

safe_builtins = {
    "len": len, "str": str, "int": int, "float": float,
    "bool": bool, "list": list, "dict": dict, "range": range,
    "enumerate": enumerate, "zip": zip, "abs": abs, "min": min,
    "max": max, "sum": sum, "round": round, "isinstance": isinstance,
    "Exception": Exception, "ValueError": ValueError,
    "TypeError": TypeError, "KeyError": KeyError,
    "True": True, "False": False, "None": None, "print": print,
}
restricted = {
    "__builtins__": safe_builtins,
    "json": json,
    "math": math,
    "input": input_item,
}
box = {}
try:
    exec(compile(code, "<code>", "exec"), restricted, box)
except Exception as exc:  # noqa: BLE001
    result = {"error": repr(exc)}
else:
    if "output" not in box:
        result = {"error": "output not assigned"}
    else:
        out = box["output"]
        try:
            json.dumps(out)
        except TypeError:
            result = {"error": "output not JSON-serializable"}
        else:
            result = {"output": out}

os.makedirs("/output", exist_ok=True)
with open("/output/result.json", "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False)
sys.exit(0 if "output" in result else 1)
'''

_RUNNER_NAME = "workflow_code_runner.py"


def _restricted_exec(code: str, item: dict, timeout: int) -> dict:
    """In-process restricted exec — dev/test fallback (same semantics as the
    sandbox runner: restricted builtins, ``input`` = upstream item, must
    assign ``output``)."""
    safe_builtins = {
        "len": len, "str": str, "int": int, "float": float,
        "bool": bool, "list": list, "dict": dict, "range": range,
        "enumerate": enumerate, "zip": zip, "abs": abs, "min": min,
        "max": max, "sum": sum, "round": round, "isinstance": isinstance,
        "Exception": Exception, "ValueError": ValueError,
        "TypeError": TypeError, "KeyError": KeyError,
        "True": True, "False": False, "None": None, "print": print,
    }
    restricted = {
        "__builtins__": safe_builtins,
        "json": json,
        "math": __import__("math"),
        "input": item,
    }
    box: dict = {}

    def _run():
        try:
            exec(compile(code, "<code>", "exec"), restricted, box)
        except Exception as exc:  # noqa: BLE001
            box["_error"] = repr(exc)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return {"success": False, "text": f"Code timed out after {timeout}s"}
    if "_error" in box:
        return {"success": False, "text": f"Code error: {box['_error']}"}
    if "output" not in box:
        return {"success": False, "text": "Code must assign an `output` variable"}
    out = box["output"]
    try:
        json.dumps(out)
    except TypeError:
        return {"success": False, "text": "output is not JSON-serializable"}
    if isinstance(out, dict):
        return {"success": True, "text": json.dumps(out, ensure_ascii=False), "items": [out]}
    if isinstance(out, list):
        return {"success": True, "text": json.dumps(out, ensure_ascii=False), "items": out}
    return {"success": True, "text": str(out), "items": [{"value": out}]}


def _normalize_runner_result(result: dict) -> dict:
    """Convert the runner's raw result.json into the runtime's output shape.

    Runner writes ``{"output": <json>}`` or ``{"error": "<repr>"}``; the
    runtime shape is ``{"success", "text", "items"?}`` (same normalization
    as ``_restricted_exec`` so sandboxed and fallback paths agree).
    """
    if "error" in result:
        return {"success": False, "text": f"Code error: {result['error']}"}
    if "output" not in result:
        return {"success": False, "text": "Sandbox runner produced no output"}
    out = result["output"]
    if isinstance(out, dict):
        return {"success": True, "text": json.dumps(out, ensure_ascii=False), "items": [out]}
    if isinstance(out, list):
        return {"success": True, "text": json.dumps(out, ensure_ascii=False), "items": out}
    return {"success": True, "text": str(out), "items": [{"value": out}]}


def _job_timeout_seconds(code_timeout: int) -> int:
    """Code timeout + container overhead, capped (job-level hard timeout)."""
    return min(code_timeout + 30, 300)


def run_code_sandboxed(db, code: str, input_item: dict, timeout: int = 10) -> dict:
    """Run the Code node in the Docker sandbox; fall back to in-process exec.

    Returns the same shape as the previous in-process implementation:
    ``{"success", "text", "items"?}``.
    """
    try:
        input_package = {
            "runner_script": base64.b64encode(WORKFLOW_CODE_RUNNER.encode()).decode(),
            "runner_script_name": _RUNNER_NAME,
            # prepare_input_package materializes ONLY skill_config →
            # /input/config.json; the runner reads code + input_item from it.
            "skill_config": {"code": code, "input_item": input_item or {}},
            "instructions": "Run the workflow Code node in the sandbox.",
        }
        service = SandboxService(db)
        job = service.create_job(
            skill_name="workflow_code",
            input_package=input_package,
            image_name=settings.SANDBOX_IMAGE_PYTHON,
            timeout_seconds=_job_timeout_seconds(timeout),
        )
        logger.info("workflow code: enqueued sandbox job %s", job.id)

        deadline = time.monotonic() + _job_timeout_seconds(timeout)
        while time.monotonic() < deadline:
            # The creating session caches the job in its identity map (status
            # 'queued'); expire it so get_job re-reads the worker's updates
            # from the DB instead of returning the stale object forever.
            if db is not None:
                db.expire_all()
            cur = service.get_job(job.id)
            if cur is None:
                break
            if cur.status == "completed":
                for ev in (cur.events or []):
                    if getattr(ev, "event_type", None) == "workflow_code_result":
                        raw = (ev.data_json or {}).get("result")
                        if isinstance(raw, dict):
                            return _normalize_runner_result(raw)
                return {"success": False, "text": "Sandbox finished without a result"}
            if cur.status == "failed":
                # The runner may have captured a real error in result.json
                # even though the container exited non-zero — prefer it over
                # the (often empty) container stderr.
                for ev in (cur.events or []):
                    if getattr(ev, "event_type", None) == "workflow_code_result":
                        raw = (ev.data_json or {}).get("result")
                        if isinstance(raw, dict):
                            return _normalize_runner_result(raw)
                return {
                    "success": False,
                    "text": cur.error_message or "Sandbox execution failed",
                }
            time.sleep(1.0)
        return {"success": False, "text": f"Code timed out in sandbox after {timeout}s"}
    except Exception as exc:  # noqa: BLE001 — fallback, never crash the node
        logger.warning("workflow code: sandbox unavailable (%s) — in-process fallback", exc)
        return _restricted_exec(code, input_item, timeout)
