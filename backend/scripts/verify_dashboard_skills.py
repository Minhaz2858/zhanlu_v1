#!/usr/bin/env python3
"""Verify the 7 dashboard-focused skills installed/refreshed in 2026-07-22 batch.

Covers:
  1. 5 new skills installed:
       task-management, datadog-api, grafana-api, macro-rates-monitor, vercel-sandbox
  2. 2 existing skills refreshed to latest upstream:
       build-dashboard (data/knowledge-work-plugins), ui-ux-pro-max (v2.11.0)
  3. build-dashboard has the marked "Zhanlu runtime integration" addendum that
     binds the skill body to the DASHBOARD marker contract + html artifact path.
  4. ui-ux-pro-max version 2.11.0 (latest upstream) is reflected in
     manifest.yaml (so the skill registry exposes the new counts).
  5. vercel-sandbox is registered under its own name (NOT 'sandbox') so it
     does not collide with Zhanlu's existing native `sandbox` skill.

Exits 0 on full pass, 1 on any failure.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"

# (skill_dir, expected_frontmatter_name, required_dependencies, additional_checks)
EXPECTED = [
    {
        "dir": "task-management",
        "frontmatter_name": "task-management",
        "requires": ["TASKS.md", "dashboard"],
        "extra": [],
    },
    {
        "dir": "datadog-api",
        "frontmatter_name": "datadog-api",
        "requires": ["DD_API_KEY", "DD_APP_KEY", "logs"],
        "extra": [
            ("file_exists", "references/api.md"),
            ("file_exists", "scripts/dd_logs.sh"),
        ],
    },
    {
        "dir": "grafana-api",
        "frontmatter_name": "grafana-api",
        "requires": ["GRAFANA_URL", "GRAFANA_TOKEN", "dashboard"],
        "extra": [
            ("file_exists", "references/api.md"),
        ],
    },
    {
        "dir": "macro-rates-monitor",
        "frontmatter_name": "macro-rates-monitor",
        "requires": ["macro", "yield curve", "MCP"],
        "extra": [
            # The LSEG/Refinitiv dependency is documented in manifest.yaml
            # (the SKILL.md body just names the MCP tools by function).
            ("manifest_contains", "LSEG"),
        ],
    },
    {
        "dir": "vercel-sandbox",
        "frontmatter_name": "vercel-sandbox",  # NOT 'sandbox' (collision)
        "requires": ["@vercel/sandbox", "MicroVM"],
        "extra": [],
    },
    {
        "dir": "build-dashboard",
        "frontmatter_name": "build-dashboard",
        "requires": ["Chart.js", "HTML"],
        "extra": [
            ("frontmatter_contains", "## Zhanlu runtime integration"),
            ("frontmatter_contains", "◤DASHBOARD◤"),
            ("frontmatter_contains", "ask_data_agent"),
        ],
    },
    {
        "dir": "ui-ux-pro-max",
        "frontmatter_name": "ui-ux-pro-max",
        "requires": ["styles", "palettes", "fonts"],
        "extra": [
            # v2.11.0 is the latest upstream (released 2026-07-13); the local
            # SKILL.md + manifest.yaml must reflect the v2.11.0 counts.
            ("manifest_version", "2.11.0"),
            ("file_exists", "references/quick-reference.md"),
            ("file_exists", "references/pro-rules.md"),
            ("file_exists", "data/colors.csv"),
            ("file_exists", "data/products.csv"),
        ],
    },
]

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
NAME_RE = re.compile(r"^name:\s*(\S+)", re.MULTILINE)
DESC_RE = re.compile(r"^description:\s*(.+)$", re.MULTILINE)


def check_skill(spec: dict) -> list[str]:
    failures: list[str] = []
    skill_dir = SKILLS_DIR / spec["dir"]
    skill_md = skill_dir / "SKILL.md"
    if not skill_dir.is_dir():
        return [f"missing directory: {skill_dir}"]
    if not skill_md.is_file():
        return [f"missing SKILL.md: {skill_md}"]
    text = skill_md.read_text(encoding="utf-8")
    fm = FRONTMATTER_RE.match(text)
    if not fm:
        failures.append("no YAML frontmatter block")
        return failures
    fm_text = fm.group(1)
    name_m = NAME_RE.search(fm_text)
    desc_m = DESC_RE.search(fm_text)
    if not name_m:
        failures.append("frontmatter missing `name`")
    elif name_m.group(1) != spec["frontmatter_name"]:
        failures.append(
            f"frontmatter name mismatch: expected '{spec['frontmatter_name']}', "
            f"got '{name_m.group(1)}' — would collide with another skill"
        )
    if not desc_m:
        failures.append("frontmatter missing `description`")
    for req in spec["requires"]:
        if req.lower() not in text.lower():
            failures.append(f"no mention of required dependency/feature '{req}'")
    for kind, val in spec.get("extra", []):
        if kind == "file_exists":
            if not (skill_dir / val).is_file():
                failures.append(f"missing companion file: {val}")
        elif kind == "frontmatter_contains":
            if val not in text:
                failures.append(
                    f"SKILL.md missing required content block: '{val}'"
                )
        elif kind == "manifest_version":
            manifest = skill_dir / "manifest.yaml"
            if not manifest.is_file():
                failures.append("missing manifest.yaml")
            elif val not in manifest.read_text(encoding="utf-8"):
                failures.append(
                    f"manifest.yaml does not declare version '{val}'"
                )
        elif kind == "manifest_contains":
            manifest = skill_dir / "manifest.yaml"
            if not manifest.is_file():
                failures.append("missing manifest.yaml (needed for " + val + " check)")
            elif val.lower() not in manifest.read_text(encoding="utf-8").lower():
                failures.append(
                    f"manifest.yaml does not mention '{val}'"
                )
    return failures


def main() -> int:
    print(f"Checking {len(EXPECTED)} dashboard skills under {SKILLS_DIR}\n")
    failed = 0
    for spec in EXPECTED:
        failures = check_skill(spec)
        name = spec["dir"]
        if failures:
            failed += 1
            print(f"FAIL  {name}")
            for f in failures:
                print(f"      - {f}")
        else:
            size = (SKILLS_DIR / name / "SKILL.md").stat().st_size
            print(f"ok    {name}  ({size} bytes SKILL.md)")
    print()
    if failed:
        print(f"{failed}/{len(EXPECTED)} skills FAILED verification.")
        return 1
    print(f"All {len(EXPECTED)} skills verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
