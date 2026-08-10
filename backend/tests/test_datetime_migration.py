"""Regression tests for M6: datetime.utcnow() migration."""
import os, subprocess


def test_no_utcnow_in_code():
    """M6: Zero datetime.utcnow calls remain in production code."""
    backend = os.path.dirname(os.path.dirname(__file__))
    result = subprocess.run(
        ["grep", "-rn", "datetime\\.utcnow", "--include=*.py", "app/"],
        cwd=backend, capture_output=True, text=True,
    )
    code_lines = []
    for line in result.stdout.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        # Extract content after filepath:lineno:
        parts = line.split(":", 2)
        content = parts[2] if len(parts) >= 3 else line
        # Allow adaptation notes (datetime.utcnow -> timezone-aware)
        if ("\u2192" in content or "->" in content) and "utcnow" in content:
            continue
        # Allow comment-only lines
        if "#" in content:
            before_comment = content.split("#")[0]
            if "utcnow" not in before_comment:
                continue
        code_lines.append(line)
    assert len(code_lines) == 0, \
        f"utcnow in code:\n" + "\n".join(code_lines[:10])


def test_base_defaults_use_now_utc():
    """M6: base.py defaults use datetime.now(timezone.utc)."""
    with open(os.path.join(os.path.dirname(__file__), "../app/models/base.py")) as f:
        src = f.read()
    assert "datetime.utcnow" not in src
    assert "timezone" in src
    assert "datetime.now(timezone.utc)" in src
