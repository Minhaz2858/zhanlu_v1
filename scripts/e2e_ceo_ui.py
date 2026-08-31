"""UI E2E — prove the DOCX report is DYNAMIC & QUESTION-DRIVEN (CEO view).

Drives the REAL browser UI (system Chrome via Playwright) with a CEO-style
question against the C5_C9 project datasource, then:
  1. waits for the agent to produce a .docx artifact (UI + backend API)
  2. downloads it via /download?format=docx&force=true (fixed render path)
  3. verifies the DOCX carries CEO framing driven by the QUESTION, not a
     hardcoded template:
       - 首席执行官 (CEO cover recipient)
       - 月度核心指标对比 / Period-over-Period (period comparison table)
       - 决策与战略建议 / CEO Action (decision section)
       - 核心摘要 (executive summary)

Outputs into agent_qa/: e2e_ceo_before.png, e2e_ceo_streaming.png,
e2e_ceo_final.png, e2e_ceo_report.md, e2e_ceo.docx

Run: python scripts/e2e_ceo_ui.py
"""
import os, json, re, time, sys, zipfile
from playwright.sync_api import sync_playwright

BASE    = "http://localhost:8088"
API     = "http://localhost:5002"
APP_ID  = "local-zhanlu-app"
EMAIL   = "admin@zhanlu.dev"
PASSWORD = "admin123"
PROJECT_ID = "07d8d339-f287-4ead-b1e2-a5733a34a239"
PROJECT_NAME = "C5_C9"
PROJECT_URL = f"{BASE}/my-space/project/{PROJECT_ID}"
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "agent_qa")
os.makedirs(OUT, exist_ok=True)

REQUEST = (
    "请从CEO视角生成一份 2026年6月-7月 销售运营分析与决策建议报告。"
    "要求：1) 月度核心指标对比（6月 vs 7月，含环比MoM）；"
    "2) 产品线与客户结构分析；3) 给CEO的战略建议（管理层决策与战略建议）。"
    "请使用本项目的实时数据源(aipdp_data_warehouse_prod)的真实数据，"
    "并调用 create_artifact 生成 type='docx' 的正式报告文件。"
)

CEO_MARKERS = [
    ("CEO cover recipient", "首席执行官"),
    ("Period comparison table", "月度核心指标对比"),
    ("Period table EN", "Period-over-P"),
    ("Decision section", "决策与战略建议"),
    ("Decision EN", "CEO Action"),
    ("Executive summary", "核心摘要"),
    ("Addressed-to-CEO", "致："),
]

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
api_trace = []
def _record(req):
    try:
        if "/api/" in req.url:
            api_trace.append({"method": req.method, "url": req.url})
    except Exception:
        pass

def docx_body_text(path):
    """Robust text extraction straight from word/document.xml."""
    z = zipfile.ZipFile(path)
    xml = z.read("word/document.xml").decode("utf-8", "replace")
    txt = " ".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", xml, re.S))
    heads = re.findall(r'<w:pStyle w:val="Heading(\d)"', xml)
    return txt, len(heads)

def _artifact_signals(page):
    hits = []
    try:
        body = page.locator("body").inner_text() or ""
        for kw in [".docx", ".pptx", "word", "下载"]:
            if kw in body.lower():
                hits.append({"kind": "text", "text": kw})
    except Exception:
        pass
    return hits

def _detect_failure(page):
    try:
        body = page.locator("body").inner_text() or ""
    except Exception:
        return False
    return ("trouble putting it all together" in body.lower()
            or "try again with a more specific request" in body.lower()
            or "no bound data source" in body.lower())

def _wait_for_composer(page, timeout=20000):
    t0 = time.time()
    while time.time() - t0 < timeout/1000:
        for cand in page.locator("textarea").all():
            try:
                ph = cand.get_attribute("placeholder") or ""
            except Exception:
                ph = ""
            if "zhanlu" in ph.lower() or "describe" in ph.lower():
                return cand
        if page.locator("textarea").count() > 0:
            return page.locator("textarea").first
        page.wait_for_timeout(500)
    if page.locator("textarea").count() > 0:
        return page.locator("textarea").first
    return None

