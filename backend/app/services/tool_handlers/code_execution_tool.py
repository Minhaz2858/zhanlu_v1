"""execute_code tool — sandboxed Python **with programmatic tool calling (PTC)**.

The sandbox keeps its original posture (restricted builtins, import whitelist,
scrubbed environment, private temp dir, hard timeout) and adds one capability:
when the block runs inside an agent turn, the code can call the agent's own
tools directly:

    rows = call_tool("ask_data_agent", question="top 10 customers by revenue")
    other = call_tool("ask_data_agent", question="same for Q3", budget_seconds=120)

    # ...pandas/numpy work across both answers...
    print(summary)

Sub-calls are dispatched through the *same* pipeline the loop uses
(``app.services.agent_loop.tool_pipeline``): guard chain, permission check,
registry dispatch, post-execute transforms. Consequences worth knowing:

- A denied sub-call raises inside the code (so the model can adapt) instead of
  silently returning empty data.
- ``execute_code`` itself can never be called recursively, interactive tools
  (``clarify``) cannot run inside a block, a block is capped at
  ``PTC_SUB_CALL_LIMIT`` sub-calls, and a call that needs the user's approval is
  refused rather than paused.
- Sub-calls are emitted as chat activity steps, so the user still sees what ran.

Transport: a Unix socket inside the block's private temp dir (0700). stdout
stays reserved for the program's own output — the model never sees protocol
traffic.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.services.agent_loop.tool_pipeline import (
    PipelineDenied,
    active_pipeline,
    dispatch_sub_call,
)
from app.services.tool_registry import registry
from app.services.tool_security import redact_secrets, truncate_output

logger = logging.getLogger(__name__)

# Modules the sandbox is allowed to import.
# Standard library + DB drivers + data-science packages needed for
# autonomous data analysis.  Security boundary is the restricted-builtins
# wrapper (no exec, eval, open, subprocess) — not the import whitelist.
_ALLOWED_IMPORTS = {
    # stdlib
    "math", "json", "re", "collections", "itertools", "functools",
    "datetime", "decimal", "statistics", "string",
    # DB drivers (top-level packages)
    "pymysql", "mysql",
    # ORM
    "sqlalchemy",
    # Data science
    "pandas", "numpy",
}

# Cap on a single sub-call payload handed to the code (characters of JSON).
# A code block that pulls a 50 MB table into memory helps nobody; the message
# tells the model to narrow the query instead.
_MAX_SUB_CALL_PAYLOAD_CHARS = 200_000


_PTC_CHILD_PREAMBLE = '''
import json as _json
import os as _os
import socket as _socket
import sys as _sys

_PTC_SOCK = _os.environ.get("ZHANLU_PTC_SOCK") or ""
_PTC_TIMEOUT = __PTC_TIMEOUT__


def _ptc_call_raw(name, args):
    if not _PTC_SOCK:
        raise RuntimeError(
            "call_tool is unavailable in this context: this code block is not "
            "running inside an agent turn. Use a normal tool call instead."
        )
    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    sock.settimeout(_PTC_TIMEOUT)
    try:
        sock.connect(_PTC_SOCK)
        stream = sock.makefile("rwb")
        stream.write((_json.dumps({"tool": name, "args": args}, default=str) + "\\n").encode("utf-8"))
        stream.flush()
        line = stream.readline()
    finally:
        try:
            sock.close()
        except Exception:
            pass
    if not line:
        raise RuntimeError("call_tool: the agent loop closed the connection")
    response = _json.loads(line.decode("utf-8"))
    if not response.get("ok"):
        raise RuntimeError("tool '%s' failed: %s" % (name, response.get("error")))
    return response.get("result")


def call_tool(name, *args, **kwargs):
    """Call one of the agent's tools and return its result dict.

    call_tool("read_file", path="data.csv")
    call_tool("ask_data_agent", {"question": "revenue by region"})

    Raises RuntimeError if the call is denied or the tool fails, so the code
    can catch it and continue.
    """
    if args:
        if len(args) == 1 and isinstance(args[0], dict):
            payload = dict(args[0])
            payload.update(kwargs)
        else:
            raise TypeError(
                "call_tool(name, **kwargs) or call_tool(name, {..args..}) — "
                "got %d positional arguments" % len(args)
            )
    else:
        payload = dict(kwargs)
    return _ptc_call_raw(name, payload)
'''


def _wrap_code(code: str) -> str:
    """Wrap user code in a sandbox with restricted builtins (+ PTC bridge).

    ``call_tool`` is always defined: without an active turn its socket env var
    is absent and the call raises a RuntimeError explaining why, which is far
    clearer to a model than a NameError.
    """
    import_whitelist = repr(list(_ALLOWED_IMPORTS))
    preamble = _PTC_CHILD_PREAMBLE.replace(
        "__PTC_TIMEOUT__", str(int(settings.PTC_SUB_CALL_TIMEOUT))
    )
    ptc_globals = '{"call_tool": call_tool, "ptc_available": bool(_PTC_SOCK)}'

    wrapper = f'''{preamble}
import builtins as _builtins_mod

# --- Build restricted builtins ---
_safe_builtins = dict(_builtins_mod.__dict__)
for _name in ("exec", "eval", "compile", "open", "input", "breakpoint", "exit", "quit"):
    _safe_builtins.pop(_name, None)

_real_import = _builtins_mod.__import__
_allowed = set({import_whitelist})

def _safe_import(name, *args, **kwargs):
    top = name.split(".")[0]
    if top not in _allowed:
        raise ImportError(f"Import of '{{name}}' is not allowed. Allowed: {{', '.join(sorted(_allowed))}}")
    return _real_import(name, *args, **kwargs)

_safe_builtins["__import__"] = _safe_import

# --- Execute user code with restricted builtins ---
_user_globals = {{"__builtins__": _safe_builtins, "__name__": "__main__"}}
_user_globals.update({ptc_globals})
if not _user_globals.get("ptc_available"):
    _user_globals["ptc_available"] = False
_user_code = {repr(code)}
exec(compile(_user_code, "<sandbox>", "exec"), _user_globals)
'''
    return wrapper


def _sub_call_payload(result) -> dict | str:
    """Shape one sub-call result for the sandbox, capped."""
    try:
        encoded = json.dumps(result, default=str, ensure_ascii=False)
    except Exception:  # noqa: BLE001 — never fail a call over serialization
        encoded = json.dumps({"success": True, "result": str(result)}, ensure_ascii=False)
    if len(encoded) > _MAX_SUB_CALL_PAYLOAD_CHARS:
        return {
            "success": False,
            "error": (
                f"result too large for a code block ({len(encoded)} chars > "
                f"{_MAX_SUB_CALL_PAYLOAD_CHARS}). Narrow the request (fewer rows / "
                f"smaller range) or handle it as a direct tool call."
            ),
            "truncated_preview": encoded[:2000],
        }
    return json.loads(encoded)


async def _handle_sub_call(request: dict, pipeline, sink: list | None = None) -> dict:
    """Run one sandbox-requested tool call through the active pipeline."""
    from app.services.agent_tools import TOOL_NAME_ALIASES

    tool_name = str(request.get("tool") or "").strip()
    tool_args = request.get("args")
    if not isinstance(tool_args, dict):
        tool_args = {"value": tool_args} if tool_args is not None else {}
    tool_name = TOOL_NAME_ALIASES.get(tool_name, tool_name)

    if not tool_name:
        return {"ok": False, "error": "call_tool requires a tool name", "code": "bad_request"}

    def _record(success: bool) -> None:
        if sink is not None:
            sink.append({"tool_name": tool_name, "success": success})

    try:
        result = await asyncio.wait_for(
            dispatch_sub_call(tool_name, tool_args, pipeline=pipeline),
            timeout=settings.PTC_SUB_CALL_TIMEOUT,
        )
    except PipelineDenied as denied:
        logger.info("PTC sub-call denied: %s (%s)", denied.tool_name, denied.code)
        _record(False)
        return {"ok": False, "error": denied.reason, "code": denied.code}
    except asyncio.TimeoutError:
        _record(False)
        return {
            "ok": False,
            "error": f"tool '{tool_name}' timed out after {settings.PTC_SUB_CALL_TIMEOUT}s",
            "code": "timeout",
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("PTC sub-call %s errored: %s", tool_name, exc)
        _record(False)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "code": "error"}

    _record(bool(result.get("success")))
    return {"ok": True, "result": _sub_call_payload(result)}


def _make_sub_call_server(path: str, pipeline, sink: list):
    """Build the Unix-socket server callback for one code block."""

    async def _on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    request = json.loads(line.decode("utf-8"))
                except Exception:  # noqa: BLE001 — malformed frame: drop it
                    logger.warning("PTC: malformed request frame dropped")
                    break
                response = await _handle_sub_call(request, pipeline, sink)
                writer.write((json.dumps(response, default=str) + "\n").encode("utf-8"))
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    return _on_client


def _private_tmpdir() -> str:
    """A 0700 temp dir with a short path (Unix socket paths are length-limited)."""
    for candidate in ("/tmp", tempfile.gettempdir()):
        try:
            return tempfile.mkdtemp(prefix="zhanlu_exec_", dir=candidate)
        except OSError:
            continue
    return tempfile.mkdtemp(prefix="zhanlu_exec_")


async def _execute_code(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    code = args.get("code", "").strip()

    if not code:
        return {"success": False, "error": "code is required"}

    pipeline = active_pipeline() if settings.PTC_ENABLED else None
    use_ptc = pipeline is not None
    timeout = settings.PTC_EXECUTION_TIMEOUT if use_ptc else settings.CODE_EXECUTION_TIMEOUT
    if use_ptc and pipeline.sub_call_limit > settings.PTC_SUB_CALL_LIMIT:
        pipeline.sub_call_limit = settings.PTC_SUB_CALL_LIMIT

    tmpdir = _private_tmpdir()
    sock_path = os.path.join(tmpdir, "tool.sock")
    server = None
    sub_calls: list[dict] = []
    try:
        if use_ptc:
            # Socket paths are limited (~104 chars on macOS): fall back to a
            # computation-only block rather than failing the call outright.
            if len(sock_path) > 100:
                logger.warning("PTC socket path too long (%s) — running without PTC", sock_path)
                use_ptc = False
                timeout = settings.CODE_EXECUTION_TIMEOUT
            else:
                server = await asyncio.start_unix_server(
                    _make_sub_call_server(sock_path, pipeline, sub_calls), path=sock_path
                )
                os.chmod(sock_path, 0o600)

        script_path = Path(tmpdir, "exec.py")
        script_path.write_text(_wrap_code(code), encoding="utf-8")

        clean_env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": tmpdir,
            "TMPDIR": tmpdir,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
        }
        if use_ptc:
            clean_env["ZHANLU_PTC_SOCK"] = sock_path

        proc = await asyncio.create_subprocess_exec(
            "python3",
            str(script_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=clean_env,
            cwd=tmpdir,
        )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            # Teardown must reach quiescence, not just request it.
            proc.kill()
            await proc.wait()
            return {
                "success": False,
                "error": f"Code execution timed out after {timeout}s",
                "stdout": "",
                "stderr": f"Timeout: killed after {timeout} seconds",
                "ptc": {"enabled": use_ptc, "sub_calls": len(sub_calls)},
            }

        stdout = truncate_output((stdout_b or b"").decode("utf-8", "replace"), settings.TOOL_MAX_OUTPUT_CHARS)
        stderr = truncate_output(
            (stderr_b or b"").decode("utf-8", "replace"), settings.TOOL_MAX_OUTPUT_CHARS // 2
        )

        # Redact secrets from output
        stdout = redact_secrets(stdout)
        stderr = redact_secrets(stderr)

        result = {
            "success": proc.returncode == 0,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": proc.returncode,
            "ptc": {
                "enabled": use_ptc,
                "sub_calls": len(sub_calls),
                "tools": [c["tool_name"] for c in sub_calls],
            },
        }
        if sub_calls:
            result["ptc"]["call_detail"] = [
                {"tool": c["tool_name"], "success": c["success"]} for c in sub_calls
            ]
        return result

    except Exception as e:
        return {"success": False, "error": f"Execution failed: {str(e)}"}
    finally:
        if server is not None:
            server.close()
            try:
                await server.wait_closed()
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Schema & Registration
# ---------------------------------------------------------------------------

EXECUTE_CODE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "execute_code",
        "description": (
            "Execute Python code in a sandboxed environment. "
            "Useful for calculations, data analysis, string manipulation, and algorithms. "
            "Allowed imports: math, json, re, collections, itertools, functools, datetime, decimal, statistics, string (stdlib), "
            "pymysql, mysql, sqlalchemy (DB drivers), pandas, numpy (data science). "
            "No file I/O, no network access, no subprocess. "
            "Output (stdout) is captured and returned. Use print() to return results. "
            "PROGRAMMATIC TOOL CALLING: inside the block you can call the agent's own tools "
            "with call_tool(name, **arguments) — e.g. "
            "rows = call_tool('ask_data_agent', question='revenue by region for Q3'). "
            "Prefer this when a question needs SEVERAL tool calls, a loop over entities, or "
            "post-processing between calls: one code block replaces many round-trips, and only "
            "what you print enters the conversation. A denied or failing call raises "
            "RuntimeError, so wrap calls you can recover from in try/except; "
            "'ptc_available' tells you whether call_tool works in the current context."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": (
                        "Python code to execute. Use print() for output. "
                        "Call agent tools with call_tool('tool_name', **arguments)."
                    ),
                },
            },
            "required": ["code"],
        },
    },
}

registry.register(
    name="execute_code",
    schema=EXECUTE_CODE_SCHEMA,
    handler=_execute_code,
    category="code",
    enabled_by_default=True,
    description="Execute Python code in a sandbox, with programmatic tool calling.",
)
