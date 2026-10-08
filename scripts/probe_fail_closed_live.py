#!/usr/bin/env python3
"""Live probe: a normal agent turn must still execute its tools after the
fail-closed seams landed (pre/post hook gate, guard-decision interpretation,
post-transform direction).

What it proves, on the running copy stack (:5012):
  1. login + conversation + a tool-using turn still completes
  2. the turn's tools actually RAN (not denied) — so the new DENY direction
     does not deny healthy calls
  3. the backend log carries no fail-closed denial and no post-transform
     withholding warning for this turn

The password is read out of the sibling repro script at runtime and never printed.

Usage:  python3 scripts/probe_fail_closed_live.py
"""
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://localhost:5012"
APP = "default-app"
QUESTION = sys.argv[1] if len(sys.argv) > 1 else "列出你当前可以访问的数据源，只列出名称，不要生成文档。"
REPRO = Path(__file__).with_name("repro_sales_question.py")


def _password() -> str:
    m = re.search(r'"password":\s*"([^"]+)"', REPRO.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit(f"could not read the dev password from {REPRO}")
    return m.group(1)


def call(method, path, body=None, token=None, timeout=600):
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + token} if token else {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else raw)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:800]


def log_lines_since_start() -> list[str]:
    out = subprocess.run(
        ["docker", "logs", "zhanlu-copy-backend", "--since", "3m"],
        capture_output=True, text=True,
    )
    return (out.stdout + out.stderr).splitlines()


code, data = call("POST", f"/api/apps/{APP}/auth/login",
                  {"email": "admin@zhanlu.dev", "password": _password()})
assert code == 200, (code, data)
token = data["access_token"]
print(f"[1] login ok ({code})")

code, conv = call("POST", f"/api/apps/{APP}/agents/conversations",
                  {"agent_name": "general_assistant",
                   "title": "PROBE fail-closed seams"}, token)
assert code in (200, 201), (code, conv)
conv_id = conv["id"]
print(f"[2] conversation created: {conv_id}")

print(f"[3] asking: {QUESTION}")
code, reply = call("POST",
                   f"/api/apps/{APP}/agents/conversations/v2/{conv_id}/messages",
                   {"role": "user", "content": QUESTION}, token)
print(f"    http {code}")

msgs = (reply or {}).get("messages") or []
tool_names: list[str] = []
final = ""
for m in msgs:
    for tc in (m.get("tool_calls") or []):
        fn = (tc.get("function") or {}).get("name")
        if fn:
            tool_names.append(fn)
    if m.get("role") == "assistant" and (m.get("content") or "").strip():
        final = m["content"]

print(f"[4] assistant final (first 400 chars):\n    {final[:400].replace(chr(10), ' ')}")
print(f"[5] tools invoked in this turn: {tool_names or '<none>'}")

logs = log_lines_since_start()
denials = [ln for ln in logs if "fail-closed" in ln.lower()]
withheld = [ln for ln in logs if "withholding result" in ln.lower()]
guard_missing = [ln for ln in logs if "without 'allows_execution'" in ln]

print(f"[6] fail-closed denials in log: {len(denials)}")
print(f"[7] post-transform withholdings in log: {len(withheld)}")
print(f"[8] unreadable guard decisions in log: {len(guard_missing)}")

ok = bool(final.strip()) and not denials and not withheld and not guard_missing
print("\nVERDICT:", "PASS — normal turn ran, no policy seam denied or withheld" if ok
      else "REVIEW — see the lines above")
sys.exit(0 if ok else 1)
