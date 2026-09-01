#!/usr/bin/env python3
"""
Live E2E: prove the DOCX report is DYNAMIC and QUESTION-DRIVEN.

Asks the real agent (C5_C9 datasource, project 07d8d339-f287-4ead-b1e2-a5733a34a239)
a CEO-style question, waits for the artifact, downloads the DOCX via
?format=docx&force=true (fixed path) and verifies CEO markers:
  - cover subtitle "致：首席执行官（CEO）" (via dynamic cover subtitle)
  - period-over-period comparison table (月度核心指标对比 / Period-over-Period)
  - decision section (管理层决策与战略建议 / CEO Action Items)
"""
import json, sys, time, zipfile, re, urllib.request

API = "http://localhost:5002"
APP = "local-zhanlu-app"
PROJECT_ID = "07d8d339-f287-4ead-b1e2-a5733a34a239"
PROJECT_NAME = "C5_C9"
QUESTION = (
    "请从CEO视角生成一份2026年6月-7月销售运营分析与决策建议报告，"
    "包含月度核心指标对比、产品线与客户结构分析，以及给CEO的战略建议。"
)

def http(method, url, body=None, headers=None, timeout=90):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()

# 1) login
st, b = http("POST", f"{API}/api/apps/{APP}/auth/login",
             {"email": "admin@zhanlu.dev", "password": "admin123"})
tok = json.loads(b)["access_token"]
H = {"Authorization": f"Bearer {tok}"}
print("[1] login ok", flush=True)

# 2) create conversation
st, b = http("POST", f"{API}/api/apps/{APP}/agents/conversations",
             {"agent_name": "general_assistant",
              "metadata": {"project_id": PROJECT_ID, "project_name": PROJECT_NAME}},
             H)
conv = json.loads(b)
cid = conv.get("conversation_id") or conv.get("id") or conv.get("data", {}).get("id")
print("[2] conversation", cid, flush=True)

# 3) send message (streaming SSE) — collect final assistant content
st, b = http("POST", f"{API}/api/apps/{APP}/agents/conversations/v3/{cid}/messages/stream",
             {"message": QUESTION, "project_id": PROJECT_ID, "project_name": PROJECT_NAME},
             H, timeout=420)
print("[3] stream status", st, "bytes", len(b), flush=True)
sse = b.decode("utf-8", "replace")
artifact_ids = re.findall(r'"artifact_id"\s*:\s*"([0-9a-f-]{36})"', sse)
if not artifact_ids:
    artifact_ids = re.findall(r'artifact_id["\']?\s*[:=]\s*["\']([0-9a-f-]{36})', sse)
print("[3b] artifact_ids found in stream:", list(dict.fromkeys(artifact_ids))[:5], flush=True)

# 4) list artifacts for conversation
st, b = http("GET", f"{API}/api/artifacts?conversation_id={cid}", None, H)
arts = json.loads(b)
items = arts if isinstance(arts, list) else arts.get("items", arts.get("artifacts", []))
print("[4] artifacts:", [(a.get("id"), a.get("artifact_type"), a.get("title")) for a in items], flush=True)

docx_id = next((a["id"] for a in items if a.get("artifact_type") == "docx"), None)
if not docx_id and artifact_ids:
    docx_id = artifact_ids[0]

if not docx_id:
    print("[FAIL] no docx artifact produced", flush=True)
    sys.exit(1)

# 5) download with force re-render (fixed path)
st, b = http("GET", f"{API}/api/artifacts/{docx_id}/download?format=docx&force=true", None, H)
open("/tmp/ceo_live.docx", "wb").write(b)
print("[5] download status", st, "bytes", len(b), flush=True)

# 6) verify CEO markers in the docx body text
z = zipfile.ZipFile("/tmp/ceo_live.docx")
xml = z.read("word/document.xml").decode("utf-8", "replace")
txt = " ".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", xml, re.S))
heads = re.findall(r'<w:pStyle w:val="Heading(\d)"', xml)
print("[6] body_chars", len(txt), "headings", len(heads), flush=True)
markers = {
    "CEO cover 致CEO": "首席执行官",
    "Period table": "月度核心指标对比",
    "Period table EN": "Period-over-Period",
    "Decision section": "决策与战略建议",
    "Decision EN": "CEO Action",
    "MoM +159": "+159",
    "Exec summary": "核心摘要",
}
for name, kw in markers.items():
    print(f"  {name}: {'FOUND' if kw in txt else 'missing'}", flush=True)
print("[7] first 700 chars:\n", txt[:700], flush=True)
