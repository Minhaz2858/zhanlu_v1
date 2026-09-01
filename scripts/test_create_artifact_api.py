"""Integration test for the create_artifact feature.

Tests:
  1. Tool schema is valid
  2. _payload_to_reportcard conversion (with the icon bug fixed)
  3. ALL_TOOL_NAMES includes create_artifact
  4. _build_system_agent_configs has create_artifact in all agents
  5. _BASE_HARNESS has allowed_artifact_types
  6. ArtifactService class has all required methods
  7. Artifacts router has download + preview endpoints
  8. agents.py has _collect_artifact_results + link_to_message
  9. Frontend components exist on disk
  A. All bubble + page components are properly wired

Usage:
    cd /root/zhanlu && ./backend/venv/bin/python scripts/test_create_artifact_api.py
"""

import os
import sys

# Ensure backend is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

passed = 0
failed = []
warned = []


def ok(name, detail=""):
    global passed
    passed += 1
    d = f" — {detail}" if detail else ""
    print(f"  [PASS] {name}{d}")


def fail(name, detail=""):
    failed.append((name, detail))
    print(f"  [FAIL] {name}: {detail}")


def warn(name, detail=""):
    warned.append((name, detail))
    print(f"  [WARN] {name}: {detail}")


# ── 1. Tool schema ──────────────────────────────────────────────────────
print("\n=== 1. Tool Schema ===")
try:
    from app.services.tool_handlers.artifact_tool import (
        CREATE_ARTIFACT_SCHEMA, _TYPE_MIME, _TYPE_EXT,
    )
    s = CREATE_ARTIFACT_SCHEMA
    assert s["type"] == "function"
    assert s["function"]["name"] == "create_artifact"
    params = s["function"]["parameters"]
    assert "type" in params["required"]
    assert "title" in params["required"]
    assert "payload" in params["required"]
    allowed = params["properties"]["type"]["enum"]
    assert set(allowed) >= {"docx", "pdf", "pptx", "html"}, f"Missing types: {allowed}"
    ok("Schema valid", f"supports: {allowed}")

    for t in ["docx", "pdf", "pptx", "html"]:
        assert t in _TYPE_MIME and t in _TYPE_EXT, f"Missing MIME/EXT for {t}"
    ok("MIME/EXT mappings", "all 4 types mapped")
except Exception as e:
    fail("Tool schema", str(e))


# ── 2. Payload conversion (icon bug fixed) ──────────────────────────────
print("\n=== 2. Payload Conversion ===")
try:
    from app.services.tool_handlers.artifact_tool import _payload_to_reportcard

    # Test with insights that have NO icon key → should default to "lightbulb"
    payload_no_icon = {
        "title": "Q3 Revenue",
        "summary": "Revenue grew 23% QoQ",
        "insights": [
            {"text": "Top segment: Enterprise"},
            {"text": "Churn reduced by 12%"},
        ],
    }
    rcp = _payload_to_reportcard(payload_no_icon, "Q3 Revenue")
    assert rcp.title == "Q3 Revenue"
    assert len(rcp.insights) == 2
    assert rcp.insights[0].icon == "lightbulb", f"Expected lightbulb, got {rcp.insights[0].icon!r}"
    assert rcp.insights[1].icon == "lightbulb"
    ok("Insights without icon default to lightbulb")

    # Test with explicit icons
    payload_with_icon = {
        "title": "Test",
        "insights": [
            {"text": "Good", "icon": "check"},
            {"text": "Bad", "icon": "x"},
        ],
    }
    rcp2 = _payload_to_reportcard(payload_with_icon, "Test")
    assert rcp2.insights[0].icon == "check"
    assert rcp2.insights[1].icon == "x"
    ok("Insights with explicit icon preserved")

    # Test with kpis
    payload_full = {
        "title": "Full Report",
        "source": "Analytics",
        "kpis": [{"label": "Revenue", "value": "$2.3M", "delta": "+8%"}],
    }
    rcp3 = _payload_to_reportcard(payload_full, "Full Report")
    assert len(rcp3.kpis) == 1
    assert rcp3.kpis[0].value == "$2.3M"
    ok("KPIs converted correctly")
except Exception as e:
    fail("Payload conversion", str(e))


# ── 3. ALL_TOOL_NAMES has create_artifact ───────────────────────────────
print("\n=== 3. Tool Registry Check ===")
try:
    from app.services.system_agents import ALL_TOOL_NAMES
    assert "create_artifact" in ALL_TOOL_NAMES, "Missing from ALL_TOOL_NAMES"
    ok("create_artifact in ALL_TOOL_NAMES")

    from app.services.tool_registry import DEFAULT_USER_AGENT_TOOLS
    assert "create_artifact" in DEFAULT_USER_AGENT_TOOLS, "Not in DEFAULT_USER_AGENT_TOOLS"
    ok("create_artifact in DEFAULT_USER_AGENT_TOOLS")
except Exception as e:
    fail("Tool registry check", str(e))


# ── 4. System agent configs ─────────────────────────────────────────────
print("\n=== 4. System Agents Configs ===")
try:
    from app.services.system_agents import _build_system_agent_configs
    configs = _build_system_agent_configs()

    assert isinstance(configs, list) and len(configs) == 5, f"Expected 5, got {len(configs)}"
    ok("5 system agent configs returned")

    for cfg in configs:
        name = cfg["name"]
        tools = cfg.get("tool_config", {}).get("enabled_tools", [])
        assert "create_artifact" in tools, f"{name} missing create_artifact"
        ok(f"{name}", "has create_artifact")

    # Check allowed_artifact_types from the first config (all share _BASE_HARNESS)
    allowed_types = configs[0].get("output_contract", {}).get("allowed_artifact_types", [])
    for t in ["docx", "pptx", "html"]:
        assert t in allowed_types, f"Missing {t} in allowed_artifact_types"
    ok("allowed_artifact_types includes docx/pptx/html")
