#!/usr/bin/env python3
"""Fail if any enterprise-specific token appears in the platform's own source.

See docs/superpowers/specs/2026-10-08-enterprise-neutralization-design.md.

Rules:
  * Scans OWN source only; vendored third-party trees and binaries are skipped.
  * ASCII word tokens  -> word-boundary, case-insensitive.
  * Punctuated / CJK   -> literal substring.
  * scripts/enterprise_token_allowlist.txt may justify individual survivors
    as "<path-substring>:<token>  # reason".
Exit 1 on any un-allowlisted hit.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOTS = [
    "backend/app", "backend/tests", "backend/alembic", "backend/skills",
    "frontend/src", "scripts", "docs",
]
ROOT_FILES = ["Makefile", "README.md", "PROJECT.md", "ZHANLU_PROJECT_OVERVIEW.md"]

EXCLUDE_PARTS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build",
    ".next", ".mypy_cache", ".pytest_cache", "NL2SQL", "n8n-master",
    "deepseek-harness-master", "superpowers-main", "backups", "data", "uploads",
    "agent_qa", "outputs",
}
# Files that legitimately contain the tokens by definition (the gate's own artifacts).
# Without this the denylist and its test self-match and the gate could never pass.
EXCLUDE_RELPATHS = {
    "scripts/enterprise_token_denylist.txt",
    "scripts/enterprise_token_allowlist.txt",
    "backend/tests/neutralization/test_check_no_enterprise_tokens.py",
    # The neutralization spec/plan name the tokens by definition.
    "docs/superpowers/specs/2026-10-08-enterprise-neutralization-design.md",
    "docs/superpowers/plans/2026-10-08-enterprise-neutralization.md",
}
BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".woff", ".woff2",
    ".ttf", ".otf", ".eot", ".pdf", ".zip", ".gz", ".tar", ".whl", ".so", ".dylib",
    ".pyc", ".sqlite3", ".db", ".mp3", ".mp4", ".xlsx", ".pptx", ".docx", ".bin",
}
_WORD = re.compile(r"[A-Za-z0-9_]+$")


def is_excluded(path: Path) -> bool:
    return any(part in EXCLUDE_PARTS for part in path.parts)


def is_binary_name(path: Path) -> bool:
    if path.suffix.lower() in BINARY_EXT:
        return True
    return path.name.endswith(".min.js") or path.name.endswith(".min.css")


def load_tokens(denylist: Path) -> list[str]:
    tokens = []
    for line in denylist.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            tokens.append(line)
    return tokens


def token_matches(token: str, text: str) -> bool:
    if _WORD.match(token):
        return re.search(rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])",
                         text, re.IGNORECASE) is not None
    return token in text


def build_matchers(tokens: list[str]):
    """One precompiled regex per token class.

    Scanning every line against every token recompiled ~177M patterns on a
    full-tree run (60s). A combined matcher turns the common no-hit file into
    two searches, and only hit files fall back to per-token line attribution.
    """
    ascii_tokens = [t for t in tokens if _WORD.match(t)]
    other_tokens = [t for t in tokens if not _WORD.match(t)]
    apat = (
        re.compile(
            "|".join(rf"(?<![A-Za-z0-9_]){re.escape(t)}(?![A-Za-z0-9_])"
                     for t in ascii_tokens),
            re.IGNORECASE,
        )
        if ascii_tokens else None
    )
    cpat = (
        re.compile("|".join(re.escape(t) for t in other_tokens))
        if other_tokens else None
    )
    return apat, cpat


def load_allowlist(path: Path) -> list[tuple[str, str]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line and ":" in line:
            p, t = line.rsplit(":", 1)
            out.append((p.strip(), t.strip()))
    return out


def _iter_files(root: Path):
    for rel in ROOTS:
        base = root / rel
        if base.is_dir():
            for p in base.rglob("*"):
                if not p.is_file():
                    continue
                relp = str(p.relative_to(root))
                if relp in EXCLUDE_RELPATHS:
                    continue
                if is_excluded(p.relative_to(root)) or is_binary_name(p):
                    continue
                yield p
    for rel in ROOT_FILES:
        p = root / rel
        if p.is_file():
            yield p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    tokens = load_tokens(root / "scripts" / "enterprise_token_denylist.txt")
    allow = load_allowlist(root / "scripts" / "enterprise_token_allowlist.txt")

    hits = []
    apat, cpat = build_matchers(tokens)
    for path in sorted(_iter_files(root)):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        # Fast path: most files contain none of the tokens.
        if not ((apat is not None and apat.search(text))
                or (cpat is not None and cpat.search(text))):
            continue
        rel = str(path.relative_to(root))
        for i, line in enumerate(text.splitlines(), 1):
            for token in tokens:
                if token_matches(token, line):
                    if any(rp in rel and t == token for rp, t in allow):
                        continue
                    hits.append(f"{rel}:{i}: {token}: {line.strip()[:120]}")

    if hits:
        if not args.quiet:
            print(f"{len(hits)} enterprise token hit(s):\n")
            print("\n".join(hits))
        else:
            for h in hits:
                print(h)
        return 1
    if not args.quiet:
        print(f"OK — no enterprise tokens in own source ({len(tokens)} tokens checked).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
