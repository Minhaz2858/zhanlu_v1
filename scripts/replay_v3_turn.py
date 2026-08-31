#!/usr/bin/env python3
"""Replay a user message through the Zhanlu v3 agent loop (SSE stream) and
summarize the turn — tool calls, verify verdict, final content.

Use to verify agent-loop fixes without the frontend (see
zhanlu-development -> references/v3-loop-failure-debugging.md).

Run INSIDE the backend container (it has httpx):
    docker cp replay_v3_turn.py zhanlu-backend:/tmp/
    docker exec zhanlu-backend python /tmp/replay_v3_turn.py \
        --conv <conversation_id> \
        --content "Build a FULL-STACK REALTIME DASHBOARD (use create_fullstack_dashboard): ..."

After backend code changes ALWAYS restart first and confirm flags loaded:
    docker restart zhanlu-backend
    docker exec zhanlu-backend python -c \
        "from app.config import settings; print(settings.CLARIFY_SUSPENDS_TURN_ENABLED)"
"""
import argparse
import asyncio
import json
import sys

import httpx

DEFAULT_BASE = "http://localhost:5002"
DEFAULT_APP = "local-zhanlu-app"
DEFAULT_EMAIL = "admin@zhanlu.dev"
DEFAULT_PASSWORD = "admin123"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--conv", required=True, help="AgentConversation id")
    p.add_argument("--content", required=True, help="user message to replay")
    p.add_argument("--app", default=DEFAULT_APP)
    p.add_argument("--base", default=DEFAULT_BASE)
    p.add_argument("--project-id", default=None)
    p.add_argument("--lang", default="en")
    p.add_argument("--email", default=DEFAULT_EMAIL)
    p.add_argument("--password", default=DEFAULT_PASSWORD)
    return p.parse_args()


async def main(args: argparse.Namespace) -> None:
    async with httpx.AsyncClient(timeout=300) as c:
        r = await c.post(
            f"{args.base}/api/apps/{args.app}/auth/login",
            json={"email": args.email, "password": args.password},
        )
        tok = r.json().get("access_token") or r.json().get("token")
        if not tok:
            print("LOGIN FAILED", r.status_code, r.text[:300])
            sys.exit(1)
        headers = {"Authorization": f"Bearer {tok}"}

        body: dict = {"content": args.content, "role": "user", "lang": args.lang}
        if args.project_id:
            body["project_id"] = args.project_id

        tool_status: dict[str, list] = {}
        events: list[str] = []
        verify: str | None = None
        done_content = ""
        url = (
            f"{args.base}/api/apps/{args.app}/agents/conversations/v3/"
            f"{args.conv}/messages/stream"
        )
        async with c.stream("POST", url, json=body, headers=headers) as s:
            async for line in s.aiter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    ev = json.loads(line[6:])
                except Exception:
                    continue
                t = ev.get("type")
                events.append(t)
                if t == "tool_call":
                    tool_status.setdefault(ev.get("name", "?"), []).append(ev.get("args"))
                elif t in ("verify_passed", "verify_failed"):
                    verify = t
                elif t == "done":
                    done_content = ev.get("content") or ""

        print("EVENT TYPES:", {t: events.count(t) for t in set(events)})
        print("VERIFY:", verify)
        for name, calls in tool_status.items():
            print(f"  tool_call {name} x{len(calls)}")
        print("DONE CONTENT (first 800):")
        print(done_content[:800])


if __name__ == "__main__":
    _args = parse_args()
    asyncio.run(main(_args))
