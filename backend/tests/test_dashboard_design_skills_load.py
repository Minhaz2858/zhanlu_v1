import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_runtime.db")
import yaml
from pathlib import Path

SKILLS = [
    "data-viz-design",
    "dashboard-layout-architecture",
    "dashboard-ui-polish",
    "dashboard-interaction-state",
]

TEMPLATE_ROOT = (
    Path(__file__).resolve().parents[2]
    / "docs" / "starter_templates" / "skill_library" / "system" / "visualization"
)
RUNTIME_ROOT = Path(__file__).resolve().parents[1] / "skills"


def test_template_manifests_valid():
    for key in SKILLS:
        m = yaml.safe_load((TEMPLATE_ROOT / key / "manifest.yaml").read_text())
        assert m["skill_key"] == key, key
        assert m["status"] == "active", key
        assert m["runtime"]["type"] == "prompt", key


def test_runtime_manifests_valid_against_schema():
    from app.services.skills_loader import validate_manifest

    for key in SKILLS + ["dashboard-generation"]:
        m = yaml.safe_load((RUNTIME_ROOT / key / "manifest.yaml").read_text())
        ok, errors = validate_manifest(m)
        assert ok, f"{key}: {errors}"
        assert m["name"] == key, key
        assert m["runtime"] == "prompt", key
        assert m["requires_sandbox"] is False, key


def test_runtime_bodies_present():
    for key in SKILLS + ["dashboard-generation"]:
        body = (RUNTIME_ROOT / key / "SKILL.md").read_text()
        assert len(body) > 500, f"{key}: SKILL.md body too short"


def test_dashboard_generation_composes_companions():
    for root in (TEMPLATE_ROOT, RUNTIME_ROOT):
        p = (root / "dashboard-generation" / "SKILL.md").read_text()
        for key in SKILLS:
            assert key in p, f"{root}: dashboard-generation missing {key}"
