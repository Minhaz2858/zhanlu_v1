"""skills_tool — unified skills search, load, and invoke.

Wraps the existing ``app.services.skill_sync`` and
``app.services.skills_loader`` subsystems into a single LLM-callable
tool. Hermes' skills_tool offers the same operations but with a more
extensive catalog; here we adapt the surface to zhanlu's existing
infrastructure.

Actions:
  - search: list available skills matching a query
  - load: read a single skill's full SKILL.md content
  - execute: invoke a skill (for skills that declare executable actions)
  - list_categories: enumerate the skill categories
"""

from __future__ import annotations

import base64
import logging
import os
import time
from pathlib import Path
from typing import List, Optional

from sqlalchemy.orm import Session

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


async def _skills_tool(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    action = (args.get("action") or "search").lower()

    if action == "list_categories":
        try:
            from app.services.skills_loader import list_skill_categories
            cats = list_skill_categories()
            return {"success": True, "categories": cats}
        except Exception as exc:
            return {"success": False, "error": f"Could not list categories: {exc}"}

    if action == "search":
        query = (args.get("query") or "").strip()
        limit = min(int(args.get("limit", 20)), 100)
        try:
            from app.services.skills_loader import unified_search
            results = unified_search(query, limit=limit, db=db)
            return {"success": True, "query": query, "count": len(results), "results": results}
        except Exception as exc:
            return {"success": False, "error": f"Search failed: {exc}"}

    if action == "load":
        name = (args.get("name") or "").strip()
        if not name:
            return {"success": False, "error": "name is required"}
        from app.services.skill_execution_recorder import SkillExecutionRecorder
        _start = time.monotonic()
        try:
            # The skills_loader module exposes `get_skill(name)` which
            # returns a SkillMetadata dataclass with a `.body` attribute
            # (the SKILL.md content). The previous implementation
            # imported `load_skill`, which does not exist — every call
            # raised ImportError, the model interpreted the failure as
            # "skill not found", and the agent_builder got stuck
            # loading skills that all "don't exist" instead of calling
            # create_agent to actually build the agent.
            from app.services.skills_loader import get_skill, list_skill_scripts
            meta = get_skill(name)
            if meta is None:
                SkillExecutionRecorder.record_from_context(name, "load", "failed", context, int((time.monotonic()-_start)*1000), f"Skill not found: {name}")
                return {"success": False, "error": f"Skill not found: {name}"}
            SkillExecutionRecorder.record_from_context(name, "load", "completed", context, int((time.monotonic()-_start)*1000))
            return {
                "success": True,
                "name": meta.name,
                "description": meta.description,
                "category": meta.category,
                "tags": meta.tags,
                "content": meta.body,
                # P2: advertise the skill's bundled runnable scripts so
                # the agent can invoke them via action="run".
                "scripts": list_skill_scripts(name),
            }
        except Exception as exc:
            SkillExecutionRecorder.record_from_context(name, "load", "failed", context, int((time.monotonic()-_start)*1000), str(exc))
            return {"success": False, "error": f"Load failed: {exc}"}

    if action == "execute":
        name = (args.get("name") or "").strip()
        inputs = args.get("inputs", {}) or {}
        if not name:
            return {"success": False, "error": "name is required"}
        # Skills in zhanlu are prompts, not runnable — this returns a
        # structured instruction telling the agent to inject the skill
        # content into the conversation as guidance.
        from app.services.skill_execution_recorder import SkillExecutionRecorder
        _start = time.monotonic()
        try:
            from app.services.skills_loader import get_skill
            meta = get_skill(name)
            if meta is None:
                SkillExecutionRecorder.record_from_context(name, "execute", "failed", context, int((time.monotonic()-_start)*1000), f"Skill not found: {name}")
                return {"success": False, "error": f"Skill not found: {name}"}
            SkillExecutionRecorder.record_from_context(name, "execute", "completed", context, int((time.monotonic()-_start)*1000), input_json=inputs)
            return {
                "success": True,
                "name": meta.name,
                "instruction": (
                    f"Skill {name!r} is now active for this turn. "
                    f"Follow the methodology in the SKILL.md content below. "
                    f"Inputs: {inputs}"
                ),
                "skill_content": meta.body[:8000],   # truncated
                "inputs": inputs,
            }
        except Exception as exc:
            SkillExecutionRecorder.record_from_context(name, "execute", "failed", context, int((time.monotonic()-_start)*1000), str(exc))
            return {"success": False, "error": f"Execute failed: {exc}"}

    if action == "collect":
        # Collect a skill from a web URL using agent-browser.
        # The skill_agent uses this interactively during chat to scrape
        # skill documentation from any website.
        url = (args.get("url") or "").strip()
        if not url:
            return {"success": False, "error": "url is required for collect"}
        skill_name = (args.get("name") or "").strip() or None
        try:
            from app.services.skill_collection_service import SkillCollectionService
            service = SkillCollectionService(db=db)
            result = await service.collect_from_url(
                url=url,
                skill_name=skill_name,
                user_id=user_id,
            )
            return result
        except Exception as exc:
            return {"success": False, "error": f"Collect failed: {exc}"}

    if action == "run":
        # P2: execute a skill's bundled script in the Docker sandbox.
        # The skill must exist in the curated registry (guard: unknown
        # skills are never raw-exec'd). See _run_skill_script for the
        # bundle + runner construction.
        name = (args.get("name") or "").strip()
        entry_point = (args.get("entry_point") or "").strip()
        if not name:
            return {"success": False, "error": "name is required"}
        if not entry_point:
            return {"success": False, "error": "entry_point is required"}
        from app.services.skill_execution_recorder import SkillExecutionRecorder
        _run_start = time.monotonic()
        result = _run_skill_script(
            db=db,
            name=name,
            entry_point=entry_point,
            args=args.get("args") or [],
            inputs=args.get("inputs") or {},
            image=args.get("image"),
            timeout=int(args.get("timeout", 120)),
        )
        SkillExecutionRecorder.record_from_context(
            name, "run",
            "completed" if result.get("success") else "failed",
            context,
            int((time.monotonic() - _run_start) * 1000),
            result.get("error"),
            input_json={"entry_point": entry_point},
        )
        return result

    if action == "read_reference":
        # Folder-style progressive disclosure: load a references/*.md file
        # on demand (never the full SKILL.md).
        name = (args.get("name") or "").strip()
        filename = (args.get("filename") or "").strip()
        if not name or not filename:
            return {"success": False, "error": "name and filename are required"}
        try:
            from app.services.skills_loader import get_skill_dir
            skill_dir = get_skill_dir(name)
            if not skill_dir:
                return {"success": False, "error": f"Skill not found: {name}"}
            ref_path = _resolve_reference_path(skill_dir, filename)
            if ref_path is None:
                return {"success": False, "error": f"Reference not found: {filename}"}
            return {
                "success": True,
                "name": name,
                "filename": filename,
                "content": ref_path.read_text(encoding="utf-8"),
            }
        except Exception as exc:
            return {"success": False, "error": f"read_reference failed: {exc}"}

    if action == "list_assets":
        # List asset files (assets/**) available for a skill, with sizes.
        name = (args.get("name") or "").strip()
        if not name:
            return {"success": False, "error": "name is required"}
        try:
            from app.services.skills_loader import get_skill_dir
            skill_dir = get_skill_dir(name)
            if not skill_dir:
                return {"success": False, "error": f"Skill not found: {name}"}
            assets = _list_asset_files(skill_dir)
            return {"success": True, "name": name, "assets": assets, "count": len(assets)}
        except Exception as exc:
            return {"success": False, "error": f"list_assets failed: {exc}"}

    if action == "download_asset":
        # Download a single asset file as base64 (for template reuse).
        name = (args.get("name") or "").strip()
        rel_path = (args.get("path") or "").strip()
        if not name or not rel_path:
            return {"success": False, "error": "name and path are required"}
        try:
            from app.services.skills_loader import get_skill_dir
            skill_dir = get_skill_dir(name)
            if not skill_dir:
                return {"success": False, "error": f"Skill not found: {name}"}
            asset_path = _resolve_asset_path(skill_dir, rel_path)
            if asset_path is None:
                return {"success": False, "error": f"Asset not found: {rel_path}"}
            data = asset_path.read_bytes()
            return {
                "success": True,
                "name": name,
                "path": rel_path,
                "size": len(data),
                "data_base64": base64.b64encode(data).decode(),
            }
        except Exception as exc:
            return {"success": False, "error": f"download_asset failed: {exc}"}

    if action == "semantic_search":
        # Embedding-based skill discovery with RRF fusion vs keyword search.
        query = (args.get("query") or "").strip()
        limit = min(int(args.get("limit", 10)), 50)
        if not query:
            return {"success": False, "error": "query is required"}
        try:
            from app.services.skill_studio.semantic_finder import semantic_search
            results = semantic_search(query, db, user_id=user_id, limit=limit)
            return {
                "success": True,
                "query": query,
                "count": len(results),
                "results": [r.__dict__ for r in results],
            }
        except Exception as exc:
            return {"success": False, "error": f"semantic_search failed: {exc}"}

    return {"success": False, "error": f"Unknown action: {action!r}"}


SKILLS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "skills",
        "description": (
            "Unified skills tool: search, load, execute, run, and collect skills. "
            "Skills are reusable methodology documents (SKILL.md); some also "
            "ship bundled scripts (in scripts/) that the 'run' action "
            "executes inside an isolated Docker sandbox. Use 'collect' to "
            "scrape a skill from a web URL — the agent-browser extracts "
            "page content and the LLM structures it as a SKILL.md."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["search", "load", "execute", "run", "collect", "list_categories", "read_reference", "list_assets", "download_asset", "semantic_search"]},
                "query": {"type": "string", "description": "Search query (for action='search'/'semantic_search')."},
                "limit": {"type": "integer", "description": "Max results (for action='search'/'semantic_search').", "default": 20},
                "name": {"type": "string", "description": "Skill name (for action='load'/'execute'/'run'/'collect'/'read_reference'/'list_assets'/'download_asset')."},
                "url": {"type": "string", "description": "Web URL to collect a skill from (for action='collect')."},
                "filename": {"type": "string", "description": "Reference filename under references/ (for action='read_reference'), e.g. 'output-formats.md'."},
                "path": {"type": "string", "description": "Asset relative path under assets/ (for action='download_asset'), e.g. 'templates/report.docx'."},
                "inputs": {"type": "object", "description": "Skill-specific inputs (for action='execute'/'run')."},
                "entry_point": {"type": "string", "description": "Bundled script to run, relative to the skill folder (e.g. 'scripts/build.sh'). Use action='load' first to see available 'scripts' (for action='run')."},
                "args": {"type": "array", "items": {"type": "string"}, "description": "CLI args passed to the entry point (for action='run')."},
                "image": {"type": "string", "description": "Override the sandbox image (for action='run'). Defaults to zhanlu-sandbox-python."},
                "timeout": {"type": "integer", "description": "Sandbox timeout in seconds (for action='run').", "default": 120},
            },
            "required": ["action"],
        },
    },
}

