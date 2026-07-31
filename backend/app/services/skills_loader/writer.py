"""Atomic skill package writer — writes manifest.yaml + SKILL.md + schemas.

All writes use temp-file + rename to avoid partial/corrupt writes.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


def write_manifest(
    package_dir: Path,
    manifest: dict[str, Any],
    *,
    overwrite: bool = False,
) -> Path:
    """Atomically write ``manifest.yaml`` into a skill package directory.

    Args:
        package_dir: Target directory (created if missing).
        manifest: Dict conforming to the skill manifest JSON Schema.
        overwrite: If ``False``, raises ``FileExistsError`` when the
            manifest already exists.  If ``True``, overwrites.

    Returns:
        Path to the written ``manifest.yaml``.

    Raises:
        FileExistsError: If ``overwrite=False`` and the file already exists.
        OSError: If the directory cannot be created.
    """
    package_dir.mkdir(parents=True, exist_ok=True)
    target = package_dir / "manifest.yaml"

    if not overwrite and target.exists():
        raise FileExistsError(f"manifest.yaml already exists at {target}")

    yaml_text = yaml.safe_dump(manifest, default_flow_style=False, sort_keys=False)

    _atomic_write(target, yaml_text)
    return target


def write_skill_md(
    package_dir: Path,
    body: str,
    frontmatter: dict[str, Any] | None = None,
    *,
    overwrite: bool = False,
) -> Path:
    """Atomically write ``SKILL.md`` with optional YAML frontmatter.

    Args:
        package_dir: Target directory.
        body: Markdown methodology body.
        frontmatter: Optional YAML frontmatter dict.
        overwrite: Overwrite existing file if ``True``.

    Returns:
        Path to written ``SKILL.md``.
    """
    package_dir.mkdir(parents=True, exist_ok=True)
    target = package_dir / "SKILL.md"

    if not overwrite and target.exists():
        raise FileExistsError(f"SKILL.md already exists at {target}")

    if frontmatter:
        fm_yaml = yaml.safe_dump(frontmatter, default_flow_style=False, sort_keys=False)
        content = f"---\n{fm_yaml}---\n\n{body}"
    else:
        content = body

    _atomic_write(target, content)
    return target


def write_json_schema(
    package_dir: Path,
    schema_type: str,
    schema: dict[str, Any],
    *,
    overwrite: bool = False,
) -> Path:
    """Atomically write ``schemas/{schema_type}.schema.json``.

    Args:
        package_dir: Target directory.
        schema_type: "input" or "output".
        schema: JSON Schema dict.
        overwrite: Overwrite existing file if ``True``.

    Returns:
        Path to the written schema file.
    """
    schemas_dir = package_dir / "schemas"
    schemas_dir.mkdir(parents=True, exist_ok=True)
    target = schemas_dir / f"{schema_type}.schema.json"

    if not overwrite and target.exists():
        raise FileExistsError(f"{schema_type}.schema.json already exists at {target}")

    json_text = json.dumps(schema, indent=2, ensure_ascii=False)
    _atomic_write(target, json_text)
    return target


def write_full_package(
    package_dir: Path,
    manifest: dict[str, Any],
    body: str = "",
    frontmatter: dict[str, Any] | None = None,
    input_schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
    *,
    overwrite: bool = False,
) -> dict[str, Path]:
    """Write a complete skill package (manifest + SKILL.md + schemas) atomically.

    Returns a dict mapping component names to written file paths.
    """
    results: dict[str, Path] = {}
    results["manifest"] = write_manifest(package_dir, manifest, overwrite=overwrite)
    results["skill_md"] = write_skill_md(package_dir, body, frontmatter=frontmatter, overwrite=overwrite)
    if input_schema is not None:
        results["input_schema"] = write_json_schema(package_dir, "input", input_schema, overwrite=overwrite)
    if output_schema is not None:
        results["output_schema"] = write_json_schema(package_dir, "output", output_schema, overwrite=overwrite)
    return results


# ── Internal helpers ──────────────────────────────────────────────────


def _atomic_write(target: Path, content: str) -> None:
    """Write content to a temp file, then os.rename for atomicity."""
    fd, tmp_path = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    try:
        os.write(fd, content.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp_path, target)
