#!/usr/bin/env python3
"""Clean E2E repro with FULL bindings: project_id + agent metadata, then capture SSE."""
import json, time, urllib.request

BASE = "http://localhost:5002"
APP = "local-zhanlu-app"
PROJECT = "a3dc76e3-6d04-4bcf-9c14-55d1a92a4d07"

def post(url, body, token=None, timeout=60):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

tok = post(f"{BASE}/api/apps/{APP}/auth/login",
           {"email": "admin@zhanlu.dev", "password": "admin123"})["access_token"]

conv = post(f"{BASE}/api/apps/{APP}/agents/conversations",
            {"agent_name": "ecisco_bi_assistant",
             "project_id": PROJECT,
             "metadata": {"name": "E2E Docx Verify",
                          "description": "E2E verify docx delivery with bindings",
                          "project_id": PROJECT}},
            token=tok)
conv_id = conv["id"] if isinstance(conv, dict) else conv[0]["id"]
print(f"CONV_ID={conv_id}", flush=True)

body = {"content": "give me top 5 customer list for last month give me in docx file"}
data = json.dumps(body).encode()
req = urllib.request.Request(f"{BASE}/api/apps/{APP}/agents/conversations/v3/{conv_id}/messages/stream",
                             data=data, method="POST")
req.add_header("Content-Type", "application/json")
req.add_header("Authorization", f"Bearer {tok}")

start = time.time()
final_content = ""
tool_names = []
artifacts = []
with urllib.request.urlopen(req, timeout=300) as r:
    buf = b""
    while True:
        chunk = r.read(4096)
        if not chunk:
            break
        buf += chunk
        while b"\n\n" in buf:
            frame, buf = buf.split(b"\n\n", 1)
            line = frame.decode(errors="replace").strip()
            if not line.startswith("data: "):
                continue
            try:
                ev = json.loads(line[6:])
            except Exception:
                continue
            t = ev.get("type", "?")
            if t == "delta" and isinstance(ev.get("content"), str):
                final_content += ev["content"]
            elif t == "content_replace" and isinstance(ev.get("content"), str):
                final_content = ev["content"]
            elif t == "done" and isinstance(ev.get("content"), str):
                if len(ev["content"]) > len(final_content):
                    final_content = ev["content"]
                convd = ev.get("conversation") or {}
                for m in reversed((convd.get("messages") or [])):
                    if m.get("role") == "assistant":
                        for tc in (m.get("tool_calls") or []):
                            r2 = tc.get("results") or {}
                            if isinstance(r2, dict) and tc.get("name") == "ask_data_agent":
                                print(f"  ask_data_agent rows={len(r2.get('rows') or [])} sql_ok={bool(r2.get('sql'))}", flush=True)
                        break
            if t == "tool_progress":
                for tc in (ev.get("tool_calls") or []):
                    nm = tc.get("name")
                    if nm and nm not in tool_names:
                        tool_names.append(nm)
            if t == "artifact_created":
                a = ev.get("artifact") or {}
                artifacts.append(f"{a.get('artifact_type')}/{a.get('filename') or a.get('file_name')}")
            if t in ("delta", "done", "content_replace", "tool_progress", "artifact_created", "error"):
                print(f"[{time.time()-start:6.1f}s] {t} (len={len(ev.get('content') or '')})", flush=True)

print(f"\nTOOLS: {tool_names}", flush=True)
print(f"ARTIFACTS: {artifacts}", flush=True)
print(f"FINAL len={len(final_content)}: {final_content[:400]!r}", flush=True)
print(f"DONE in {time.time()-start:.1f}s", flush=True)
