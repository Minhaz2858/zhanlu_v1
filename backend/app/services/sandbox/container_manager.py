"""Container manager — Docker container lifecycle for sandbox execution.

Creates temporary Docker containers with strict security limits:
  --network none     — no network access
  --read-only        — filesystem read-only (except /tmp and /output)
  --memory 1g        — memory limit
  --cpus 1           — CPU limit
  --cap-drop ALL     — drop all Linux capabilities

The container mounts:
  /input   (read-only)  — approved skill + DataSnapshot + template
  /output  (writable)   — generated files

After execution, outputs are collected and the container is destroyed.
"""

import logging
import shutil
from typing import Optional

logger = logging.getLogger(__name__)

# Output collection size caps — prevent OOM from large sandbox outputs
MAX_OUTPUT_FILE_SIZE = 50 * 1024 * 1024   # 50 MB per file
MAX_TOTAL_OUTPUT_SIZE = 200 * 1024 * 1024  # 200 MB total across all files


# Fallback extension→MIME map for formats that the system mimetypes
# database (often trimmed in minimal container images) doesn't know
# about.  When ``mimetypes.guess_type()`` returns ``None`` we consult
# this map before falling back to ``application/octet-stream``.
_FALLBACK_MIME_BY_EXT = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".ppt": "application/vnd.ms-powerpoint",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".pdf": "application/pdf",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}

# Check if Docker is available
_DOCKER_PATH = shutil.which("docker")


def is_docker_available() -> bool:
    """Check if Docker is available on this system."""
    return _DOCKER_PATH is not None


def run_sandbox_container(
    image_name: str,
    input_dir: str,
    output_dir: str,
    command: list[str],
    timeout: int = 120,
    memory: str = "1g",
    cpus: str = "1",
    env_vars: Optional[dict] = None,
    host_input_dir: Optional[str] = None,
    host_output_dir: Optional[str] = None,
    extra_mounts: Optional[list[dict]] = None,
) -> dict:
    """Run a command in an isolated Docker container.

    Args:
        host_input_dir / host_output_dir: When the caller runs inside a
            container and talks to the host Docker daemon over a mounted
            socket (Docker-outside-of-Docker), ``input_dir``/``output_dir``
            are container-local paths that the daemon cannot resolve.
            These optional overrides are the host-side paths used for the
            ``-v`` bind mounts, while the caller keeps using the local
            paths for writing inputs and collecting outputs.  Defaults to
            ``input_dir``/``output_dir`` (direct host execution).
        extra_mounts: List of additional bind mounts to apply inside the
            sandbox container.  Each entry is a dict with keys:
              - ``source`` (host path, required; for DooD the host path)
              - ``target`` (in-container path, required)
              - ``read_only`` (bool, default True)
            Used by the skill-driven runner to bind-mount the host-side
            LLM proxy Unix socket at ``/var/run/llm-proxy.sock`` so the
            container can call the LLM API without TCP/IP network access.

    Returns:
        {
            "exit_code": int,
            "stdout": str,
            "stderr": str,
            "container_id": str,
            "duration_ms": int,
        }

    Raises RuntimeError if Docker is not available.
    """
    if not is_docker_available():
        raise RuntimeError(
            "Docker is not available — sandbox execution requires Docker. "
            "Install Docker or use the fallback in-process executor."
        )

    import subprocess
    import tempfile
    import os
    import time
    import uuid

    start_time = time.time()

    # DooD: bind-mount sources are resolved by the (host) Docker daemon,
    # so use the host-side paths when provided.
    mount_input = host_input_dir or input_dir
    mount_output = host_output_dir or output_dir

    # Generate a unique container name so we can force-remove it on timeout.
    # --rm alone only fires when the container exits on its own; on TimeoutExpired
    # the subprocess kills the docker-run CLIENT but the container keeps running.
    container_name = f"zhanlu-sbx-{uuid.uuid4().hex[:12]}"

    # Build Docker run command with security constraints
    docker_cmd = [
        _DOCKER_PATH, "run",
        "--name", container_name,  # named so we can docker rm -f on timeout
        "--rm",                    # Remove container after exit
        "--network", "none",       # No network access
        "--read-only",             # Read-only filesystem
        "--memory", memory,        # Memory limit
        "--cpus", cpus,            # CPU limit
        "--cap-drop", "ALL",       # Drop all capabilities
        "--tmpfs", "/tmp:rw,size=256m",  # Writable /tmp
        "-v", f"{mount_input}:/input:ro",   # Input mounted read-only
        "-v", f"{mount_output}:/output:rw", # Output writable
        "-w", "/output",           # Working directory
    ]

    # Add any extra bind mounts (e.g. LLM proxy Unix socket).
    if extra_mounts:
        for m in extra_mounts:
            src = m.get("source")
            tgt = m.get("target")
            ro = bool(m.get("read_only", True))
            if not src or not tgt:
                logger.warning("Skipping malformed extra_mount: %r", m)
                continue
            ro_suffix = ":ro" if ro else ":rw"
            docker_cmd.extend(["-v", f"{src}:{tgt}{ro_suffix}"])

    # Add environment variables
    if env_vars:
        for key, value in env_vars.items():
            docker_cmd.extend(["-e", f"{key}={value}"])

    docker_cmd.append(image_name)
    docker_cmd.extend(command)

    logger.info("Starting sandbox container: %s", " ".join(docker_cmd[:10]))

    try:
        result = subprocess.run(
            docker_cmd,
            capture_output=True,
            timeout=timeout,
            text=True,
        )
        duration_ms = int((time.time() - start_time) * 1000)

        return {
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "container_id": None,  # --rm removes the container
            "duration_ms": duration_ms,
        }

    except subprocess.TimeoutExpired:
        duration_ms = int((time.time() - start_time) * 1000)
        # Force-remove the container — the docker-run client was killed but
        # the container is still running, leaking CPU/memory.
        try:
            subprocess.run(
                [_DOCKER_PATH, "rm", "-f", container_name],
                capture_output=True, timeout=15,
            )
            logger.warning("Sandbox container %s timed out and was force-removed", container_name)
        except Exception as cleanup_err:
            logger.error("Failed to force-remove timed-out container %s: %s", container_name, cleanup_err)
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": f"Timeout after {timeout}s",
            "container_id": None,
            "duration_ms": duration_ms,
        }


