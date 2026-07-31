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

            # Run the container
            try:
                command = ["python", "/input/skill/sandbox_runner.py"]
                result = run_sandbox_container(
                    image_name=image,
                    input_dir=input_dir,
                    output_dir=output_dir,
                    command=command,
                    timeout=job.timeout_seconds,
                    host_input_dir=host_input_dir,
                    host_output_dir=host_output_dir,
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


def main():
    """Main worker loop — poll for jobs and execute them."""
    logger.info("Sandbox worker started")
    logger.info("Docker available: %s", is_docker_available())
    logger.info("Redis URL: %s", settings.REDIS_URL or "(not configured)")

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
