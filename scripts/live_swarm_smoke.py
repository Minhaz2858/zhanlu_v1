"""Live swarm smoke test — proves the full team path works in the container."""
import asyncio
import logging
logging.basicConfig(level=logging.WARNING)

from app.database import SessionLocal
from app.services.tool_handlers import swarm_tools  # noqa: F401  ensure registration

# 1. Create a team via the same handler the agent would call
res = swarm_tools._handle_create_team({"name": "live-smoke", "description": "e2e"}, None, None)
print("create_team:", res)
team_id = res.get("team_id")
assert team_id, res

# 2. List teams
teams = swarm_tools._handle_list_teams({}, None, None)
print("list_teams count:", len(teams.get("teams", [])))

# 3. Spawn a real worker (harness path now, AGENT_HARNESS_ENABLED=true)
res2 = swarm_tools._handle_spawn_agent({
    "team_id": team_id,
    "agent_name": "worker",
    "task": "Answer in one short sentence: what is 2+2?",
    "member_name": "smoke-worker",
}, None, None)
print("spawn_agent:", res2)

# 4. Check the mailbox for the worker's answer
import time
time.sleep(2)
msgs = swarm_tools._handle_get_messages({"team_id": team_id, "member_name": "main"}, None, None)
print("get_messages keys:", list(msgs.keys()))
msgs_list = msgs.get("messages") or []
print("message count:", len(msgs_list))
for m in msgs_list[:3]:
    print(" -", str(m)[:160])

# 5. Orchestrate a small batch with retry policy
res3 = swarm_tools._handle_orchestrate({
    "team_id": team_id,
    "tasks": [
        {"agent_name": "worker", "task": "Say OK in one word"},
        {"agent_name": "explore", "task": "Say DONE in one word"},
    ],
}, None, None)
print("orchestrate:", str(res3)[:300])

print("SWARM_SMOKE_OK")
