"""Sandbox Worker — standalone service that executes sandbox jobs.

This is the ONLY service with Docker socket access.  It:
1. Polls Redis for new sandbox jobs (BRPOP on "sandbox:queue")
2. Creates a temporary Docker container with security limits
3. Mounts the input package (read-only) and output directory (writable)
4. Streams events back to PostgreSQL + Redis pub/sub
5. Collects outputs and stores them as artifact blobs
6. Destroys the container after execution

When Redis is not available (local dev), it falls back to polling the
PostgreSQL sandbox_jobs table for queued jobs.

Usage:
    cd backend && python -m sandbox_worker.main

Or as a Docker service in docker-compose.yml.
"""

import json
import logging
import os
import sys
import time
import tempfile
import shutil
from datetime import datetime
from typing import Optional

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings
from app.database import SessionLocal
from app.models.sandbox_job import SandboxJob
from app.services.sandbox.sandbox_service import SandboxService
from app.services.sandbox.container_manager import (
    is_docker_available, run_sandbox_container,
    prepare_input_package, collect_outputs,
    write_runner_script,
)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")


# Default sandbox image mapping by skill type
IMAGE_MAP = {
    "pptx": "zhanlu-sandbox-pptx:latest",
    "docx": "zhanlu-sandbox-office:latest",
    "xlsx": "zhanlu-sandbox-office:latest",
    "pdf": "zhanlu-sandbox-office:latest",
    "md": "zhanlu-sandbox-python:latest",
    "html": "zhanlu-sandbox-python:latest",
    "chart": "zhanlu-sandbox-python:latest",
    "dashboard": "zhanlu-sandbox-python:latest",
    "skill_runner": "zhanlu-sandbox-skill:latest",  # C-Heavy skill-driven path
    "default": "zhanlu-sandbox-python:latest",
}


_HOST_TMP_ROOT_CACHE: Optional[str] = None


