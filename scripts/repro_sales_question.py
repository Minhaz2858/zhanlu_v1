#!/usr/bin/env python3
"""Reproduce the user's failing data question against the COPY stack and print
the agent's answer verbatim.

Failing turn (2026-09-18 03:15Z): conversation 0f14e019, agent general_assistant,
project "Data Analysis" (d28cb0b0 — which holds the aipdp warehouse KB),
question "本周销售情况 ，只统计 惠州伊斯科数据 ，直接输出，不要输出 文档"
answer claimed 0 rows. Ground truth: erp_v_sale_huizhou has 12 rows /
¥17,772,540 for the same week.
"""
import json
import sys
import urllib.error
import urllib.request

BASE = "http://localhost:5012"
APP = "default-app"
PROJECT = "d28cb0b0-fd61-453e-8967-df8953951a6d"   # "Data Analysis"
QUESTION = sys.argv[1] if len(sys.argv) > 1 else "本周销售情况 ，只统计 惠州伊斯科数据 ，直接输出，不要输出 文档"


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


code, data = call("POST", f"/api/apps/{APP}/auth/login",
                  {"email": "admin@zhanlu.dev", "password": "admin123"})
token = data["access_token"]
print(f"[login ok] {code}")

code, conv = call("POST", f"/api/apps/{APP}/agents/conversations",
                  {"agent_name": "general_assistant",
                   "title": f"REPRO 惠州伊斯科 thisweek {QUESTION[:12]}",
                   "project_id": PROJECT}, token)
print(f"[create conversation] {code} id={conv.get('id') if isinstance(conv, dict) else conv}")
conv_id = conv["id"]

print(f"[asking] {QUESTION}")
code, reply = call("POST",
                   f"/api/apps/{APP}/agents/conversations/v2/{conv_id}/messages",
                   {"role": "user", "content": QUESTION}, token)
print(f"[reply http {code}] conv={conv_id}")

msgs = (reply or {}).get("messages") or []
print(f"\n===== CONVERSATION HAS {len(msgs)} MESSAGES =====")
last_assistant = None
for m in msgs:
    role = m.get("role")
    content = m.get("content") or ""
    if role == "assistant":
        last_assistant = m
    tcs = m.get("tool_calls") or []
    print(f"\n--- {role} ({len(content)} chars, {len(tcs)} tool_calls) ---")
    if tcs:
        for tc in tcs:
            name = tc.get("name")
            args = tc.get("arguments_string") or json.dumps(tc.get("arguments"), ensure_ascii=False)
            res = json.dumps(tc.get("results"), ensure_ascii=False) if tc.get("results") is not None else ""
            print(f"    TOOL {name} status={tc.get('status')}")
            print(f"      args: {str(args)[:600]}")
            if res:
                print(f"      result: {res[:600]}")
    if role == "assistant":
        print("    CONTENT:\n" + "\n".join("      " + l for l in content.splitlines()[:60]))

print("\n\n===== LAST ASSISTANT MESSAGE (verbatim) =====")
print((last_assistant or {}).get("content", "(none)"))
print("\nCONV_ID_FOR_LOG_GREP=" + conv_id)