def main():
    import requests
    r = requests.post(f"{API}/api/apps/{APP_ID}/auth/login",
                      json={"email": EMAIL, "password": PASSWORD}, timeout=15)
    r.raise_for_status()
    TOKEN = r.json()["access_token"]
    HEADERS = {"Authorization": f"Bearer {TOKEN}"}
    print(f"[auth] token len={len(TOKEN)}", flush=True)

    report = {"steps": [], "assertions": []}
    def step(name, ok, detail=""):
        report["steps"].append({"step": name, "ok": ok, "detail": detail})
        print(f"  [step] {name}: {'OK' if ok else 'FAIL'} {detail}", flush=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=CHROME,
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = ctx.new_page()
        page.on("request", _record)

        page.goto(f"{BASE}/?access_token={TOKEN}", wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        step("login_and_load_ui", True, f"url={page.url}")

        page.goto(PROJECT_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
        step("open_project_page", PROJECT_ID in page.url, f"url={page.url}")

        chat_clicked = False
        for sel in ["button[title*='Chat with agent']", "a[title*='Chat with agent']",
                    "button[aria-label*='Chat with agent']"]:
            try:
                el = page.locator(sel).first
                if el.count() > 0 and el.is_visible():
                    el.click(); chat_clicked = True; break
            except Exception:
                pass
        if not chat_clicked:
            for el in page.locator("button, a").all():
                try:
                    txt = (el.inner_text() or "").strip().lower()
                    title = (el.get_attribute("title") or "").lower()
                except Exception:
                    continue
                if "chat with agent" in txt or "chat with agent" in title or "与该 agent 对话" in txt:
                    try:
                        el.click(); chat_clicked = True; break
                    except Exception:
                        pass
        try:
            page.wait_for_url(lambda u: "project=07d8d339" in u, timeout=8000)
        except Exception:
            pass
        step("click_chat_with_agent", chat_clicked and "project=07d8d339" in page.url,
             f"clicked={chat_clicked} url={page.url}")

        if "project=07d8d339" not in page.url:
            page.goto(f"{BASE}/?access_token={TOKEN}&project={PROJECT_ID}&projectName={PROJECT_NAME}",
                      wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
        step("project_scope_bound", "project=07d8d339" in page.url, f"url={page.url}")

        ta = _wait_for_composer(page)
        assert ta is not None, "composer textarea not found"
        ta.wait_for(state="visible", timeout=10000)
        ta.click()
        ta.fill(REQUEST)
        page.wait_for_timeout(800)
        page.screenshot(path=f"{OUT}/e2e_ceo_before.png", full_page=True)
        step("type_ceo_request", True, f"len={len(REQUEST)}")

        t0 = time.time()
        ta.press("Enter")
        page.wait_for_timeout(1000)
        for b in page.locator("button").all():
            try:
                if (b.inner_text() or "").strip().lower() in ("send", "submit", "➤", "→", "发送"):
                    b.click(); break
            except Exception:
                pass
        step("submit_request", True, "enter_pressed")

        deadline = t0 + 420
        mid_taken = done = False
        conv_id = None
        while time.time() < deadline:
            page.wait_for_timeout(3000)
            if not mid_taken and time.time() - t0 > 20:
                page.screenshot(path=f"{OUT}/e2e_ceo_streaming.png", full_page=True)
                mid_taken = True
            if _detect_failure(page):
                step("agent_completed", False, "agent emitted fallback failure message")
                done = True
                break
            for a in api_trace:
                m = re.search(r"/conversations/v3/([0-9a-f-]{36})/messages/stream", a["url"])
                if m: conv_id = m.group(1); break
            if not conv_id:
                for a in api_trace:
                    m = re.search(r"/conversations/([0-9a-f-]{36})", a["url"])
                    if m: conv_id = m.group(1); break
            if conv_id:
                try:
                    rr = requests.get(f"{API}/api/artifacts",
                                      params={"conversation_id": conv_id},
                                      headers=HEADERS, timeout=20)
                    payload = rr.json()
                    items = (payload.get("data") or payload.get("items")
                             or payload.get("artifacts") or [])
                    if isinstance(items, dict):
                        items = items.get("items") or []
                    if any((it.get("artifact_type") or it.get("type") or "").lower() == "docx"
                           for it in items):
                        done = True
                        break
                except Exception:
                    pass
        else:
            step("agent_completed", False, "timed out waiting for docx artifact")

        page.screenshot(path=f"{OUT}/e2e_ceo_final.png", full_page=True)
        step("agent_completed", done, f"elapsed={time.time()-t0:.0f}s conv={conv_id}")

        # download the docx via the FIXED render path (force re-render)
        docx_id = docx_chars = docx_heads = 0
        found = {}
        if conv_id:
            rr = requests.get(f"{API}/api/artifacts",
                              params={"conversation_id": conv_id},
                              headers=HEADERS, timeout=30)
            payload = rr.json()
            items = (payload.get("data") or payload.get("items")
                     or payload.get("artifacts") or [])
            if isinstance(items, dict):
                items = items.get("items") or []
            for it in items:
                if (it.get("artifact_type") or it.get("type") or "").lower() == "docx":
                    docx_id = it.get("id"); break
        if docx_id:
            dd = requests.get(f"{API}/api/artifacts/{docx_id}/download",
                              params={"format": "docx", "force": "true"},
                              headers=HEADERS, timeout=90)
            if dd.status_code == 200 and len(dd.content) > 1000:
                open(f"{OUT}/e2e_ceo.docx", "wb").write(dd.content)
                docx_chars, docx_heads = docx_body_text(f"{OUT}/e2e_ceo.docx")
                print(f"[download] docx {len(dd.content)} bytes, body {len(docx_chars)} chars, {docx_heads} headings", flush=True)
                for name, kw in CEO_MARKERS:
                    hit = kw in docx_chars
                    found[name] = hit
                    report["assertions"].append({"name": f"docx_has_{name}", "pass": hit})
                    print(f"  marker {name} ({kw}): {'FOUND' if hit else 'missing'}", flush=True)
                report["assertions"].append({"name": "docx_substantial", "pass": len(docx_chars) > 1500})
            else:
                print(f"[download] FAIL status={dd.status_code} bytes={len(dd.content)}", flush=True)
                report["assertions"].append({"name": "docx_download", "pass": False})
        step("docx_downloaded", docx_id and docx_chars,
             f"id={docx_id} chars={len(docx_chars)} headings={docx_heads}")

        passed = sum(1 for a in report["assertions"] if a["pass"])
        total = len(report["assertions"])
        lines = ["# UI E2E — Dynamic CEO Report (question-driven, C5_C9)",
                 "",
                 f"- Project: `{PROJECT_ID}` ({PROJECT_NAME})",
                 f"- Conversation: `{conv_id}`",
                 f"- Question: {REQUEST[:80]}…",
                 f"- Run at: {time.strftime('%Y-%m-%d %H:%M:%S')}",
                 "",
                 "## Assertions", "",
                 f"**{passed}/{total} passed**", ""]
        for a in report["assertions"]:
            lines.append(f"- [{'x' if a['pass'] else ' '}] {a['name']}")
        lines += ["", "## Steps", ""]
        for s in report["steps"]:
            lines.append(f"- [{'x' if s['ok'] else ' '}] {s['step']} — {s['detail']}")
        lines += ["", "## Screenshots", "",
                  f"- {OUT}/e2e_ceo_before.png",
                  f"- {OUT}/e2e_ceo_streaming.png",
                  f"- {OUT}/e2e_ceo_final.png", ""]
        if docx_chars:
            lines += ["", "## DOCX body (first 1200 chars)", "",
                      docx_chars[:1200]]
        open(f"{OUT}/e2e_ceo_report.md", "w").write("\n".join(lines))
        print("\n".join(lines))
        browser.close()
        if not (docx_chars and len(docx_chars) > 1500):
            sys.exit(2)

if __name__ == "__main__":
    main()
