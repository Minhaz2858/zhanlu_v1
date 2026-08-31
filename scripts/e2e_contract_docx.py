"""E2E: 'Contract Performance for last month report in docx file' via the v3 stream."""
import json
import sys
import urllib.request

APP_ID = "14219d3d-015d-4e1d-bf2e-97c4ab6274ca"
BASE = f"http://localhost:5002/api/apps/{APP_ID}"

def post(path, payload, token=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())

def main():
    # 1. login
    try:
        login = post("/auth/login", {"email": "admin@zhanlu.dev", "password": "admin123"})
    except Exception as e:
        print("LOGIN FAIL:", e)
        # try alternate
        try:
            login = post("/auth/login", {"username": "admin@zhanlu.dev", "password": "admin123"})
            print("login alt ok")
        except Exception as e2:
            print("LOGIN ALT FAIL:", e2)
            sys.exit(1)
    token = login.get("access_token") or login.get("token") or ""
    print("login keys:", list(login.keys()))

    # 2. create conversation — project_id is REQUIRED: datasource bindings
    # are project-scoped (Ecisco BI project a3dc76e3-...), so without it
    # the data agent finds no bound sources.
    conv = post("/agents/conversations", {
        "agent_name": "ecisco_bi_assistant",
        "title": "E2E contract perf check",
        "metadata": {"project_id": "a3dc76e3-6d04-4bcf-9c14-55d1a92a4d07"},
    }, token)
    cid = conv.get("id") or conv.get("conversation", {}).get("id")
    print("conversation:", cid, "keys:", list(conv.keys()))

    # 3. stream the message
    body = {"role": "user", "content": "give me Contract Performance for last month report in docx file"}
    req = urllib.request.Request(
        BASE + f"/agents/conversations/v3/{cid}/messages/stream",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}" if token else ""},
    )
    final_content = ""
    events = []
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            buf = b""
            while True:
                chunk = r.read(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n\n" in buf:
                    raw, buf = buf.split(b"\n\n", 1)
                    line = raw.decode(errors="replace").strip()
                    if line.startswith("data: "):
                        try:
                            evt = json.loads(line[6:])
                        except Exception:
                            continue
                        events.append(evt)
                        t = evt.get("type")
                        if t == "delta":
                            final_content += evt.get("content") or ""
                        elif t in ("done", "error", "paused"):
                            print("EVENT:", t)
                            if t == "done":
                                final_content = evt.get("content") or final_content
                            if t == "error":
                                print("ERROR MSG:", evt.get("message"))
    except Exception as e:
        print("STREAM ERROR:", e)

    print("=== EVENTS (%d) ===" % len(events))
    types = {}
    for e in events:
        types[e.get("type")] = types.get(e.get("type"), 0) + 1
    print("event type counts:", types)
    print("=== FINAL CONTENT (first 4000) ===")
    print(final_content[:4000])
    with open("/tmp/e2e_contract_final.txt", "w") as f:
        f.write(final_content)
    print("saved /tmp/e2e_contract_final.txt")

if __name__ == "__main__":
    main()