registry.register(
    name="skills",
    schema=SKILLS_SCHEMA,
    handler=_skills_tool,
    category="skills",
    toolset="skills",
    description="Search, load, and execute skills.",
    emoji="🎓",
    max_result_size_chars=20_000,
)

# ── P2: sandbox script execution ────────────────────────────────────────
#
# A skill's bundled ``scripts/`` (e.g. artifacts-builder's init/bundle
# shell scripts, the pptx skill's html2pptx.js) are executed inside the
# existing Docker sandbox (progressive-disclosure Layer 3). The ``run``
# action packages SKILL.md + scripts/ as base64 into the input package;
# the sandbox worker materializes them at /input/skill_bundle/ and runs
# the generic runner below, which execs the entry point with /output as
# the working directory. Reuses the existing sandbox_worker job path
# (image map, resource limits, output collector) — no new services.

_SKILL_RUNNER_SCRIPT = (
    "#!/usr/bin/env python\n"
    '"""Generic skill-script runner — execs the skill\'s bundled entry point.\n'
    "\n"
    "Written to /input/skill/sandbox_runner.py by the worker; reads the\n"
    "entry_point + args from /input/config.json and execs\n"
    "/input/skill_bundle/<entry_point> with /output as cwd.\n"
    '"""\n'
    "import json, os, subprocess, sys\n"
    "cfg = json.load(open('/input/config.json'))\n"
    "ep = cfg.get('entry_point') or ''\n"
    "args = list(cfg.get('args') or [])\n"
    "target = os.path.join('/input/skill_bundle', ep)\n"
    "if ep.endswith('.py'):\n"
    "    cmd = [sys.executable, target] + args\n"
    "elif ep.endswith('.js'):\n"
    "    cmd = ['node', target] + args\n"
    "else:\n"
    "    cmd = ['sh', target] + args\n"
    "env = dict(os.environ)\n"
    "env.update(cfg.get('env') or {})\n"
    "r = subprocess.run(cmd, cwd='/output', env=env)\n"
    "sys.exit(r.returncode)\n"
)