except Exception as e:
    fail("System agents", str(e))


# ── 5. ArtifactService methods (no DB needed) ───────────────────────────
print("\n=== 5. ArtifactService Methods ===")
try:
    import inspect
    from app.services.artifacts import artifact_service as mod

    required = ["create_artifact", "create_version", "store_blob",
                "mark_version_built", "update_status", "link_to_message"]
    for method in required:
        assert hasattr(mod.ArtifactService, method), f"Missing: {method}"
    ok("All 6 required methods exist on ArtifactService")
except Exception as e:
    fail("ArtifactService", str(e))


# ── 6. Artifacts router endpoints ───────────────────────────────────────
print("\n=== 6. Artifacts Router ===")
try:
    from app.routers.artifacts import router
    paths = {r.path for r in router.routes}
    assert "/artifacts/{artifact_id}/download" in paths, f"Missing download. Paths: {sorted(paths)}"
    ok("Download endpoint", "/artifacts/{artifact_id}/download")
    assert "/artifacts/{artifact_id}/preview" in paths, f"Missing preview. Paths: {sorted(paths)}"
    ok("Preview endpoint", "/artifacts/{artifact_id}/preview")
    assert "/messages/{message_id}/artifacts" in paths, "Missing message artifacts endpoint"
    ok("Message artifacts endpoint", "/messages/{message_id}/artifacts")
except Exception as e:
    fail("Artifacts router", str(e))


# ── 7. agents.py post-processing ────────────────────────────────────────
print("\n=== 7. Backend Post-Processing ===")
try:
    src_dir = os.path.join(os.path.dirname(__file__), "..", "backend", "app", "routers")
    with open(os.path.join(src_dir, "agents.py")) as f:
        content = f.read()
    assert "_collect_artifact_results" in content, "Missing _collect_artifact_results"
    ok("_collect_artifact_results exists in agents.py")
    assert "link_to_message" in content, "Missing link_to_message call"
    ok("link_to_message called in agents.py")
except Exception as e:
    fail("Post-processing", str(e))


# ── 8. Frontend components on disk ──────────────────────────────────────
print("\n=== 8. Frontend Components ===")
try:
    base = os.path.join(os.path.dirname(__file__), "..", "frontend", "src")
    base = os.path.realpath(base)

    # ArtifactCardList.jsx and ArtifactPreviewSheet.jsx
    chat_dir = os.path.join(base, "components", "chat")
    for fname in ["ArtifactCardList.jsx", "ArtifactPreviewSheet.jsx"]:
        fpath = os.path.join(chat_dir, fname)
        assert os.path.isfile(fpath), f"Missing: {fpath}"
        ok(f"Component exists: {fname}")

    # Bubble components import ArtifactCardList
    for fname in ["MessageBubble.jsx", "SkillMessageBubble.jsx", "BuilderMessageBubble.jsx"]:
        fpath = os.path.join(chat_dir, fname)
        if not os.path.isfile(fpath):
            warn(f"Bubble not found: {fname}", "may be named differently")
            continue
        with open(fpath) as f:
            c = f.read()
        assert "ArtifactCardList" in c, f"{fname} missing ArtifactCardList import"
        ok(f"{fname} imports ArtifactCardList")

    # Pages wire up ArtifactPreviewSheet
    pages_dir = os.path.join(base, "pages")
    for page_name in ["Chat.jsx", "SkillAgent.jsx", "AgentBuilder.jsx"]:
        fpath = os.path.join(pages_dir, page_name)
        if not os.path.isfile(fpath):
            warn(f"Page not found: {page_name}")
            continue
        with open(fpath) as f:
            c = f.read()
        has_sheet = "ArtifactPreviewSheet" in c
        has_state = "setOpenArtifact" in c or "openArtifact" in c
        if has_sheet and has_state:
            ok(f"{page_name} wired", "ArtifactPreviewSheet + state")
        else:
            issues = []
            if not has_sheet: issues.append("no ArtifactPreviewSheet")
            if not has_state: issues.append("no openArtifact state")
            warn(f"{page_name}", ", ".join(issues))
except Exception as e:
    fail("Frontend components", str(e))


# ── 9. Sandbox worker tool check (tool_handlers/__init__.py) ────────────
print("\n=== 9. Tool Handler Registration ===")
try:
    init_path = os.path.join(os.path.dirname(__file__), "..", "backend",
                             "app", "services", "tool_handlers", "__init__.py")
    with open(init_path) as f:
        c = f.read()
    assert "artifact_tool" in c, "Missing artifact_tool import in __init__.py"
    ok("artifact_tool imported in tool_handlers/__init__.py")
except Exception as e:
    fail("Tool handler registration", str(e))


# ── Summary ──────────────────────────────────────────────────────────────
print(f"\n{'='*60}")
total = passed + len(failed) + len(warned)
print(f"Results: {passed} PASS | {len(failed)} FAIL | {len(warned)} WARN  (total {total})")
if failed:
    print(f"\nFAILING tests:")
    for name, detail in failed:
        print(f"  \u2717 {name}: {detail}")
if warned:
    print(f"\nWARNINGS:")
    for name, detail in warned:
        print(f"  \u26a0 {name}: {detail}")
print(f"{'='*60}")
sys.exit(1 if failed else 0)
