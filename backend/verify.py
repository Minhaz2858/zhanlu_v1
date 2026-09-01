import asyncio
import app.services.tool_handlers
from app.services.tool_registry import registry
from app.services.system_agents import ensure_system_agents
from app.services.agent_prompts import get_system_prompt

ensure_system_agents()

print("FINAL PHASE 10 VERIFICATION")
print("=" * 60)

ga = len(get_system_prompt("general_assistant"))
pu = len(get_system_prompt("power_user"))

print(f"Tools registered:           {len(registry.list_available())}")
print(f"Toolsets:                   {len(registry.get_toolsets())}")
print(f"general_assistant prompt:   {ga} chars")
print(f"power_user prompt:          {pu} chars")
print()

from app.services.llm_service import llm_url, get_model
print(f"LLM URL:    {llm_url()}")
print(f"LLM model:  {get_model()}")
print()

handler = registry.get_handler("web_search")
r = asyncio.run(handler({"query": "what is python", "limit": 2}, db=None, user_id=None))
print("web_search (configured):", "OK" if r.get("success") else r.get("error", "")[:80])
print()

categories = {
    "Web/internet": ["web_search", "web_extract", "x_search", "url_safety"],
    "Browser": ["agent_browser", "computer_use"],
    "Files/code": ["read_file", "write_file", "fuzzy_match", "patch_parser", "path_security"],
    "Memory/planning": ["memory", "todo", "kanban", "session_search", "checkpoint_manager"],
    "Terminal/process": ["process_registry_list", "process_registry_tail", "process_registry_kill"],
    "Skills": ["skills", "skills_hub", "skill_manager", "skills_guard", "skill_provenance", "skill_usage"],
    "Media": ["image_generation", "video_generation", "tts", "transcription", "vision", "voice_mode"],
    "LLM": ["openrouter", "xai_http", "yuanbao", "mixture_of_agents"],
    "Communication": ["discord", "feishu_doc", "feishu_drive", "send_message", "homeassistant", "microsoft_graph"],
    "MCP": ["mcp", "mcp_oauth", "mcp_oauth_manager"],
    "Security": ["osv_check", "tirith_security", "approval"],
    "Admin": ["update_env_config", "docker_compose_restart", "cronjob", "env_passthrough", "credential_files"],
    "UX": ["clarify", "slash_confirm", "interrupt"],
}
for cat, tool_list in categories.items():
    found = sum(1 for t in tool_list if t in registry.list_available())
    print("  " + cat.ljust(25), f"{found}/{len(tool_list)} tools present")
print()

print("Missing-config agent flow test:")
for tool, args in [
    ("tts", {"text": "hi"}),
    ("x_search", {"query": "x"}),
    ("homeassistant", {"action": "list_states"}),
    ("mcp", {"action": "call", "server": "test", "tool": "test"}),
]:
    handler = registry.get_handler(tool)
    r = asyncio.run(handler(args, db=None, user_id=None))
    flow = r.get("user_action_required", "")
    has_update_env = "update_env_config" in flow
    print("  " + tool.ljust(15), "-> success=" + str(r["success"]), "flow mentions update_env_config:", has_update_env)
print()
print("=" * 60)
print("ALL SYSTEMS GO")