def _build_skill_bundle(skill_dir: str) -> list[dict]:
    """Package a skill's SKILL.md + scripts/ as base64 for the sandbox.

    Bounded: bundles ONLY ``SKILL.md`` and the top-level files in
    ``scripts/`` (never large font/asset trees), so the input package
    stays small. Each item is ``{"path", "data_base64"}`` where ``path``
    is relative to the skill folder (e.g. ``scripts/build.sh``).
    """
    base = Path(skill_dir)
    items: list[dict] = []
    skill_md = base / "SKILL.md"
    if skill_md.is_file():
        items.append({
            "path": "SKILL.md",
            "data_base64": base64.b64encode(skill_md.read_bytes()).decode(),
        })
    scripts_dir = base / "scripts"
    if scripts_dir.is_dir():
        for p in sorted(scripts_dir.iterdir()):
            if p.is_file():
                items.append({
                    "path": f"scripts/{p.name}",
                    "data_base64": base64.b64encode(p.read_bytes()).decode(),
                })
    return items


def _run_skill_script(
    db: Optional[Session],
    name: str,
    entry_point: str,
    args: Optional[list] = None,
    inputs: Optional[dict] = None,
    image: Optional[str] = None,
    timeout: int = 120,
) -> dict:
    """Enqueue a sandbox job to run a skill's bundled entry point.

    The skill must exist in the curated registry (guard: unknown skills
    are never raw-exec'd). The skill's SKILL.md + scripts/ are bundled as
    base64 into the input package; the worker materializes them at
    ``/input/skill_bundle/`` and the generic runner execs the entry point.

    Returns ``{"success", "job_id", ...}`` on success, or
    ``{"success": False, "error"}`` on validation/enqueue failure.
    """
    from app.services.skills_loader import get_skill_dir

    # Path-traversal guard: entry_point must be a relative path inside
    # the skill bundle (no leading slash, no ``..``).
    ep = (entry_point or "").strip()
    norm = os.path.normpath(ep).replace("\\", "/")
    if not ep or ep.startswith("/") or norm.startswith(".."):
        return {"success": False, "error": f"Invalid entry_point: {entry_point!r}"}

    skill_dir = get_skill_dir(name)
    if not skill_dir:
        return {"success": False, "error": f"Skill not found: {name}"}

    bundle = _build_skill_bundle(skill_dir)
    bundled_paths = {b["path"] for b in bundle}
    # entry_point must refer to a file actually present in the bundle.
    if ep not in bundled_paths:
        return {
            "success": False,
            "error": f"entry_point {entry_point!r} not found in skill {name!r} scripts",
        }

    runner_b64 = base64.b64encode(_SKILL_RUNNER_SCRIPT.encode()).decode()
    input_package = {
        "skill_config": {
            "entry_point": ep,
            "args": list(args or []),
            "inputs": inputs or {},
        },
        "skill_bundle": bundle,
        "runner_script": runner_b64,
        "runner_script_name": "sandbox_runner.py",
        "instructions": f"Run skill {name!r} entry point {ep!r} in the sandbox.",
    }
    img = image or "zhanlu-sandbox-python:latest"

    try:
        from app.services.sandbox.sandbox_service import SandboxService
        job = SandboxService(db).create_job(
            skill_name=name,
            input_package=input_package,
            image_name=img,
            timeout_seconds=timeout,
        )
        logger.info("skills run: enqueued sandbox job %s for %s/%s", job.id, name, ep)
        return {
            "success": True,
            "job_id": job.id,
            "skill": name,
            "entry_point": ep,
            "image": img,
        }
    except Exception as exc:
        logger.warning("skills run: failed to enqueue sandbox job for %s: %s", name, exc)
        return {"success": False, "error": f"Failed to enqueue sandbox job: {exc}"}


