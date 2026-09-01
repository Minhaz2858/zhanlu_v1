"""Atomic skill package writer — writes manifest.yaml + SKILL.md + schemas.

All writes use temp-file + rename to avoid partial/corrupt writes.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Reference files must live under references/ and be markdown documents.
_REFERENCE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.md$")

# Asset paths may contain subdirectories (e.g. templates/report.docx) but each
# path segment must be a safe token — no traversal, no absolute paths.
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]*$")


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


def _safe_reference_name(filename: str) -> str:
    """Validate a reference filename; returns it unchanged or raises ValueError."""
    if not _REFERENCE_NAME_RE.match(filename):
        raise ValueError(
            f"Invalid reference filename {filename!r}: must match "
            f"{_REFERENCE_NAME_RE.pattern} (markdown file, no path separators)."
        )
    return filename


def _safe_asset_path(rel_path: str) -> Path:
    """Validate an asset relative path and return it as a Path.

    Every path segment must be a safe token.  Rejects absolute paths, ``..``
    traversal, empty segments, and hidden files.
    """
    if not rel_path or rel_path.startswith(("/", "\\")):
        raise ValueError(f"Invalid asset path {rel_path!r}: must be relative.")
    parts = Path(rel_path).parts
    if not parts:
        raise ValueError(f"Invalid asset path {rel_path!r}: empty.")
    for seg in parts:
        if seg in (".", ".."):
            raise ValueError(f"Invalid asset path {rel_path!r}: no traversal allowed.")
        if not _SAFE_SEGMENT_RE.match(seg):
            raise ValueError(
                f"Invalid asset path segment {seg!r}: must match {_SAFE_SEGMENT_RE.pattern}."
            )
    return Path(*parts)


def write_reference(
    package_dir: Path,
    filename: str,
    content: str,
    *,
    overwrite: bool = False,
) -> Path:
    """Atomically write ``references/{filename}`` (markdown) into a package.

    Args:
        package_dir: Target package directory.
        filename: Reference filename (e.g. ``"output-formats.md"``).
        content: Markdown text content.
        overwrite: Overwrite existing file if ``True``.

    Returns:
        Path to the written reference file.

    Raises:
        ValueError: If the filename is unsafe.
        FileExistsError: If ``overwrite=False`` and the file exists.
    """
    safe_name = _safe_reference_name(filename)
    refs_dir = package_dir / "references"
    refs_dir.mkdir(parents=True, exist_ok=True)
    target = refs_dir / safe_name

    if not overwrite and target.exists():
        raise FileExistsError(f"references/{safe_name} already exists at {target}")

    _atomic_write(target, content)
    return target


def write_asset(
    package_dir: Path,
    rel_path: str,
    content: bytes,
    *,
    overwrite: bool = False,
) -> Path:
    """Atomically write ``assets/{rel_path}`` (binary or text) into a package.

    Args:
        package_dir: Target package directory.
        rel_path: Asset relative path under ``assets/`` (e.g. ``"templates/report.docx"``).
        content: Raw file bytes.
        overwrite: Overwrite existing file if ``True``.

    Returns:
        Path to the written asset file.

    Raises:
        ValueError: If the path is unsafe.
        FileExistsError: If ``overwrite=False`` and the file exists.
    """
    safe_rel = _safe_asset_path(rel_path)
    assets_dir = package_dir / "assets"
    target = assets_dir / safe_rel
    target.parent.mkdir(parents=True, exist_ok=True)

    if not overwrite and target.exists():
        raise FileExistsError(f"assets/{safe_rel.as_posix()} already exists at {target}")

    _atomic_write_bytes(target, content)
    return target


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


def _atomic_write_bytes(target: Path, content: bytes) -> None:
    """Write raw bytes to a temp file, then os.rename for atomicity."""
    fd, tmp_path = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    try:
        os.write(fd, content)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp_path, target)