def prepare_input_package(input_dir: str, input_package: dict):
    """Materialize the input package into the input directory.

    The input package contains:
    - skill_config: dict of skill parameters
    - data_snapshots: list of snapshot data (JSON/CSV)
    - template: optional template file (base64)
    - instructions: str
    """
    import os
    import json
    import base64

    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(os.path.join(input_dir, "data"), exist_ok=True)

    # Write skill config
    config = input_package.get("skill_config", {})
    with open(os.path.join(input_dir, "config.json"), "w") as f:
        json.dump(config, f, indent=2)

    # Write data snapshots
    for i, snapshot in enumerate(input_package.get("data_snapshots", [])):
        snapshot_name = snapshot.get("name", f"snapshot_{i}")
        snapshot_data = snapshot.get("data", [])
        snapshot_format = snapshot.get("format", "json")

        if snapshot_format == "csv":
            with open(os.path.join(input_dir, "data", f"{snapshot_name}.csv"), "w") as f:
                f.write(snapshot.get("csv_content", ""))
        else:
            with open(os.path.join(input_dir, "data", f"{snapshot_name}.json"), "w") as f:
                json.dump(snapshot_data, f, indent=2)

    # Write template if provided
    template = input_package.get("template")
    if template:
        template_data = base64.b64decode(template.get("data_base64", ""))
        template_name = template.get("file_name", "template")
        with open(os.path.join(input_dir, template_name), "wb") as f:
            f.write(template_data)

    # Write instructions
    instructions = input_package.get("instructions", "")
    with open(os.path.join(input_dir, "instructions.md"), "w") as f:
        f.write(instructions)

    # Materialize the skill bundle (SKILL.md + scripts/) so the sandbox
    # container can exec the skill's bundled entry point at
    # /input/skill_bundle/<entry_point>. The caller (skills_tool ``run``
    # action) packages this as base64 because the sandbox worker does not
    # share the skills folder mount — it only has the tmp root + docker
    # socket.
    bundle = input_package.get("skill_bundle") or []
    if bundle:
        bundle_dir = os.path.join(input_dir, "skill_bundle")
        os.makedirs(bundle_dir, exist_ok=True)
        written = 0
        for item in bundle:
            rel = (item.get("path") or "").strip().lstrip("/")
            # Path-traversal guard: entry must stay inside skill_bundle/.
            norm = os.path.normpath(rel).replace("\\", "/")
            if not rel or norm.startswith("..") or os.path.isabs(rel):
                logger.warning(
                    "prepare_input_package: skipping unsafe bundle path %r",
                    item.get("path"),
                )
                continue
            dest = os.path.join(bundle_dir, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            data = base64.b64decode(item.get("data_base64", ""))
            with open(dest, "wb") as f:
                f.write(data)
            written += 1
        logger.info("Wrote %d skill_bundle files into %s", written, bundle_dir)

    # Vendored runner modules (e.g. layout_engine.py, branded_charts.py) — the
    # deterministic sandbox_runner.generate_pptx imports these from /input/skill
    # (already on sys.path).  Skill-driven jobs also rely on this for
    # llm_client.py / fallback_generator.py.
    modules_b64 = input_package.get("runner_modules_b64")
    if modules_b64:
        write_runner_modules(input_dir, modules_b64)

    logger.info("Input package prepared at %s", input_dir)


def collect_outputs(output_dir: str) -> list[dict]:
    """Collect output files from the output directory.

    Returns a list of {file_name, mime_type, data_base64, file_size} dicts.
    """
    import os
    import base64
    import mimetypes

    results = []
    if not os.path.exists(output_dir):
        return results

    max_per_file = MAX_OUTPUT_FILE_SIZE
    max_total = MAX_TOTAL_OUTPUT_SIZE
    total_collected = 0

    for root, dirs, files in os.walk(output_dir):
        for fname in files:
            fpath = os.path.join(root, fname)
            rel_path = os.path.relpath(fpath, output_dir)

            file_size = os.path.getsize(fpath)
            if file_size > max_per_file:
                logger.warning(
                    "Skipping oversized output file %s (%d bytes > %d cap)",
                    rel_path, file_size, max_per_file,
                )
                continue

            if total_collected + file_size > max_total:
                logger.warning(
                    "Output total cap reached (%d bytes); skipping remaining files",
                    total_collected,
                )
                return results

            with open(fpath, "rb") as f:
                data = f.read()

            total_collected += file_size

            mime_type, _ = mimetypes.guess_type(fname)
            if not mime_type:
                # Python's stdlib mimetypes database is often incomplete
                # in minimal container images (Alpine, distroless).  The
                # Office Open XML formats and other common ones used by
                # our skills aren't always registered, so fall back to
                # an explicit extension→mime map before resorting to
                # ``application/octet-stream``.  Without this, the
                # artifact's blob gets a generic mime which the
                # inline-preview endpoint later rejects (it only
                # accepts the precise Office MIME strings), so a
                # perfectly valid .docx shows "Inline preview is not
                # available" even though the file is there.
                mime_type = _FALLBACK_MIME_BY_EXT.get(
                    os.path.splitext(fname)[1].lower()
                ) or "application/octet-stream"

            results.append({
                "file_name": rel_path,
                "mime_type": mime_type,
                "data_base64": base64.b64encode(data).decode(),
                "file_size": len(data),
            })

    logger.info("Collected %d output files from %s", len(results), output_dir)
    return results


def write_runner_script(input_dir: str, runner_script_b64: str, script_name: str = "sandbox_runner.py"):
    """Write the sandbox runner script into the input directory so it's
    available inside the container at /input/<script_name>.

    The tool handler base64-encodes sandbox_runner.py into the input package.
    The sandbox worker decodes it here and writes it to the input folder,
    which is mounted read-only at /input inside the container.

    The container command then runs:  python /input/sandbox_runner.py
    """
    import os
    import base64

    skill_dir = os.path.join(input_dir, "skill")
    os.makedirs(skill_dir, exist_ok=True)

    script_path = os.path.join(skill_dir, script_name)
    script_content = base64.b64decode(runner_script_b64)

    with open(script_path, "wb") as f:
        f.write(script_content)

    logger.info("Wrote runner script to %s (%d bytes)", script_path, len(script_content))


def write_runner_modules(input_dir: str, modules_b64: dict[str, str]) -> list[str]:
    """Write additional runner modules alongside the main runner script.

    Skill-driven jobs (skill_driven_runner.py) need to import sibling
    modules like ``llm_client`` and ``fallback_generator``.  The tool
    handler base64-encodes them into ``runner_modules_b64`` (a dict of
    {filename: base64_content}); this helper decodes each and writes it
    to ``<input_dir>/skill/<filename>`` so the runner can ``import`` them
    via the existing ``sys.path.insert(0, "/input/skill")`` line.

    Returns the list of filenames actually written (skipping any
    whose name would escape the skill directory).
    """
    import os
    import base64

    skill_dir = os.path.join(input_dir, "skill")
    os.makedirs(skill_dir, exist_ok=True)

    written: list[str] = []
    for filename, content_b64 in (modules_b64 or {}).items():
        # Path-traversal guard: only allow simple filenames, no slashes,
        # no parent-directory refs, no absolute paths.
        if not filename or "/" in filename or "\\" in filename or filename.startswith("."):
            logger.warning("Skipping suspicious runner module filename: %r", filename)
            continue
        if not filename.endswith(".py"):
            logger.warning("Skipping non-Python runner module: %r", filename)
            continue
        try:
            content = base64.b64decode(content_b64)
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not decode runner module %s: %s", filename, e)
            continue
        out_path = os.path.join(skill_dir, filename)
        with open(out_path, "wb") as f:
            f.write(content)
        written.append(filename)
        logger.info("Wrote runner module %s (%d bytes)", out_path, len(content))
    return written
