#!/usr/bin/env python3
"""Clean E2E repro with proper bindings (conv-e2e-0001), capture SSE, then verify artifact."""
import json, time, urllib.request

BASE = "http://localhost:5002"
APP = "local-zhanlu-app"

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

body = {"content": "give me top 5 customer list for last month give me in docx file"}
data = json.dumps(body).encode()
req = urllib.request.Request(f"{BASE}/api/apps/{APP}/agents/conversations/v3/conv-e2e-0001/messages/stream",
                             data=data, method="POST")
req.add_header("Content-Type", "application/json")
req.add_header("Authorization", f"Bearer {tok}")

start = time.time()
final_content = ""
tool_names = []
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
            if t == "tool_progress":
                for tc in (ev.get("tool_calls") or []):
                    nm = tc.get("name")
                    if nm and nm not in tool_names:
                        tool_names.append(nm)
            if t in ("delta", "done", "content_replace", "tool_progress", "artifact_created", "error", "quality_eval"):
                extra = ""
                if "artifact" in ev:
                    a = ev["artifact"]
                    extra = f" artifact={a.get('artifact_id','')[:8]}/{a.get('artifact_type')}/{a.get('filename') or a.get('file_name')}"
                if t in ("delta", "content_replace", "done"):
                    extra += f" len={len(ev.get('content') or '')}"
                print(f"[{time.time()-start:6.1f}s] {t}{extra}", flush=True)

print(f"\nTOOLS: {tool_names}", flush=True)
print(f"FINAL len={len(final_content)}: {final_content[:300]!r}", flush=True)
print(f"DONE in {time.time()-start:.1f}s", flush=True)