def _host_tmp_root() -> str:
    """Resolve the host-side path of ``settings.SANDBOX_TMP_ROOT``.

    The worker runs inside a container but talks to the HOST Docker daemon
    via the mounted socket.  Bind-mount sources (``-v <src>:...``) for the
    sandbox containers it spawns are resolved by the daemon on the HOST
    filesystem, so container-local paths would mount empty directories.

    We discover our own container's mount for ``SANDBOX_TMP_ROOT`` via
    ``docker inspect $HOSTNAME`` and use its host ``Source`` path.  Falls
    back to the container path unchanged (correct when the worker runs
    directly on the host, e.g. local dev).
    """
    global _HOST_TMP_ROOT_CACHE
    if _HOST_TMP_ROOT_CACHE:
        return _HOST_TMP_ROOT_CACHE

    tmp_root = settings.SANDBOX_TMP_ROOT
    host_root = tmp_root
    try:
        import socket
        import subprocess
        out = subprocess.run(
            ["docker", "inspect", socket.gethostname(),
             "--format", "{{json .Mounts}}"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            for mount in json.loads(out.stdout or "[]"):
                if mount.get("Destination") == tmp_root and mount.get("Source"):
                    host_root = mount["Source"]
                    break
    except Exception as e:
        logger.warning("Could not resolve host path for %s: %s — using as-is", tmp_root, e)

    logger.info("Sandbox tmp root: container=%s → host=%s", tmp_root, host_root)
    _HOST_TMP_ROOT_CACHE = host_root
    return host_root


def poll_redis_queue():
    """Poll Redis for new sandbox jobs (BRPOP with 1s timeout)."""
    from app.database import get_redis
    redis = get_redis()
    if not redis:
        return None

    result = redis.brpop("sandbox:queue", timeout=1)
    if result:
        _, data = result
        return json.loads(data)
    return None


def poll_db_queue(db) -> Optional[dict]:
    """Fallback: poll the database for queued jobs (when Redis is not available)."""
    job = (
        db.query(SandboxJob)
        .filter(SandboxJob.status == "queued")
        .order_by(SandboxJob.created_date)
        .first()
    )
    if job:
        return {"job_id": job.id, "skill_name": job.skill_name}
    return None


def execute_job(job_id: str, skill_name: str):
    """Execute a single sandbox job."""
    db = SessionLocal()
    try:
        service = SandboxService(db)
        job = service.get_job(job_id)
        if not job:
            logger.error("Job %s not found", job_id)
            return

        # Determine image
        image = job.image_name or IMAGE_MAP.get(skill_name, IMAGE_MAP["default"])

        # Update status to running
        service.update_job_status(job_id, "running")
        service.record_event(job_id, "job_started", f"Starting sandbox container with image '{image}'")

        if not is_docker_available():
            # Fallback: in-process execution (dev mode)
            logger.warning("Docker not available — using in-process fallback for job %s", job_id)
            service.record_event(job_id, "job_failed", "Docker not available — in-process fallback")
            _execute_in_process(service, job)
            return

        # Create temp directories under the shared tmp root.  That root is
        # bind-mounted from the host into this worker container, so files
        # written here are visible to the host Docker daemon — which is what
        # actually performs the bind mounts for the sandbox container.
        tmp_root = settings.SANDBOX_TMP_ROOT
        os.makedirs(tmp_root, exist_ok=True)
        host_root = _host_tmp_root()
        with tempfile.TemporaryDirectory(prefix="sandbox_", dir=tmp_root) as workspace:
            input_dir = os.path.join(workspace, "input")
            output_dir = os.path.join(workspace, "output")
            os.makedirs(input_dir, exist_ok=True)
            os.makedirs(output_dir, exist_ok=True)
            # Ensure the output dir is writable by the container's non-root user
            os.chmod(output_dir, 0o777)

            # Host-side equivalents of input_dir/output_dir (DooD translation)
            host_workspace = host_root + workspace[len(tmp_root):]
            host_input_dir = os.path.join(host_workspace, "input")
            host_output_dir = os.path.join(host_workspace, "output")
            # The spawned sandbox container is created by the HOST Docker
            # daemon, so bind-mount permissions are evaluated on the host-side
            # paths, not just the worker container paths. Make both sides
            # traversable/writable so non-root sandbox users can write outputs.
            for _path, _mode in ((workspace, 0o755), (input_dir, 0o755), (output_dir, 0o777), (host_workspace, 0o755), (host_input_dir, 0o755), (host_output_dir, 0o777)):
                try:
                    os.chmod(_path, _mode)
                except OSError as chmod_err:
                    logger.warning("Could not chmod sandbox path %s to %o: %s", _path, _mode, chmod_err)

            # Prepare input package
            if job.input_package:
                prepare_input_package(input_dir, job.input_package)
                service.record_event(job_id, "input_prepared", f"Input package materialized at {input_dir}")

                # Write the sandbox runner script if it's in the input package
                runner_b64 = job.input_package.get("runner_script")
                if runner_b64:
                    script_name = job.input_package.get("runner_script_name", "sandbox_runner.py")
                    write_runner_script(input_dir, runner_b64, script_name)
                    service.record_event(job_id, "script_prepared", f"Runner script written to /input/skill/{script_name}")

                # Write any additional runner modules (e.g. for skill-driven
                # jobs that need llm_client.py + fallback_generator.py next
                # to the main runner script so the runner can ``import``
                # them at /input/skill/).
                runner_modules = job.input_package.get("runner_modules_b64") or {}
                if runner_modules:
                    from app.services.sandbox.container_manager import write_runner_modules
                    written = write_runner_modules(input_dir, runner_modules)
                    service.record_event(
                        job_id,
                        "modules_prepared",
                        f"Wrote {len(written)} runner modules: {sorted(written)}",
                    )

            # Run the container
            try:
                # Skill-driven jobs use a different runner script and
                # image; we also need to bind-mount the LLM proxy
                # socket and pass its config via env vars.
                is_skill_driven = (
                    job.input_package
                    and job.input_package.get("runner_script_name") == "skill_driven_runner.py"
                ) or job.skill_name == "skill_runner"

                if is_skill_driven:
                    runner_script_name = "skill_driven_runner.py"
                    # Skill-driven jobs default to the new unified image
                    # unless the caller already specified something else.
                    skill_image = (
                        job.image_name
                        or IMAGE_MAP.get("skill_runner")
                        or IMAGE_MAP["default"]
                    )
                    # LLM proxy mount (None when disabled / not ready).
                    extra_mounts = _resolve_llm_proxy_mount()
                    # Tell the runner where the proxy lives + which model
                    # to ask for.
                    skill_env = {
                        "LLM_PROXY_SOCKET": settings.SANDBOX_LLM_PROXY_SOCKET_IN_CONTAINER,
                        "LLM_PROXY_MODEL": settings.SANDBOX_LLM_PROXY_MODEL,
                        "PYTHONUNBUFFERED": "1",
                    }
                    # The proxy may be disabled but the job still wants
                    # to run — the runner will fall back to the
                    # deterministic generator when LLM_PROXY_SOCKET is
                    # absent or unreachable.  That's fine.
                else:
                    runner_script_name = job.input_package.get(
                        "runner_script_name", "sandbox_runner.py"
                    ) if job.input_package else "sandbox_runner.py"
                    skill_image = image
                    extra_mounts = None
                    skill_env = {"PYTHONUNBUFFERED": "1"}

                command = ["python", f"/input/skill/{runner_script_name}"]

                # Merge base env vars (always PYTHONUNBUFFERED) with any
                # per-job extras from the input package.
                env_vars = dict(skill_env)
                if job.input_package and job.input_package.get("env_vars"):
                    env_vars.update(job.input_package["env_vars"])

                result = run_sandbox_container(
                    image_name=skill_image,
                    input_dir=input_dir,
                    output_dir=output_dir,
                    command=command,
                    timeout=job.timeout_seconds,
                    host_input_dir=host_input_dir,
                    host_output_dir=host_output_dir,
                    extra_mounts=extra_mounts,
                    env_vars=env_vars,
                )

                # Record command
                service.record_command(
                    job_id=job_id,
                    command=" ".join(command),
                    exit_code=result["exit_code"],
                    stdout=result["stdout"],
                    stderr=result["stderr"],
                    duration_ms=result["duration_ms"],
                )

                if result["exit_code"] == 0:
                    # Collect outputs
                    outputs = collect_outputs(output_dir)
                    service.record_event(job_id, "file_created", f"Generated {len(outputs)} output files",
                                         {"files": [o["file_name"] for o in outputs]})

                    # Store outputs as artifact blobs
                    if job.artifact_version_id:
                        _store_outputs_as_blobs(db, job, outputs, service)

                    service.record_event(job_id, "job_completed", "Sandbox execution completed successfully")
                    service.update_job_status(job_id, "completed", exit_code=0)
                else:
                    service.record_event(job_id, "job_failed",
                                        f"Container exited with code {result['exit_code']}",
                                        {"stderr": result["stderr"][:500]})
                    service.update_job_status(job_id, "failed", exit_code=result["exit_code"],
                                             error_message=result["stderr"][:1000])

            except Exception as e:
                logger.error("Sandbox execution error: %s", e)
                service.record_event(job_id, "job_failed", str(e))
                service.update_job_status(job_id, "failed", error_message=str(e))

    finally:
        db.close()


def _execute_in_process(service: SandboxService, job: SandboxJob):
    """Fallback: execute skill in-process (when Docker is not available).

    This is for local dev only — no isolation guarantees.
    """
    try:
        # Try to import and run the skill directly
        from app.services.skills_loader import get_skills_registry
        registry = get_skills_registry()

        skill = registry.get(job.skill_name)
        if not skill:
            service.record_event(job.id, "job_failed", f"Skill '{job.skill_name}' not found")
            service.update_job_status(job.id, "failed", error_message=f"Skill '{job.skill_name}' not found")
            return

        service.record_event(job.id, "command_started", f"Running skill '{job.skill_name}' in-process")

        # Execute skill
        config = (job.input_package or {}).get("skill_config", {})
        result = skill.execute(config) if hasattr(skill, "execute") else None

        if result:
            service.record_event(job.id, "job_completed", "Skill executed successfully (in-process)")
            service.update_job_status(job.id, "completed", exit_code=0)
        else:
            service.record_event(job.id, "job_completed", "Skill executed (no return value)")
            service.update_job_status(job.id, "completed", exit_code=0)

    except Exception as e:
        logger.error("In-process execution failed: %s", e)
        service.record_event(job.id, "job_failed", str(e))
        service.update_job_status(job.id, "failed", error_message=str(e))


def _store_outputs_as_blobs(db, job: SandboxJob, outputs: list[dict], service: SandboxService):
    """Store collected output files as artifact blobs.

    The sandbox runner writes a ``build_manifest.json`` next to the
    actual report (e.g. ``report.docx``); the manifest is metadata
    about the generation, not a downloadable artifact.  Filtering it
    out here means the artifact's ``original`` blob slot is reserved
    for the file the user actually wants to download.
    """
    from app.services.artifacts.artifact_service import ArtifactService
    artifact_service = ArtifactService(db)

    artifact_ids = []
    for output in outputs:
        # Skip the generation manifest — it duplicates the artifact
        # row's metadata and would otherwise claim the "first blob"
        # slot, causing the download endpoint to return JSON instead
        # of the user's report file.
        if output.get("file_name") == "build_manifest.json":
            continue
        import base64
        data = base64.b64decode(output["data_base64"])

        if job.artifact_version_id:
            blob = artifact_service.store_blob(
                version_id=job.artifact_version_id,
                blob_type="original",
                file_name=output["file_name"],
                mime_type=output["mime_type"],
                data=data,
            )
            service.record_event(job.id, "file_stored",
                                f"Stored {output['file_name']} as blob {blob.id}")

    # Mark version as built
    if job.artifact_version_id:
        artifact_service.mark_version_built(job.artifact_version_id)


def _start_llm_proxy_background():
    """Start the LLM Unix-socket proxy in a daemon thread.

    The proxy gives skill-driven sandbox containers access to the LLM
    API without TCP/IP network (the sandbox runs --network none).
    Without it, skill-driven jobs would fall back to the deterministic
    generator immediately.

    The thread dies with the main process (daemon=True) — no orphaned
    socket files left behind.
    """
    if not getattr(settings, "SANDBOX_LLM_PROXY_ENABLED", False):
        logger.info("LLM proxy disabled (SANDBOX_LLM_PROXY_ENABLED=false)")
        return None
    import threading
    from app.services.sandbox.llm_proxy import LLMProxy

    socket_path = settings.SANDBOX_LLM_PROXY_SOCKET
    # Ensure the socket directory exists and is writable by the worker user.
    try:
        os.makedirs(os.path.dirname(socket_path), exist_ok=True)
    except OSError as e:
        logger.warning("Cannot create LLM proxy socket dir: %s", e)
        return None

    container: dict = {"proxy": None, "loop": None, "ready": threading.Event()}

    def _runner():
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        container["loop"] = loop
        proxy = LLMProxy(socket_path=socket_path)
        container["proxy"] = proxy
        try:
            loop.run_until_complete(proxy.start())
            container["ready"].set()
            # Block forever; cancellation arrives when the daemon thread
            # is killed (parent process exit).
            loop.run_forever()
        except Exception:  # noqa: BLE001
            logger.exception("LLM proxy crashed")
            container["ready"].set()
        finally:
            try:
                loop.run_until_complete(proxy.stop())
            except Exception:
                pass
            loop.close()

    thread = threading.Thread(target=_runner, name="llm-proxy", daemon=True)
    thread.start()
    # Wait briefly for the proxy to bind the socket so the first job
    # doesn't race the startup.
    if not container["ready"].wait(timeout=10):
        logger.warning("LLM proxy did not become ready within 10s")
    else:
        logger.info("LLM proxy thread started on unix:%s", socket_path)
    return container


def _resolve_llm_proxy_mount() -> list[dict] | None:
    """Build the extra_mounts entry to bind-mount the LLM proxy socket
    into a sandbox container.

    Returns None when the proxy is disabled (caller should skip the
    mount).  The container sees the socket at
    ``settings.SANDBOX_LLM_PROXY_SOCKET_IN_CONTAINER`` and reads
    ``LLM_PROXY_SOCKET`` from its environment to find it.
    """
    if not getattr(settings, "SANDBOX_LLM_PROXY_ENABLED", False):
        return None
    host_socket = settings.SANDBOX_LLM_PROXY_SOCKET
    if not os.path.exists(host_socket):
        # Proxy didn't start — skip the mount so the container doesn't
        # fail at bind-mount time.  The runner will see no socket and
        # fall back to the deterministic generator.
        logger.warning(
            "LLM proxy socket %s does not exist — skipping mount",
            host_socket,
        )
        return None
    return [{
        "source": host_socket,
        "target": settings.SANDBOX_LLM_PROXY_SOCKET_IN_CONTAINER,
        "read_only": True,
    }]


def main():
    """Main worker loop — poll for jobs and execute them."""
    logger.info("Sandbox worker started")
    logger.info("Docker available: %s", is_docker_available())
    logger.info("Redis URL: %s", settings.REDIS_URL or "(not configured)")

    # Start the LLM proxy in a background daemon thread (if enabled).
    # This must happen BEFORE the main loop starts accepting jobs so the
    # first skill-driven job doesn't race the socket creation.
    _start_llm_proxy_background()

    use_redis = bool(settings.REDIS_URL)

    while True:
        try:
            job_data = None

            if use_redis:
                job_data = poll_redis_queue()
            else:
                # Fallback: poll DB
                db = SessionLocal()
                try:
                    job_data = poll_db_queue(db)
                finally:
                    db.close()
                if not job_data:
                    time.sleep(1)  # Avoid busy-waiting

            if job_data:
                logger.info("Picked up job %s (skill=%s)", job_data["job_id"], job_data.get("skill_name"))
                execute_job(job_data["job_id"], job_data.get("skill_name", "unknown"))

        except KeyboardInterrupt:
            logger.info("Worker interrupted — shutting down")
            break
        except Exception as e:
            logger.error("Worker error: %s", e)
            time.sleep(5)  # Back off on error


if __name__ == "__main__":
    main()