# ── folder-package helpers (references/ + assets/) ───────────────────────
#
# The Kimi-style folder anatomy stores on-demand detail in references/*.md
# and reusable templates in assets/** as real files. These helpers resolve
# those files safely (no path traversal) for the read_reference /
# list_assets / download_asset tool actions.


def _resolve_reference_path(skill_dir: str, filename: str) -> Path | None:
    """Resolve a references/*.md filename safely; None if missing/invalid."""
    base = Path(skill_dir)
    # Guard: filename must be a bare markdown name inside references/.
    if filename != Path(filename).name or not filename.endswith(".md"):
        return None
    target = (base / "references" / filename).resolve()
    refs_root = (base / "references").resolve()
    if target.parent != refs_root or not target.is_file():
        return None
    return target


def _resolve_asset_path(skill_dir: str, rel_path: str) -> Path | None:
    """Resolve an assets/** relative path safely; None if missing/invalid."""
    base = Path(skill_dir)
    assets_root = (base / "assets").resolve()
    candidate = (base / "assets" / rel_path).resolve()
    # Ensure the resolved path stays within assets/ (no traversal).
    if not str(candidate).startswith(str(assets_root) + os.sep):
        return None
    if not candidate.is_file():
        return None
    return candidate


def _list_asset_files(skill_dir: str) -> list[dict]:
    """List all files under a skill's assets/ directory with sizes."""
    base = Path(skill_dir)
    assets_dir = base / "assets"
    if not assets_dir.is_dir():
        return []
    out: list[dict] = []
    for p in sorted(assets_dir.rglob("*")):
        if p.is_file():
            out.append({
                "path": p.relative_to(assets_dir).as_posix(),
                "size": p.stat().st_size,
            })
    return out


