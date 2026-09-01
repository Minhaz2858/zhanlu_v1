"""Task 1: pptx SKILL.md must have a clean, parseable YAML frontmatter with no
stray line-number prefixes.

This is an AST-equivalent check for a Markdown file: we read the raw bytes,
assert the frontmatter block is well-formed (opens with `---`, closes with
`---`, has a non-empty key:value pair), and assert there are NO lines in the
file that match the pattern of a pasted line-number prefix (e.g. `     6|`,
`     7|`, …). Those showed up in the original file as visible content
artifacts that broke the markdown rendering.
"""
import re
from pathlib import Path

import pytest

PPTX_SKILL = Path(__file__).resolve().parents[1] / "skills" / "pptx" / "SKILL.md"


def _read() -> str:
    return PPTX_SKILL.read_text(encoding="utf-8")


def test_pptx_skill_exists():
    assert PPTX_SKILL.is_file(), f"missing: {PPTX_SKILL}"


def test_pptx_skill_starts_with_yaml_frontmatter():
    text = _read()
    assert text.startswith("---\n"), "pptx SKILL.md must begin with `---` (YAML frontmatter)"


def test_pptx_skill_frontmatter_closes():
    text = _read()
    # The frontmatter must close on its own line with `---` followed by a newline.
    assert "\n---\n" in text, "pptx SKILL.md YAML frontmatter must close with `---` on its own line"


def test_pptx_skill_frontmatter_has_name():
    text = _read()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert m is not None, "could not isolate frontmatter block"
    fm = m.group(1)
    assert re.search(r"^name:\s*\S+", fm, re.MULTILINE), "frontmatter must declare `name`"


def test_pptx_skill_no_line_number_artifacts():
    """The original file had visible content lines starting with `     6|`, `     7|`, …
    These are pasted-from-editor line-number prefixes. No real content line in the
    rendered markdown should look like that.
    """
    text = _read()
    # Pattern: 1-3 digits, right-aligned in 5+ spaces, then a literal `|`
    # and either EOL or more content. Matches the `     6|` style.
    pattern = re.compile(r"^ {3,}\d{1,3}\|", re.MULTILINE)
    matches = pattern.findall(text)
    assert not matches, (
        "pptx SKILL.md still has pasted line-number artifacts. Found: "
        f"{matches[:5]}"
    )


def test_pptx_skill_first_h1_is_pptx_skill():
    text = _read()
    # After the frontmatter, the first heading should be `# PPTX Skill`.
    m = re.match(r"^---\n.*?\n---\n+(# .+)$", text, re.MULTILINE)
    assert m is not None, "no H1 found after frontmatter"
    assert "PPTX" in m.group(1), f"first H1 should mention PPTX, got: {m.group(1)!r}"
