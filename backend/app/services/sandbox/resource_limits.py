"""Resource limits configuration for sandbox containers.

Defines per-runtime CPU, memory, and timeout defaults used by the
Docker sandbox runner.  Skills can override these via the ``runtime``
frontmatter field (e.g. ``runtime: python-3.12-2g`` for 2GB RAM).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SandboxResourceLimits:
    """Resource limits for a sandbox container."""

    memory: str = "256m"       # Docker --memory flag value
    memory_swap: str = "256m"  # --memory-swap (must equal memory for no-swap)
    cpus: str = "1"            # --cpus
    timeout: int = 120         # seconds
    tmpfs_size: str = "256m"   # /tmp writable area
    pids_limit: int = 64       # max processes
    # Disk I/O limits (bps)
    device_read_bps: str = "50m"
    device_write_bps: str = "50m"
    # ulimit
    nofile: int = 1024


# Default limits per runtime category
_LIMITS_BY_RUNTIME: dict[str, SandboxResourceLimits] = {
    "python": SandboxResourceLimits(),
    "python-1g": SandboxResourceLimits(memory="1g", memory_swap="1g", tmpfs_size="512m"),
    "python-2g": SandboxResourceLimits(memory="2g", memory_swap="2g", tmpfs_size="1g"),
    "node": SandboxResourceLimits(memory="512m", memory_swap="512m", tmpfs_size="256m"),
    "node-1g": SandboxResourceLimits(memory="1g", memory_swap="1g", tmpfs_size="512m"),
    "bash": SandboxResourceLimits(memory="128m", memory_swap="128m", tmpfs_size="128m", timeout=60),
    "default": SandboxResourceLimits(),
}


def get_resource_limits(runtime: str | None) -> SandboxResourceLimits:
    """Return resource limits for a given runtime string.

    If the runtime includes a custom memory suffix (e.g.
    ``python-2g``), the limits are looked up directly.  Unknown
    runtimes fall back to ``"default"``.
    """
    if not runtime:
        return _LIMITS_BY_RUNTIME["default"]

    runtime = runtime.strip().lower()

    # Direct match
    if runtime in _LIMITS_BY_RUNTIME:
        return _LIMITS_BY_RUNTIME[runtime]

    # Try to infer base runtime (e.g. "python-3.12" → "python")
    base = runtime.split("-")[0]
    if base in _LIMITS_BY_RUNTIME:
        return _LIMITS_BY_RUNTIME[base]

    return _LIMITS_BY_RUNTIME["default"]


def parse_runtime_info(runtime: str | None) -> dict:
    """Extract runtime metadata from a runtime string.

    Example: ``python-3.12-2g`` returns
        {"engine": "python", "version": "3.12", "memory": "2g"}
    """
    if not runtime:
        return {"engine": "python", "version": "3", "memory": "256m"}

    runtime = runtime.strip().lower()
    parts = runtime.split("-")

    engine = parts[0] if parts else "python"
    version = "3"
    memory = "256m"

    for p in parts[1:]:
        if p.replace(".", "").isdigit():
            version = p
        elif p.endswith("m") or p.endswith("g"):
            memory = p

    return {"engine": engine, "version": version, "memory": memory}


# Mapping from engine to Docker image
_RUNTIME_IMAGES: dict[str, str] = {
    "python": "zhanlu-sandbox-python:latest",
    "node": "zhanlu-sandbox-node:latest",
    "bash": "zhanlu-sandbox-bash:latest",
}


def get_runtime_image(runtime: str | None) -> str:
    """Return the Docker image name for a given runtime."""
    if not runtime:
        return _RUNTIME_IMAGES.get("python", "zhanlu-sandbox-python:latest")

    info = parse_runtime_info(runtime)
    return _RUNTIME_IMAGES.get(info["engine"], "zhanlu-sandbox-python:latest")