# ── list_default_skills tool — agent introspection ──────────────────────
#
# Every agent has access to the built-in default artifact-format skills
# (docx, pptx, pdf, html, dashboard) via the _DEFAULT_SKILLS_BLOCK in the
# system prompt. The LLM can call this tool to get the full list with
# trigger words and formats for self-introspection / user education.
# This complements the passive injection — it gives the agent an active
# way to discover what defaults are available.


async def _list_default_skills_tool(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    """Return the list of built-in default skills always available."""
    try:
        from app.services.synexia.default_skills import get_default_skills_list
        skills_list = get_default_skills_list()
        return {"success": True, "default_skills": skills_list, "count": len(skills_list)}
    except Exception as exc:
        return {"success": False, "error": f"Could not list default skills: {exc}"}


LIST_DEFAULT_SKILLS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_default_skills",
        "description": (
            "List the built-in default skills that are always available. "
            "Returns the skill name, trigger words, and output format for "
            "each of the default artifact skills: docx, pptx, pdf, html, "
            "and dashboard."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
}

registry.register(
    name="list_default_skills",
    schema=LIST_DEFAULT_SKILLS_SCHEMA,
    handler=_list_default_skills_tool,
    category="skills",
    toolset="skills",
    description="List built-in default skills always available to every agent.",
    emoji="🔧",
    enabled_by_default=False,  # Not auto-added to user agents; LLM calls it on demand
)
