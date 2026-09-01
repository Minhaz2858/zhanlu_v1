import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")
import yaml
from pathlib import Path

SKILL_DIR = (
    Path(__file__).resolve().parents[2]
    / "docs" / "starter_templates" / "skill_library" / "system"
    / "visualization" / "dashboard-generation"
)


def test_manifest_valid():
    m = yaml.safe_load((SKILL_DIR / "manifest.yaml").read_text())
    assert m["skill_key"] == "dashboard-generation"
    assert m["status"] == "active"
    assert "create_dashboard" in m.get("tools", [])


def test_prompt_describes_widget_schema():
    p = (SKILL_DIR / "SKILL.md").read_text()
    for token in ["kpi", "line", "bar", "pie", "table", "create_dashboard",
                  "SELECT", "datasource", "read-only"]:
        assert token in p, f"SKILL.md missing token: {token}"
