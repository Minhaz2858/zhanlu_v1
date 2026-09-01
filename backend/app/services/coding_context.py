"""Coding context -- detect project facts for smarter verification.

Detects project-level facts (test commands, build systems, languages) from
the workspace structure. These facts power:

1. **Verification-on-stop**: instead of generic "run a test", the nudge can
   say "run `pytest`" or "run `npm test`".
2. **Project memories**: store detected facts as project memories for
   future conversations.

Detection is file-system based (no LLM call) -- it looks for config files
like ``package.json``, ``pyproject.toml``, ``Makefile``, etc.

Inspired by Hermes' coding context patterns.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ProjectFacts:
    """Detected facts about a project workspace."""
    languages: list[str] = field(default_factory=list)
    test_commands: list[str] = field(default_factory=list)
    build_commands: list[str] = field(default_factory=list)
    lint_commands: list[str] = field(default_factory=list)
    package_manager: str = ""
    framework: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "languages": self.languages,
            "test_commands": self.test_commands,
            "build_commands": self.build_commands,
            "lint_commands": self.lint_commands,
            "package_manager": self.package_manager,
            "framework": self.framework,
        }

    @property
    def primary_test_command(self) -> str | None:
        """The most likely test command for this project."""
        if self.test_commands:
            return self.test_commands[0]
        return None


# -- Detection rules --

# Each rule: (filename, language, test_cmd, build_cmd, lint_cmd, framework, pkg_mgr)
_DETECTION_RULES: list[tuple[str, str, str | None, str | None, str | None, str, str]] = [
    # Python
    ("pyproject.toml", "python", "pytest", None, "ruff check .", "", ""),
    ("pytest.ini", "python", "pytest", None, None, "", ""),
    ("setup.py", "python", "python -m pytest", None, None, "", ""),
    ("tox.ini", "python", "tox", None, None, "", ""),
    ("requirements.txt", "python", None, None, None, "", ""),
    # JavaScript/TypeScript
    ("package.json", "javascript", None, None, None, "", "npm"),
    ("tsconfig.json", "typescript", None, None, None, "", ""),
    # Rust
    ("Cargo.toml", "rust", "cargo test", "cargo build", "cargo clippy", "", ""),
    # Go
    ("go.mod", "go", "go test ./...", "go build", "golangci-lint run", "", ""),
    # Make
    ("Makefile", "make", "make test", "make", "make lint", "", ""),
    # Java
    ("pom.xml", "java", "mvn test", "mvn compile", None, "maven", ""),
    ("build.gradle", "java", "gradle test", "gradle build", None, "gradle", ""),
    # Ruby
    ("Gemfile", "ruby", "bundle exec rspec", None, "rubocop", "", ""),
    # C/C++
    ("CMakeLists.txt", "c", None, "cmake --build .", None, "", ""),
]


def _read_package_json(workspace: Path) -> dict[str, Any]:
    """Read and parse package.json if it exists."""
    pkg_path = workspace / "package.json"
    if not pkg_path.exists():
        return {}
    try:
        with open(pkg_path, "r", encoding="utf-8") as f:
            return json.loads(f.read())
    except (json.JSONDecodeError, OSError):
        return {}


def _detect_npm_scripts(pkg: dict[str, Any]) -> ProjectFacts:
    """Extract test/build/lint commands from package.json scripts."""
    facts = ProjectFacts(languages=["javascript"], package_manager="npm")
    scripts = pkg.get("scripts", {})
    if not isinstance(scripts, dict):
        return facts

    if "test" in scripts:
        facts.test_commands.append(f"npm test")
    if "test:unit" in scripts:
        facts.test_commands.append(f"npm run test:unit")
    if "build" in scripts:
        facts.build_commands.append("npm run build")
    if "lint" in scripts:
        facts.lint_commands.append("npm run lint")

    # Detect TypeScript
    dev_deps = pkg.get("devDependencies", {})
    if isinstance(dev_deps, dict):
        if "typescript" in dev_deps:
            facts.languages = ["typescript"]
        if "jest" in dev_deps or "vitest" in dev_deps:
            if not facts.test_commands:
                facts.test_commands.append("npx jest" if "jest" in dev_deps else "npx vitest")

    # Detect framework
    deps = {**(pkg.get("dependencies", {}) or {}), **dev_deps}
    if "react" in deps:
        facts.framework = "react"
    elif "vue" in deps:
        facts.framework = "vue"
    elif "next" in deps:
        facts.framework = "next"

    # Detect package manager from lockfile
    if (Path("pnpm-lock.yaml")).exists():
        facts.package_manager = "pnpm"
    elif (Path("yarn.lock")).exists():
        facts.package_manager = "yarn"

    return facts


def detect_project_facts(workspace_path: str | Path) -> ProjectFacts:
    """Detect project facts from the workspace file structure.

    Args:
        workspace_path: Path to the project workspace root.

    Returns:
        ProjectFacts with detected languages, test/build/lint commands.
    """
    workspace = Path(workspace_path)
    facts = ProjectFacts()

    if not workspace.exists() or not workspace.is_dir():
        return facts

    # Check each detection rule
    for filename, lang, test_cmd, build_cmd, lint_cmd, framework, pkg_mgr in _DETECTION_RULES:
        if not (workspace / filename).exists():
            continue

        if lang not in facts.languages:
            facts.languages.append(lang)

        if test_cmd and test_cmd not in facts.test_commands:
            facts.test_commands.append(test_cmd)
        if build_cmd and build_cmd not in facts.build_commands:
            facts.build_commands.append(build_cmd)
        if lint_cmd and lint_cmd not in facts.lint_commands:
            facts.lint_commands.append(lint_cmd)

        if framework and not facts.framework:
            facts.framework = framework
        if pkg_mgr and not facts.package_manager:
            facts.package_manager = pkg_mgr

    # Special handling for package.json (extract scripts)
    if (workspace / "package.json").exists():
        pkg = _read_package_json(workspace)
        if pkg:
            npm_facts = _detect_npm_scripts(pkg)
            # Merge npm-detected facts
            for cmd in npm_facts.test_commands:
                if cmd not in facts.test_commands:
                    facts.test_commands.append(cmd)
            for cmd in npm_facts.build_commands:
                if cmd not in facts.build_commands:
                    facts.build_commands.append(cmd)
            for cmd in npm_facts.lint_commands:
                if cmd not in facts.lint_commands:
                    facts.lint_commands.append(cmd)
            for lang in npm_facts.languages:
                if lang not in facts.languages:
                    facts.languages.append(lang)
            if npm_facts.framework and not facts.framework:
                facts.framework = npm_facts.framework
            if npm_facts.package_manager and not facts.package_manager:
                facts.package_manager = npm_facts.package_manager

    return facts


def get_test_command_for_files(
    file_paths: list[str],
    facts: ProjectFacts,
) -> str | None:
    """Get the most appropriate test command for the given files.

    Args:
        file_paths: List of file paths that were edited.
        facts: Detected project facts.

    Returns:
        A test command string, or None if no test command is available.
    """
    if not facts.test_commands:
        return None

    # Check file extensions to pick the most relevant test command
    extensions = {Path(p).suffix.lower() for p in file_paths if p}

    # Python files -> pytest
    if any(ext in extensions for ext in {".py"}) and "pytest" in " ".join(facts.test_commands):
        return "pytest"

    # JS/TS files -> npm test
    if any(ext in extensions for ext in {".js", ".jsx", ".ts", ".tsx"}):
        for cmd in facts.test_commands:
            if "npm" in cmd or "jest" in cmd or "vitest" in cmd:
                return cmd

    # Rust files -> cargo test
    if ".rs" in extensions and "cargo test" in facts.test_commands:
        return "cargo test"

    # Go files -> go test
    if ".go" in extensions and "go test" in facts.test_commands:
        return "go test"

    # Fall back to the primary test command
    return facts.primary_test_command


__all__ = [
    "ProjectFacts",
    "detect_project_facts",
    "get_test_command_for_files",
]
