#!/usr/bin/env python3
"""
Verify that the 8 newly-installed agent skills are present and parseable.

Checks:
  1. Each skill directory exists under backend/skills/
  2. Each SKILL.md has valid YAML frontmatter with `name` and `description`
  3. External dependency mentions are present (`lark-cli` for lark-* skills,
     Notion MCP for notion-* skills)

Exit code 0 if all 8 pass, 1 otherwise.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"

EXPECTED = {
    "lark-shared":               {"requires": ["lark-cli"]},
    "lark-doc":                  {"requires": ["lark-cli"]},
    "lark-sheets":               {"requires": ["lark-cli"]},
    "lark-slides":               {"requires": ["lark-cli"]},
    "lark-workflow-meeting-summary": {"requires": ["lark-cli"]},
    "notion-knowledge-capture":  {"requires": ["notion"]},
    "notion-meeting-intelligence": {"requires": ["notion"]},
    "brand-guidelines":          {"requires": []},
}

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
NAME_RE = re.compile(r"^name:\s*(\S+)", re.MULTILINE)
DESC_RE = re.compile(r"^description:\s*(.+)$", re.MULTILINE)


def check_skill(name: str, expected_requires: list[str]) -> list[str]:
    """Return a list of failure messages (empty list = pass)."""
    failures: list[str] = []
    skill_dir = SKILLS_DIR / name
    skill_md = skill_dir / "SKILL.md"

    if not skill_dir.is_dir():
        return [f"missing directory: {skill_dir}"]
    if not skill_md.is_file():
        return [f"missing SKILL.md: {skill_md}"]

    text = skill_md.read_text(encoding="utf-8")

    fm = FRONTMATTER_RE.match(text)
    if not fm:
        failures.append("no YAML frontmatter block")
    else:
        fm_text = fm.group(1)
        name_m = NAME_RE.search(fm_text)
        desc_m = DESC_RE.search(fm_text)
        if not name_m:
            failures.append("frontmatter missing `name`")
        elif name_m.group(1) != name:
            failures.append(
                f"frontmatter name mismatch: expected '{name}', got '{name_m.group(1)}'"
            )
        if not desc_m:
            failures.append("frontmatter missing `description`")

    for req in expected_requires:
        if req.lower() not in text.lower():
            failures.append(f"no mention of required dependency '{req}'")

    return failures


def main() -> int:
    print(f"Checking {len(EXPECTED)} skills under {SKILLS_DIR}\n")
    failed = 0
    for name, spec in EXPECTED.items():
        failures = check_skill(name, spec["requires"])
        if failures:
            failed += 1
            print(f"FAIL  {name}")
            for f in failures:
                print(f"      - {f}")
        else:
            size = (SKILLS_DIR / name / "SKILL.md").stat().st_size
            print(f"ok    {name}  ({size} bytes)")
    print()
    if failed:
        print(f"{failed}/{len(EXPECTED)} skills FAILED verification.")
        return 1
    print(f"All {len(EXPECTED)} skills verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
