"""run_sandbox_skill — generate file artifacts from data via Docker sandbox.

This tool bridges the chat agent to the existing sandbox execution pipeline.
When an agent has real data rows (from ask_data_agent) and needs to produce
a downloadable file (xlsx, pptx, html dashboard, pdf), it calls this tool.

The handler:
1. Creates an Artifact + ArtifactVersion (draft → building)
2. Builds an input package (data snapshots + instructions + format)
3. Writes the sandbox_runner.py script into the input package
4. Calls SandboxService.create_job() which enqueues to Redis
5. Polls job status every 1s until completed/failed/timeout (max 120s)
6. Returns artifact_id + preview/download URLs to the LLM

Architecture invariants respected:
- SBX-1: Only sandbox-worker creates containers. This handler only creates job records.
- SBX-4: Sandbox receives data as JSON snapshots, never DB credentials.
- SBX-11: Outputs stored as PostgreSQL-backed artifact versions.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.services.tool_registry import registry

# Import is deferred to first use to avoid circular imports during app boot.
# The auto-analysis module is pure-Python / pandas-only — safe to import
# from a tool handler running in the API process.

logger = logging.getLogger(__name__)

# Proactive post-generation edit suggestion.  Appended to the success message
# of the ``pptx`` branch only when (a) the deck-edit routing flag is on and
# (b) a ``deck_plan`` was persisted into ``source_json`` — i.e. the artifact is
# actually editable by the six ``deck_edit`` tools.  Bilingual so the agent can
# surface it in the user's language.
_DECK_EDIT_SUGGESTION = (
    "You can now ask me to refine this deck: add/remove slides, edit a slide's "
    "title or bullets, change the chart, switch the theme, or reorder slides. "
    "（您还可以让我继续编辑这份 PPT：添加/删除页面、修改标题或要点、调整图表、"
    "更换主题或调整页面顺序。）"
)


def _deck_edit_hint(fmt: str, version_source_json: Optional[dict]) -> str:
    """Return the proactive edit hint when the deck is editable, else ''."""
    if fmt != "pptx":
        return ""
    if not version_source_json or "deck_plan" not in version_source_json:
        return ""
    try:
        from app.config import settings
        if not getattr(settings, "DECK_EDIT_ROUTING_ENABLED", False):
            return ""
    except Exception:  # pragma: no cover - settings import is safe
        return ""
    return " " + _DECK_EDIT_SUGGESTION


# Path to the generic sandbox runner script — gets mounted into the container
_RUNNER_SCRIPT_PATH = Path(__file__).parent.parent / "sandbox" / "sandbox_runner.py"

# Source content of the C-Heavy skill-driven runner.  We read it once
# at import time and re-embed it (base64) into every skill-driven job
# rather than bind-mounting the file.  This keeps the sandbox image
# stateless — the same image can be used for skill-driven and
# deterministic jobs interchangeably.
_SKILL_DRIVEN_RUNNER_PATH = Path(__file__).parent.parent / "sandbox" / "skill_driven_runner.py"
try:
    _SKILL_DRIVEN_RUNNER_SCRIPT = _SKILL_DRIVEN_RUNNER_PATH.read_text(encoding="utf-8")
except OSError:
    # Skill-driven path unavailable — every job will silently fall back
    # to the deterministic runner.
    _SKILL_DRIVEN_RUNNER_SCRIPT = ""

# Image mapping by output format.
#
# Deterministic office formats use the existing office/pptx images. The
# separate ``zhanlu-sandbox-skill`` image is only for the optional slow
# skill-driven runner, currently disabled by _SKILL_DRIVEN_FORMATS.
_SKILL_DRIVEN_IMAGE = "zhanlu-sandbox-pptx:latest"  # FIX 2026-08-23: use pptx image (has Node.js + Playwright for html2pptx)
_IMAGE_BY_FORMAT = {
    "xlsx": "zhanlu-sandbox-office:latest",
    "pptx": "zhanlu-sandbox-pptx:latest",
    "html": "zhanlu-sandbox-python:latest",
    "pdf":  "zhanlu-sandbox-office:latest",
    "docx": "zhanlu-sandbox-office:latest",
    "md":   "zhanlu-sandbox-python:latest",
}

# Formats where the C-Heavy skill-driven path is the primary generator.
# Disabled in production for now: the LLM-equipped runner can exceed the
# sandbox timeout for ordinary weekly reports. The deterministic runner still
# receives synthesized summary/methodology/KPIs/recommendations and produces
# reliable DOCX/PPTX/PDF/XLSX artifacts.
_SKILL_DRIVEN_FORMATS = frozenset({"pptx"})  # FIX 2026-08-23: enable skill-driven PPTX for professional quality


def _format_supports_skill_driven(fmt: str) -> bool:
    """True if the format should go through the skill-driven path."""
    return fmt in _SKILL_DRIVEN_FORMATS


# Timeout (seconds) for skill-driven jobs.  Each job makes up to
# 3 LLM calls + Node.js code execution, so the default 120s is too
# tight.  Loaded lazily so a config override at import time is honored.
def _skill_driven_timeout() -> int:
    from app.config import settings
    return int(getattr(settings, "SANDBOX_SKILL_TIMEOUT_SECONDS", 240))


# Per-file size cap for skill bundle entries.  A 100 KB markdown file
# is already huge for an LLM prompt; anything bigger almost certainly
# is a font/asset tree we don't want to ship into the sandbox.
_SKILL_BUNDLE_MAX_FILE_BYTES = 100 * 1024


def _build_skill_bundle(format_key: str) -> list[dict]:
    """Bundle the relevant document-skill files for a given format.

    Returns a list of ``{"path": "<rel-path>", "data_base64": "..."}``
    entries ready to drop into ``input_package["skill_bundle"]``.  The
    worker materializes these into ``/input/skill_bundle/`` inside the
    container; the skill_driven_runner reads SKILL.md and the
    format-specific companion doc (docx-js.md, html2pptx.md, …) from
    there.

    Path-traversal guard: every file must live under
    ``backend/skills/document-skills/<format_key>/``.  Anything outside
    that directory is silently skipped.
    """
    from app.config import settings

    # Try BASE_DIR if exposed; otherwise fall back to the canonical
    # location relative to this file (backend/skills/document-skills).
    # The file lives at backend/app/services/tool_handlers/sandbox_tool.py
    # so the repo root is 4 parents up: tool_handlers → services → app → backend
    candidates = []
    if hasattr(settings, "BASE_DIR") and settings.BASE_DIR:
        candidates.append(Path(settings.BASE_DIR) / "skills" / "document-skills")
        candidates.append(Path(settings.BASE_DIR).parent / "skills" / "document-skills")
    candidates.append(Path(__file__).parent.parent.parent.parent / "skills" / "document-skills")

    skills_root = None
    for c in candidates:
        if c.exists():
            skills_root = c
            break
    if skills_root is None:
        logger.warning(
            "Skill bundle dir not found; tried: %s",
            [str(c) for c in candidates],
        )
        return []
    fmt_dir = skills_root / format_key
    if not fmt_dir.exists():
        logger.warning("Skill bundle dir not found: %s", fmt_dir)
        return []

    # Curated list of files to bundle per format.  We deliberately skip
    # ``scripts/`` Python files (the LLM writes its own code) and any
    # binary assets (LICENSE.txt is included for attribution but is
    # only a few hundred bytes).
    wanted: list[str] = []
    wanted.append("SKILL.md")
    if format_key == "docx":
        wanted.extend(["docx-js.md", "ooxml.md"])
    elif format_key == "pptx":
        wanted.extend(["html2pptx.md", "ooxml.md"])
    elif format_key == "xlsx":
        wanted.append("xlsx.md")
    elif format_key == "pdf":
        wanted.extend(["pdf.md" if (fmt_dir / "pdf.md").exists() else "reference.md", "forms.md"])

    bundle: list[dict] = []
    fmt_dir_resolved = fmt_dir.resolve()
    for rel_name in wanted:
        # Defence-in-depth: refuse anything that escapes the format dir
        candidate = (fmt_dir / rel_name).resolve()
        if fmt_dir_resolved not in candidate.parents and candidate != fmt_dir_resolved:
            logger.warning("Refusing to bundle escaped path: %s", candidate)
            continue
        if not candidate.exists() or not candidate.is_file():
            logger.info("Skill bundle file not present, skipping: %s", rel_name)
            continue
        size = candidate.stat().st_size
        if size > _SKILL_BUNDLE_MAX_FILE_BYTES:
            logger.warning("Skill bundle file too large (%d bytes), skipping: %s", size, rel_name)
            continue
        try:
            content = candidate.read_bytes()
        except OSError as e:
            logger.warning("Could not read skill bundle file %s: %s", candidate, e)
            continue
        bundle.append({
            "path": rel_name,
            "data_base64": base64.b64encode(content).decode("ascii"),
            "size_bytes": size,
        })
    logger.info(
        "Built skill bundle for format=%s: %d files, %d bytes total",
        format_key, len(bundle), sum(b["size_bytes"] for b in bundle),
    )
    return bundle


def _build_runner_modules() -> dict[str, str]:
    """Bundle the Python files the skill_driven_runner needs as imports.

    Returns a dict of ``{filename: base64_content}`` for the worker to
    write to ``/input/skill/`` alongside the main runner script.  We
    inline the contents rather than mounting a directory because the
    sandbox worker runs without a skills/ mount.

    Path-traversal / extension guard is enforced inside
    ``container_manager.write_runner_modules``.
    """
    sandbox_dir = Path(__file__).parent.parent / "sandbox"
    sidebar_charts = Path(__file__).parent.parent / "artifacts" / "charts"
    exporters_dir = Path(__file__).parent.parent / "artifacts" / "exporters"
    targets = {
        "skill_driven_runner.py": sandbox_dir / "skill_driven_runner.py",
        "llm_client.py":          sandbox_dir / "llm_client.py",
        "fallback_generator.py":  sandbox_dir / "fallback_generator.py",
        # Phase 1B: the deterministic sandbox_runner.generate_pptx now renders
        # through the shared layout_engine (single source of truth with the
        # in-process exporter).  Vendor both files so the sandbox container can
        # `import layout_engine` / `import branded_charts` from /input/skill/.
        "layout_engine.py":       sandbox_dir.parent / "artifacts" / "layout_engine.py",
        "branded_charts.py":      sidebar_charts / "branded_charts.py",
        # Sandbox-portability (2026-08-19): layout_engine.py now imports
        # ``cap_chart_categories`` from this pure-Python module (no app.*
        # dependencies) instead of ``_common.py`` (which carries the heavy
        # ``from app.services.synexia.contracts import …`` at module top).
        # Vendor it alongside layout_engine.py so the sandbox container can
        # resolve ``from _chart_helpers import cap_chart_categories`` without
        # ``No module named 'app'``.
        "_chart_helpers.py":      exporters_dir / "_chart_helpers.py",
    }
    out: dict[str, str] = {}
    for filename, path in targets.items():
        if not path.exists():
            logger.error("Skill-driven runner module missing on host: %s", path)
            continue
        try:
            content = path.read_bytes()
        except OSError as e:
            logger.error("Could not read runner module %s: %s", path, e)
            continue
        out[filename] = base64.b64encode(content).decode("ascii")
    return out

# Artifact type mapping
_ARTIFACT_TYPE_BY_FORMAT = {
    "xlsx": "xlsx",
    "pptx": "pptx",
    "html": "html",
    "pdf":  "pdf",
    "docx": "docx",
    "md":   "md",
}

# MIME type mapping
_MIME_BY_FORMAT = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "html": "text/html",
    "pdf":  "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "md":   "text/markdown",
}

# File extension mapping
_EXT_BY_FORMAT = {
    "xlsx": "xlsx",
    "pptx": "pptx",
    "html": "html",
    "pdf":  "pdf",
    "docx": "docx",
    "md":   "md",
}

# Max poll time for the tool handler (seconds)
_MAX_POLL_SECONDS = 120
_POLL_INTERVAL_SECONDS = 1.0


# ---------------------------------------------------------------------------
# Metric/value row enrichment
# ---------------------------------------------------------------------------
# When the agent hands us a flat list of ``{metric: <name>, value: <text>}``
# rows (or similar 2-column "label: value" rows) rather than a tabular row
# set, auto_analyze() cannot derive narrative findings/recommendations/
# sections because there are no dimensions to slice across.  These helpers
# detect that shape and build the rich payload directly: each metric
# becomes a KPI, and rows sharing a common prefix (``Customer N``,
# ``Product N``, etc.) become a section.

import re as _re_meta
import sys as _sys_meta


_META_PREFIX_RE = _re_meta.compile(
    r"^(customer|product|top\s+(?:customer|product|region|supplier))\b.*",
    _re_meta.IGNORECASE,
)


def _is_meta_style_rows(data) -> bool:
    """Return True when ``data`` is a flat list of single-metric/value
    rows rather than a tabular row set with multiple numeric dimensions.

    A "meta-style" row looks like
    ``{report_level: 'TOTAL', metric: 'Revenue', value: '¥55M', detail: '...'}``
    where each row represents ONE named indicator.  The key heuristic:
    the row has a string-typed "label" column (``metric``/``name``/``key``)
    AND a single ``value``-ish column, AND most ``value`` cells are
    strings (currency, percent, dates, IDs — not raw numbers in
    multiple dimensions).

    A "tabular" row has multiple numeric dimensions per row (e.g.
    ``{month: '2026-07', revenue: 348M, volume: 50k, orders: 196}``)
    where auto_analyze can derive slices.  In that shape we leave it
    alone.
    """
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return False
    first = data[0]
    if not isinstance(first, dict) or len(first) < 2 or len(first) > 4:
        return False

    # Find a label-like key (str) and value-like key (str or scalar).
    label_keys = ["metric", "name", "key", "indicator", "label", "title"]
    value_keys = ["value", "v", "val", "amount", "metric_value"]
    label_key = next((k for k in label_keys if k in first), None)
    value_key = next((k for k in value_keys if k in first and k != label_key), None)
    if not label_key:
        # Fallback: pick first string-valued key as label
        for k in first:
            if isinstance(k, str) and isinstance(first.get(k), str):
                label_key = k
                break
    if not value_key:
        # Fallback: pick first non-label key as value
        for k in first:
            if k != label_key:
                value_key = k
                break
    if not (label_key and value_key):
        return False

    # Now verify the values look "metric-y" (mostly strings, not all numeric
    # columns side-by-side).  We accept up to 4 cols and the rest are
    # treated as level/detail (kept as plain text alongside the value).
    metaish = 0
    for r in data[:10]:
        if not isinstance(r, dict):
            return False
        if label_key not in r or value_key not in r:
            return False
        v = r.get(value_key)
        if isinstance(v, str):
            metaish += 1  # value as string (currency/%/date) -> meta
    return metaish >= max(3, len(data[:10]) * 0.5)


def _metric_key_value(rows) -> tuple[str | None, str | None]:
    """Return (metric_key, value_key) for the first row, with priority
    for the conventional names (``metric``/``name``/``key`` and
    ``value``/``amount``)."""
    if not rows or not isinstance(rows[0], dict):
        return None, None
    first = rows[0]
    label_keys = ["metric", "name", "key", "indicator", "label", "title"]
    value_keys = ["value", "v", "val", "amount", "metric_value"]
    metric_key = next((k for k in label_keys if k in first), None)
    value_key = next((k for k in value_keys if k in first and k != metric_key), None)
    if not metric_key:
        # Fallback: first str-valued key
        for k in first:
            if isinstance(k, str) and isinstance(first.get(k), str):
                metric_key = k
                break
    if not value_key:
        # Fallback: first non-label key
        for k in first:
            if k != metric_key:
                value_key = k
                break
    return metric_key, value_key


def _kpis_from_metric_value_rows(rows) -> list[dict]:
    """Convert each meta-row into a KPI dict, capped to 8.  Skips
    rows where the metric is a sub-row label (``Customer 1``,
    ``Product 2``) — those belong in sections, not the KPI bar."""
    metric_key, value_key = _metric_key_value(rows)
    if not metric_key or not value_key:
        return []
    out: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        m = str(r.get(metric_key, "")).strip()
        v = r.get(value_key, "")
        if not m or v in (None, "", []):
            continue
        # Skip enumerations (Customer N / Product N) — they're
        # top-N rows, not KPIs.
        if _re_meta.match(r"^(customer|product|region|supplier|item)\s+\d+\b", m, _re_meta.IGNORECASE):
            continue
        if len(out) >= 8:
            break
        if isinstance(v, (int, float)):
            disp = f"{v:,.2f}" if isinstance(v, float) else f"{v:,}"
        else:
            disp = str(v)
        out.append({"label": m[:60], "value": disp})
    return out


def _normalize_metric_for_group(metric: str) -> tuple[str, str]:
    """(group, title) — 'Customer 1', 'Customer 2' -> ('customer', 'Customer 1')
    'Top Customer' -> ('customer', 'Top Customer').
    Unknown -> ('', metric).
    """
    if not metric:
        return "", metric or ""
    m = str(metric).strip()
    low = m.lower()
    if low.startswith("customer"):
        return "customer", m
    if low.startswith("product"):
        return "product", m
    if low.startswith("top customer"):
        return "customer", m
    if low.startswith("top product"):
        return "product", m
    if low.startswith("region") or low.startswith("top region"):
        return "region", m
    return "", m


def _sections_from_metric_value_rows(rows) -> list[dict] | None:
    """Group metric/value rows into sections, by either the dedicated
    ``report_level`` column or by the metric-name prefix.

    Returns a list of section dicts ready for ``SectionSpec``:

    .. code-block:: python

        {"title": "Total Performance", "bullets": [...], "type": "bullets"}

    or ``None`` when the rows are not in meta-style.
    """
    if not _is_meta_style_rows(rows):
        return None
    label_key, value_key = _metric_key_value(rows)
    if not label_key or not value_key:
        return None

    # An optional "level" key — group rows by its value (e.g.
    # report_level / category / section).  When present, this is the
    # primary grouping.
    level_key = None
    if rows and isinstance(rows[0], dict):
        for k in ("report_level", "category", "section", "group"):
            if k in rows[0] and k != label_key and k != value_key:
                level_key = k
                break

    level_groups: dict[str, list[tuple[str, str]]] = {}
    prefix_groups: dict[str, list[tuple[str, str]]] = {}
    standalone: list[tuple[str, str]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        m = str(r.get(label_key, "")).strip()
        v = r.get(value_key, "")
        v_str = str(v).strip() if v not in (None, "") else ""

        if level_key:
            lvl = str(r.get(level_key, "")).strip() or "Summary"
            level_groups.setdefault(lvl, []).append((m, v_str))
            continue

        group, _title = _normalize_metric_for_group(m)
        if group:
            prefix_groups.setdefault(group, []).append((m, v_str))
        else:
            standalone.append((m, v_str))

    sections: list[dict] = []

    if level_groups:
        # Title mapping for known level keys
        title_map = {
            "TOTAL": "Overall Performance",
            "COMPARISON": "Month-over-Month Comparison",
            "CUSTOMER": "Customer Analysis",
            "CUSTOMERS": "Customer Analysis",
            "PRODUCT": "Product Analysis",
            "PRODUCTS": "Product Analysis",
            "REGION": "Regional Analysis",
            "REGIONS": "Regional Analysis",
        }
        # Order: priority groups first
        order = ["TOTAL", "COMPARISON", "CUSTOMER", "PRODUCT", "REGION"]
        level_keys_sorted = sorted(
            level_groups.keys(),
            key=lambda k: (order.index(k.upper()) if k.upper() in order else 99, k),
        )
        for lvl in level_keys_sorted:
            items = level_groups[lvl]
            sec_title = title_map.get(lvl.upper(), lvl.title() + " Breakdown")
            bullets = []
            for m, v in items[:20]:
                bullets.append(f"{m}: {v}" if v else m)
            if bullets:
                sections.append({
                    "title": sec_title,
                    "bullets": bullets,
                    "type": "bullets",
                })
    else:
        # Fall back to prefix-based grouping (Customer N, Product N)
        ordered_groups = sorted(prefix_groups.items(), key=lambda kv: (
            {"customer": 0, "product": 1, "region": 2}.get(kv[0], 9), kv[0]
        ))
        for group_name, items in ordered_groups:
            title_map = {
                "customer": "Customer Analysis",
                "product": "Product Analysis",
                "region": "Regional Analysis",
            }
            sec_title = title_map.get(group_name, group_name.title() + " Breakdown")
            bullets = []
            for m, v in items[:20]:
                bullets.append(f"{m}: {v}" if v else m)
            if bullets:
                sections.append({
                    "title": sec_title,
                    "bullets": bullets,
                    "type": "bullets",
                })

        # If still no section, build from standalone
        if not sections and standalone:
            bullets = [f"{m}: {v}" if v else m for m, v in standalone[:12]]
            sections.append({
                "title": "Highlights",
                "bullets": bullets,
                "type": "bullets",
            })

    return sections or None


def _summary_from_metric_value_rows(rows, *, title_hint: str = "") -> str:
    """Build a short summary that names the dataset + the title."""
    metric_key, _value_key = _metric_key_value(rows)
    n = len(rows)
    prefix = f"{title_hint}: " if title_hint else "This report covers "
    tail = (
        f"the top {min(n, 10)} key indicators below, "
        f"followed by {len(rows)} supporting metric{'s' if len(rows) != 1 else ''} "
        f"covering customer, product, and overall performance."
    )
    return f"{prefix}{tail}"



def _enrich_args_from_sibling_html_report(
    *,
    args: dict,
    db: Session,
    conversation_id: str,
    title: str,
) -> dict:
    """Fill in Claude-style content fields on ``args`` from a sibling
    ``html_report`` artifact in the same conversation.

    When the agent calls ``finalize_into_artifact`` first (which
    produces a rich HTML report card) and then ``run_sandbox_skill``
    to convert that report into a downloadable file, the rich
    ``ReportCardPayload`` (summary / methodology / kpis / insights /
    recommendations / sql / next_step) is stored on the
    ``html_report`` artifact's ``source_json.rcp`` field.  This
    helper picks the most recent title-matching ``html_report`` in
    the same conversation and copies any blank field on ``args``
    from it so the docx/pptx/xlsx renders the same rich content as
    the HTML report the user just saw.
    """
    try:
        from app.models.artifact import Artifact, ArtifactVersion
        from sqlalchemy import desc

        # The "rich" sibling could be one of:
        #   - artifact_type='html_report'  — produced by
        #     ``finalize_into_artifact`` and carries the full rcp in
        #     ``artifact.metadata_json["report_card_payload"]``.
        #   - artifact_type='html'         — the sidecar produced by
        #     the sandbox's ``generate_html`` for a previous
        #     ``run_sandbox_skill(format='html', ...)`` call.  This
        #     sidecar is also rich (KPI cards, charts, etc.) but the
        #     rcp lives in ``version.source_json.rcp``.
        siblings = (
            db.query(Artifact)
            .filter(
                Artifact.conversation_id == conversation_id,
                Artifact.artifact_type.in_(("html_report", "html")),
                Artifact.is_deleted == False,
            )
            .order_by(desc(Artifact.created_date))
            .all()
        )
        if not siblings:
            return args

        def _norm(t: str) -> str:
            import re as _re
            t = (t or "").strip().rstrip(" (preview)").strip()
            t = _re.sub(r"\.(docx|pptx|xlsx|pdf|md|html|htm)$", "", t, flags=_re.IGNORECASE)
            return _re.sub(r"\s+", " ", t.replace("_", " ")).lower().strip()

        a = _norm(title)

        def _matches(b: str) -> bool:
            if not a or not b:
                return False
            if a == b:
                return True
            if a.startswith(b) or b.startswith(a):
                return True
            aw, bw = a.split(), b.split()
            for n in (3, 2):
                if len(aw) >= n and len(bw) >= n and aw[:n] == bw[:n]:
                    return True
            sw = {w for w in aw if len(w) >= 3}
            shared = sum(1 for w in bw if w in sw)
            if shared >= 2:
                return True
            return False

        def _extract_rcp(art: Artifact) -> dict | None:
            """Pull the rcp out of an artifact, returning a non-empty
            dict or None.  An empty rcp (everything blank) is treated
            as missing so we don't accidentally pick a sparse sidecar
            that just happens to match the title."""
            md = art.metadata_json or {}
            rcp = None
            if isinstance(md, dict):
                rcp = md.get("report_card_payload") or md.get("rcp")
            if not isinstance(rcp, dict) or not rcp:
                version = (
                    db.query(ArtifactVersion)
                    .filter(ArtifactVersion.id == art.current_version_id)
                    .first()
                )
                if version is not None:
                    src = version.source_json or {}
                    if isinstance(src, dict):
                        rcp = src.get("rcp") or src
            if not isinstance(rcp, dict):
                return None
            # Treat an effectively-empty rcp (no summary, no kpis, no
            # insights, no key_findings, no recommendations) as missing.
            if not any(rcp.get(k) for k in (
                "summary", "methodology", "kpis", "insights",
                "key_findings", "recommendations", "sections",
                "next_step", "sql",
            )):
                return None
            return rcp

        # Prefer a title-matched sibling that has a real rcp.
        sibling = next(
            (s for s in siblings if _matches(_norm(s.title)) and _extract_rcp(s)),
            None,
        )
        # Fall back to the most-recent sibling with a real rcp.
        if sibling is None:
            sibling = next((s for s in siblings if _extract_rcp(s)), None)
        if sibling is None:
            return args

        rcp = _extract_rcp(sibling)
        if not isinstance(rcp, dict) or not rcp:
            return args

        # Only fill fields the agent left blank so we never clobber a
        # more-specific instruction.
        filled = []
        for k in (
            "summary",
            "methodology",
            "source",
            "sql",
            "next_step",
            "kpis",
            "insights",
            "key_findings",
            "recommendations",
            "sections",
        ):
            if not args.get(k) and rcp.get(k):
                args[k] = rcp[k]
                filled.append(k)
        if filled:
            logger.info(
                "Enriched sandbox args from sibling %s (title=%r, type=%s): filled %s",
                sibling.id, sibling.title, sibling.artifact_type, filled,
            )
        return args
    except Exception as enrich_err:
        # Enrichment is best-effort; never fail the file generation
        # just because we couldn't find a sibling.
        logger.warning(
            "Could not enrich sandbox args from sibling html_report: %s",
            enrich_err,
        )
        return args


def _enrich_args_from_data_auto_analysis(args: dict, *, fmt: str) -> dict:
    """Fill blank rich fields (summary/kpis/findings/etc.) from raw rows.

    The sibling-HTML-report enrichment only fires when an earlier
    ``finalize_into_artifact`` already produced a rich ``html_report``
    in the same conversation.  When the agent goes straight from
    ``ask_data_agent`` to ``run_sandbox_skill(format="docx")`` — the
    common pattern after a small data lookup — there is no sibling, and
    the deterministic ``generate_docx`` renderer emits a bare cover +
    "Instructions" heading + raw data table.  That is the exact
    complaint this function exists to fix.

    We delegate to the backend's ``_report_auto_analysis.auto_analyze``
    (the same safety net used by the in-process ``create_artifact`` /
    ``_payload_from_execution`` path) which deterministically derives a
    full ReportCard-shaped payload — Executive Summary, KPI grid, Key
    Findings, Recommendations, top-N breakdown sections, an aggregated
    chart, and methodology — from the rows + columns the agent passed.

    Only blank fields are filled.  LLM / orchestrator values still win.
    Returns the (possibly mutated) ``args`` dict.
    """
    # Only do work when the agent actually handed us rows.
    data = args.get("data")
    if not isinstance(data, list) or not data:
        return args

    # Skip when the config already carries rich content — don't override
    # the LLM / orchestrator's authoring.  We only fire when ALL of the
    # primary rich fields are blank.
    rich_keys = ("summary", "kpis", "key_findings", "recommendations", "sections")
    has_any = any(args.get(k) for k in rich_keys)
    if has_any:
        return args

    # Derive columns from the first row when the agent didn't pass an
    # explicit column list.  ``auto_analyze`` accepts either, but a flat
    # list of dicts is the most common shape from ``ask_data_agent``.
    columns = args.get("columns")
    if not columns and isinstance(data[0], dict):
        # Union of keys preserves order of first-seen — keeps time/date
        # columns ahead of derived numeric columns, which reads better in
        # the auto-generated KPI grid.
        seen: list[str] = []
        for row in data[:50]:
            if not isinstance(row, dict):
                continue
            for k in row.keys():
                if k not in seen:
                    seen.append(str(k))
        columns = seen
    if not columns:
        return args

    # Special case: the data is a flat list of metric/value pairs
    # ("label" → single value) rather than a tabular row set.  In this
    # shape auto_analyze() cannot derive narrative findings/recommendations
    # because there are no dimensions to slice across.  Build them
    # directly: each metric becomes a KPI, rows sharing a prefix
    # ("Customer N", "Product N") become a section.
    metric_sections = _sections_from_metric_value_rows(data)
    if metric_sections is not None:
        # Only fill in when the meta-style path produced sections AND
        # the agent didn't already provide them (we already returned if
        # any rich key was set, above).
        args["sections"] = metric_sections
        # meta-style data also benefits from KPI rows as kpis (when
        # the values look like clean numbers / currency).
        kpis = _kpis_from_metric_value_rows(data)
        if kpis:
            args["kpis"] = kpis
        # Synthesize a summary that names the dataset + a few highlights.
        args["summary"] = _summary_from_metric_value_rows(
            data,
            title_hint=(args.get("title") or "").strip(),
        )
        print(
            f"[SBX META] built {len(metric_sections)} sections, "
            f"{len(kpis)} kpis from {len(data)} metric/value rows",
            file=sys.stderr, flush=True,
        )
        filled.append("sections")
        filled.append("kpis")
        filled.append("summary")
        return args

    try:
        # Lazy import — keeps app startup fast and avoids pulling pandas
        # at import time.
        from app.services.tool_handlers._report_auto_analysis import auto_analyze

        title_hint = (args.get("title") or "").strip() or None
        enriched = auto_analyze(
            rows=data,
            columns=columns,
            tool_name=f"run_sandbox_skill/{fmt}",
            title_hint=title_hint,
        )
    except Exception as exc:
        logger.warning(
            "auto_analyze enrichment in run_sandbox_skill failed: %s; "
            "falling back to raw data.",
            exc,
        )
        return args

    if not enriched:
        return args

    # Merge — ``auto_analyze`` only returns the fields it derived, so
    # existing keys in ``args`` (instructions, data, title, user_message,
    # source, etc.) are preserved verbatim.
    filled = []
    for key, value in enriched.items():
        if not args.get(key) and value not in (None, "", [], {}):
            args[key] = value
            filled.append(key)

    if filled:
        logger.info(
            "run_sandbox_skill(%s) auto-analyzed %d rows → filled %s",
            fmt, len(data), filled,
        )
    return args


def run_sandbox_skill_sync(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    """Synchronous public entry point for the sandbox skill.

    Wraps the existing async ``_run_sandbox_skill`` so synchronous
    callers (Synexia FSM ``finalize_into_artifact``, FastAPI routes
    that aren't ``async``, etc.) can trigger the same pipeline
    without bridging an event loop.

    Behavior is identical to the tool path: validates input, creates
    Artifact + Version, enqueues the sandbox job to Redis, polls
    until completion, and returns the artifact info.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # We're inside an async context — schedule and wait via
            # ``asyncio.run_coroutine_threadsafe`` semantics.  In
            # practice the FSM finalize path runs from a sync
            # FastAPI route, so this branch is rarely taken; we
            # fall through to the ``run_until_complete`` path below
            # for that case.
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    asyncio.run,
                    _run_sandbox_skill(args, db, user_id, context),
                )
                return future.result(timeout=max(_MAX_POLL_SECONDS, _skill_driven_timeout()) + 30)
        return loop.run_until_complete(
            _run_sandbox_skill(args, db, user_id, context)
        )
    except RuntimeError:
        # No event loop in the current thread — run one ourselves.
        return asyncio.run(_run_sandbox_skill(args, db, user_id, context))


async def _run_sandbox_skill(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    """Generate a file artifact from data via the Docker sandbox.

    Flow:
        1. Validate inputs
        2. Create Artifact + ArtifactVersion
        3. Build input package (data snapshots + runner script + instructions)
        4. Create SandboxJob (enqueued to Redis)
        5. Poll job status until complete
        6. Return artifact info to LLM
    """
    fmt = (args.get("format") or "").lower().strip()
    # "dashboard" is a user-facing alias: it renders as an interactive
    # HTML artifact (KPI cards + chart + table), so map it to ``html``.
    if fmt == "dashboard":
        fmt = "html"
    if fmt not in _IMAGE_BY_FORMAT:
        return {
            "success": False,
            "error": f"Invalid format '{fmt}'. Must be one of: {list(_IMAGE_BY_FORMAT.keys())}",
        }

    data = args.get("data")
    if not data or not isinstance(data, list):
        return {"success": False, "error": "data must be a non-empty array of objects"}

    title = (args.get("title") or "Generated Report").strip()
    instructions = (args.get("instructions") or "").strip()
    conversation_id = (context or {}).get("conversation_id")
    agent_app_id = (context or {}).get("agent_app_id")

    # --- 0. Enrich sparse args from a sibling rich HTML report ---
    # The agent often calls run_sandbox_skill right after
    # ``finalize_into_artifact`` to convert the rich HTML report
    # into a downloadable file.  In that case the rich HTML report
    # already carries the Executive Summary / Methodology / KPIs /
    # Insights / Recommendations / SQL / Next Step fields, but the
    # sandbox call's args are usually just ``{data, title,
    # instructions}``.  Without this enrichment the resulting
    # docx/pptx/xlsx is just a title + bare data table, which is
    # exactly the "docx is empty" complaint the user reported.
    # We look up a recent ``html_report`` artifact in the same
    # conversation whose title matches, and merge its
    # ``source_json.rcp`` into ``args`` for any field the agent
    # left blank.
    if conversation_id and fmt in ("docx", "pptx", "xlsx"):
        args = _enrich_args_from_sibling_html_report(
            args=args,
            db=db,
            conversation_id=conversation_id,
            title=title,
        )

    # --- 0b. Enrich sparse args from the raw DATA itself ---
    # When the agent passes only ``{data, title, instructions}`` and no
    # sibling HTML report exists (e.g. direct ``run_sandbox_skill`` call
    # after ``ask_data_agent``), the deterministic ``generate_docx``
    # renderer still produces a bare "Instructions + data table" because
    # none of the rich fields (summary / kpis / key_findings /
    # recommendations / sections) are populated.
    #
    # The backend's ``_report_auto_analysis.auto_analyze`` derives all
    # of those fields deterministically from the rows + columns — it is
    # the same safety-net that the in-process ``create_artifact`` path
    # uses.  We invoke it here so docx (and pptx/xlsx) produced via the
    # sandbox also get a proper Executive Summary, KPI grid, Key
    # Findings and Recommendations instead of the empty cover page.
    #
    # Only fires when the config is genuinely sparse — if the LLM /
    # orchestrator already supplied rich fields, we keep them.  This
    # matches the "LLM values win, auto-fill fills blanks" contract
    # used by ``_payload_from_execution``.
    if fmt in ("docx", "pptx", "xlsx") and isinstance(args.get("data"), list):
        import sys
        print(f"[DEBUG sandbox_tool] enriching fmt={fmt} data_len={len(args.get('data',[]))} rich_present={any(args.get(k) for k in ('summary','kpis','key_findings','recommendations','sections'))}", file=sys.stderr, flush=True)
        args = _enrich_args_from_data_auto_analysis(args=args, fmt=fmt)
        print(f"[DEBUG sandbox_tool] AFTER enrich: has_summary={bool(args.get('summary'))} has_kpis={bool(args.get('kpis'))} has_findings={bool(args.get('key_findings'))}", file=sys.stderr, flush=True)

    # --- 1. Create Artifact + Version ---
    from app.services.artifacts.artifact_service import ArtifactService
    artifact_service = ArtifactService(db)

    artifact_type = _ARTIFACT_TYPE_BY_FORMAT[fmt]
    artifact = artifact_service.create_artifact(
        artifact_type=artifact_type,
        title=title,
        conversation_id=conversation_id,
        created_by_agent_id=agent_app_id,
        description=f"Generated from {len(data)} data rows",
    )

    # Phase 1B: persist the DeckPlan into source_json so PHASE 2 edit tools can
    # round-trip through DeckPlan.model_validate.  The orchestrator ships the
    # plan dict in args["deck_plan"]; we store a stable JSON snapshot.
    version_source_json = None
    if fmt == "pptx":
        deck_plan = args.get("deck_plan")
        if isinstance(deck_plan, dict):
            version_source_json = {"deck_plan": deck_plan}
        elif hasattr(deck_plan, "model_dump"):
            version_source_json = {"deck_plan": deck_plan.model_dump(mode="json")}

    version = artifact_service.create_version(
        artifact_id=artifact.id,
        changelog=f"Initial generation ({fmt.upper()})",
        source_json=version_source_json,
        produced_by_skill="sandbox_runner",
    )

    # --- 2. Read the runner script ---
    try:
        runner_script = _RUNNER_SCRIPT_PATH.read_text(encoding="utf-8")
    except Exception as e:
        logger.error("Failed to read sandbox_runner.py: %s", e)
        return {"success": False, "error": f"Runner script not found: {e}"}

    # --- 3. Build input package ---
    # Claude-style content fields are forwarded to the sandbox
    # generators (generate_docx / generate_pptx) so they can render
    # the full Executive Summary / Methodology / Key Findings /
    # Recommendations / SQL layout instead of a bare title + data
    # table.  Falls back to legacy kpis/insights when not provided.
    kpis = args.get("kpis") or []
    insights = args.get("insights") or []
    source = args.get("source") or ""
    summary = args.get("summary") or ""
    methodology = args.get("methodology") or ""
    key_findings = args.get("key_findings") or []
    recommendations = args.get("recommendations") or []
    sections = args.get("sections") or []
    sql_text = args.get("sql") or ""
    next_step = args.get("next_step") or ""
    # chart + warnings are forwarded so generate_pptx / generate_docx can
    # render a native chart slide and an amber warnings box respectively.
    chart = args.get("chart")
    warnings = args.get("warnings") or []
    user_message = (args.get("user_message") or instructions or "").strip()
    # synthesized_payload is the v2 Claude-style payload from the
    # report_synthesis LLM call.  When present, the skill-driven runner
    # uses it as the canonical source of content (the user_message +
    # title alone are not enough to drive an LLM document plan).
    synthesized_payload = args.get("synthesized_payload") or {}

    # --- Decide which runner to use for this format ---
    use_skill_driven = _format_supports_skill_driven(fmt)
    if use_skill_driven:
        # Pull the runner script content from the SKILL_DRIVEN_RUNNER_SCRIPT
        # field (set below in module init) so the source-of-truth lives
        # in one place.
        runner_script_b64 = base64.b64encode(
            _SKILL_DRIVEN_RUNNER_SCRIPT.encode("utf-8")
        ).decode("ascii")
        runner_script_name = "skill_driven_runner.py"
        job_skill_name = "skill_runner"
        job_timeout = _skill_driven_timeout()
    else:
        runner_script_b64 = base64.b64encode(runner_script.encode("utf-8")).decode("ascii")
        runner_script_name = "sandbox_runner.py"
        job_skill_name = "sandbox_runner"
        job_timeout = 120

    input_package = {
        "skill_config": {
            "format": fmt,
            "title": title,
            "instructions": instructions,
            "row_count": len(data),
            "kpis": kpis,
            "insights": insights,
            "source": source,
            # v2 Claude-style fields
            "summary": summary,
            "methodology": methodology,
            "key_findings": key_findings,
            "recommendations": recommendations,
            "sections": sections,
            "sql": sql_text,
            "next_step": next_step,
            "chart": chart,
            "warnings": warnings,
            # C-Heavy skill-driven additions: the runner uses these to
            # plan + generate the document via the LLM.  When the LLM
            # path fails or is disabled, the fallback_generator uses
            # the same fields as content.
            "user_message": user_message,
            "synthesized_payload": synthesized_payload,
        },
        "data_snapshots": [
            {
                "name": "query_results",
                "data": data,
                "format": "json",
            }
        ],
        "instructions": instructions or f"Generate a {fmt.upper()} report titled '{title}' from the provided data.",
        "runner_script": runner_script_b64,
        "runner_script_name": runner_script_name,
    }

    # Vendor the shared layout engine + branded charts alongside the runner so
    # the deterministic sandbox_runner.generate_pptx can render via the SAME
    # single source of truth as the in-process exporter.  The worker writes
    # these to /input/skill/ (already on sys.path) so the runner can
    # ``import layout_engine``.  Always present (cheap, base64 ~16 KB).
    input_package["runner_modules_b64"] = _build_runner_modules()

    # Skill-driven jobs need the skill bundle + runner modules written
    # alongside the main script.  Both go through the existing base64
    # mechanisms (``skill_bundle`` → /input/skill_bundle/,
    # ``runner_modules_b64`` → /input/skill/).
    if use_skill_driven:
        skill_bundle = _build_skill_bundle(fmt)
        runner_modules = _build_runner_modules()
        if skill_bundle:
            input_package["skill_bundle"] = skill_bundle
        if runner_modules:
            input_package["runner_modules_b64"] = runner_modules
        # If we couldn't bundle any skill files, drop back to the
        # deterministic path so we don't send the LLM into a job with
        # no workflow to follow.
        if not skill_bundle or not runner_modules:
            logger.warning(
                "Skill-driven bundle incomplete for format=%s "
                "(skill_files=%d, modules=%d); falling back to deterministic runner",
                fmt, len(skill_bundle), len(runner_modules),
            )
            runner_script_b64 = base64.b64encode(runner_script.encode("utf-8")).decode("ascii")
            runner_script_name = "sandbox_runner.py"
            job_skill_name = "sandbox_runner"
            job_timeout = 120
            input_package["runner_script"] = runner_script_b64
            input_package["runner_script_name"] = runner_script_name
            input_package.pop("skill_bundle", None)
            input_package.pop("runner_modules_b64", None)

    output_spec = {
        "format": fmt,
        "expected_files": [f"report.{_EXT_BY_FORMAT[fmt]}"],
    }

    # --- 4. Create sandbox job ---
    from app.services.sandbox.sandbox_service import SandboxService
    sandbox_service = SandboxService(db)

    job = sandbox_service.create_job(
        skill_name=job_skill_name,
        artifact_id=artifact.id,
        artifact_version_id=version.id,
        conversation_id=conversation_id,
        input_package=input_package,
        output_spec=output_spec,
        timeout_seconds=job_timeout,
        image_name=_IMAGE_BY_FORMAT[fmt],
    )

    logger.info(
        "Created sandbox job %s for artifact %s (format=%s, rows=%d)",
        job.id, artifact.id, fmt, len(data),
    )

    # --- 5. Poll for completion ---
    # Skill-driven document jobs can legitimately run longer than the
    # historical 120s handler window. Poll for the job's own timeout plus a
    # small grace period so we don't return "status running" while the worker
    # is still rendering the artifact.
    poll_limit = max(_MAX_POLL_SECONDS, int(job_timeout or 0) + 10)
    elapsed = 0.0
    while elapsed < poll_limit:
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        elapsed += _POLL_INTERVAL_SECONDS

        # Refresh job status from DB
        db.expire_all()
        current_job = sandbox_service.get_job(job.id)
        if not current_job:
            break

        status = current_job.status
        if status in ("completed", "failed", "timeout", "cancelled"):
            break

    # --- 6. Build result ---
    final_job = sandbox_service.get_job(job.id)
    if not final_job:
        return {"success": False, "error": "Sandbox job disappeared during polling"}

    if final_job.status != "completed":
        if final_job.status in ("queued", "running"):
            error_msg = (
                final_job.error_message
                or f"Sandbox job is still {final_job.status} after {int(elapsed)}s "
                f"(timeout window {poll_limit}s). The worker may still be rendering; job_id={job.id}."
            )
            try:
                sandbox_service.update_job_status(job.id, "timeout", error_message=error_msg)
                final_status = "timeout"
            except Exception:
                final_status = final_job.status
        else:
            error_msg = final_job.error_message or f"Sandbox job ended with status '{final_job.status}'"
            final_status = final_job.status
        # Mark artifact version as failed
        artifact_service.mark_version_failed(version.id, error_msg)
        return {
            "success": False,
            "error": error_msg,
            "job_id": job.id,
            "job_status": final_status,
            "artifact_id": artifact.id,
        }

    # Job completed — check for stored blobs
    blobs = artifact_service.get_version_blobs(version.id, blob_type="original")

    files_info = []
    for blob in blobs:
        files_info.append({
            "file_name": blob.file_name,
            "file_size": blob.file_size,
            "mime_type": blob.mime_type,
        })

    # Canonical create_artifact-compatible keys so the turn pipeline's
    # artifact collector surfaces sandbox output identically to the
    # direct/exporter path (one render path, swappable engines).
    primary_blob = blobs[0] if blobs else None
    canonical_file_name = (
        primary_blob.file_name if primary_blob and primary_blob.file_name
        else f"report.{_EXT_BY_FORMAT[fmt]}"
    )
    canonical_mime = (
        primary_blob.mime_type if primary_blob and primary_blob.mime_type
        else _MIME_BY_FORMAT[fmt]
    )
    canonical_size = primary_blob.file_size if primary_blob else None

    # ------------------------------------------------------------------
    # Sidecar rich HTML preview (Layer 2 of the "one card per file
    # format" plan).  For docx/pptx/xlsx we generate an interactive
    # Plotly-dashboard HTML artifact in-process and link it to the
    # file-format artifact via ``metadata_json.preview_artifact_id``.
    # The frontend ``ArtifactPreviewPane`` will then iframe the
    # sidecar's preview URL instead of falling back to the plain
    # mammoth rendering.
    # ------------------------------------------------------------------
    preview_artifact_id: Optional[str] = None
    if fmt in ("docx", "pptx", "xlsx"):
        try:
            # Build the same data/config dicts the sandbox runner uses
            # so the sidecar matches the file content 1:1.
            sidecar_config = {
                "format": fmt,
                "title": title,
                "instructions": instructions,
                "kpis": kpis,
                "insights": insights,
                "source": source,
                # v2 Claude-style fields (so the sidecar dashboard
                # surfaces the same rich sections as the docx).
                "summary": summary,
                "methodology": methodology,
                "key_findings": key_findings,
                "recommendations": recommendations,
                "sections": sections,
                "sql": sql_text,
                "next_step": next_step,
            }
            # ``generate_html`` is the same function the sandbox
            # Docker job calls; it writes to ``OUTPUT_DIR`` (=/output
            # inside the container) and returns the filename.  We
            # need the actual bytes for the sidecar blob, so we run
            # it under a temporary OUTPUT_DIR and read the file back.
            import tempfile
            from pathlib import Path
            from app.services.sandbox import sandbox_runner as _sr
            with tempfile.TemporaryDirectory(prefix="zhanlu-sidecar-") as tmp:
                original_output_dir = _sr.OUTPUT_DIR
                _sr.OUTPUT_DIR = Path(tmp)
                try:
                    _sr.generate_html(data, sidecar_config, instructions)
                    sidecar_path = Path(tmp) / "report.html"
                    sidecar_html = sidecar_path.read_text(encoding="utf-8")
                finally:
                    _sr.OUTPUT_DIR = original_output_dir

            # Fresh sidecar artifact + version + blob.
            sidecar = artifact_service.create_artifact(
                artifact_type="html",
                title=f"{title} (preview)",
                conversation_id=conversation_id,
                created_by_agent_id=agent_app_id,
                description="Rich interactive preview generated alongside the file-format artifact.",
            )
            sidecar_version = artifact_service.create_version(
                artifact_id=sidecar.id,
                changelog="Rich HTML sidecar preview",
                produced_by_skill="sandbox_runner_sidecar",
            )
            sidecar_file_name = f"{title}-preview.html"
            artifact_service.store_blob(
                sidecar_version.id,
                blob_type="original",
                file_name=sidecar_file_name,
                mime_type="text/html",
                data=sidecar_html.encode("utf-8"),
            )
            # Mark the sidecar as preview-ready immediately — the bytes
            # were generated in-process and don't need a Docker job.
            artifact_service.mark_version_built(sidecar_version.id)

            # Link the file-format artifact → sidecar via metadata.
            # Note: ``mark_version_built`` already issued an explicit
            # commit on the sidecar's version, so the parent artifact
            # row's update is still in this session's transaction.  We
            # flush and commit here to make the linkage visible to the
            # chat response collector (``_collect_artifact_results``)
            # and the frontend, even when the caller's outer
            # transaction is later rolled back.
            artifact = artifact_service.get_artifact(artifact.id)
            if artifact is not None:
                md = dict(artifact.metadata_json or {})
                md["preview_artifact_id"] = sidecar.id
                artifact.metadata_json = md
                db.add(artifact)
                db.flush()
                db.commit()

            preview_artifact_id = sidecar.id
            logger.info(
                "Sidecar HTML preview %s created for %s artifact %s",
                sidecar.id, fmt, artifact.id,
            )
        except Exception as sidecar_err:
            # Non-fatal: the file format artifact still works without
            # the rich preview.  Frontend will fall back to mammoth
            # for docx or no-preview for pptx/xlsx.
            logger.warning(
                "Failed to generate sidecar HTML preview for %s artifact %s: %s",
                fmt, artifact.id, sidecar_err,
            )

    return {
        "success": True,
        "artifact_id": artifact.id,
        "artifact_version_id": version.id,
        # canonical keys (same names as create_artifact results)
        "version_id": version.id,
        "version_number": version.version_number,
        "file_url": f"/api/artifacts/{artifact.id}/download",
        "type": fmt,
        "file_name": canonical_file_name,
        "mime_type": canonical_mime,
        "file_size": canonical_size,
        "has_preview": bool(blobs),
        "title": title,
        "format": fmt,
        "files": files_info,
        "preview_url": f"/api/artifacts/{artifact.id}/preview",
        "download_url": f"/api/artifacts/{artifact.id}/download",
        # Sidecar rich-preview linkage (Layer 2).  Frontend
        # ArtifactPreviewPane prefers this over the file-format
        # artifact's own preview endpoint.
        "preview_artifact_id": preview_artifact_id,
        "job_id": job.id,
        "message": (
            f"Artifact generated successfully: {title} ({fmt.upper()}). "
            f"The file is stored as a versioned artifact. "
            f"Tell the user they can preview or download it using the artifact card. "
            f"Preview URL: /api/artifacts/{artifact.id}/preview, "
            f"Download URL: /api/artifacts/{artifact.id}/download"
            f"{_deck_edit_hint(fmt, version_source_json)}"
        ),
    }


# ---------------------------------------------------------------------------
# Schema & Registration
# ---------------------------------------------------------------------------

RUN_SANDBOX_SKILL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "run_sandbox_skill",
        "description": (
            "Generate a file artifact (Excel, PPTX, HTML dashboard, or PDF) from data. "
            "Use this AFTER calling ask_data_agent to get real data rows. "
            "Pass the rows as the 'data' parameter. The file is generated in an "
            "isolated Docker sandbox and stored as a versioned artifact with "
            "preview and download URLs. "
            "Supported formats: xlsx (spreadsheet), pptx (presentation), "
            "html (interactive dashboard with charts), pdf (document), docx (Word), md (markdown). "
            "For docx/pptx/xlsx/pdf, the document is generated by an LLM inside the sandbox "
            "using the relevant document skill (docx-js, html2pptx, openpyxl, reportlab) so the "
            "layout adapts to whatever document type the user asks for (competitive analysis, "
            "risk assessment, sales report, etc.) — not just a single hardcoded layout."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "format": {
                    "type": "string",
                    "enum": ["xlsx", "pptx", "html", "pdf", "docx", "md"],
                    "description": "Output file format.",
                },
                "data": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "Data rows to include in the report. "
                        "Pass the 'rows' array from a prior ask_data_agent call."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": "Title for the generated file/report.",
                },
                "instructions": {
                    "type": "string",
                    "description": (
                        "Natural-language instructions for what to generate. "
                        "E.g. 'Create a weekly sales summary with totals and a chart' "
                        "or 'Make a dashboard with bar charts for revenue by month'."
                    ),
                },
                "user_message": {
                    "type": "string",
                    "description": (
                        "The original user request that motivated this document. "
                        "For docx/pptx/xlsx/pdf, the LLM inside the sandbox uses this "
                        "to understand the user's document-type intent (competitive "
                        "analysis, risk assessment, sales report, etc.) and plan a "
                        "structure that matches.  Falls back to ``instructions`` when omitted."
                    ),
                },
                "synthesized_payload": {
                    "type": "object",
                    "description": (
                        "Optional rich content payload from the report_synthesis LLM call. "
                        "When present, the skill-driven runner uses summary/methodology/"
                        "key_findings/recommendations/sections/kpis as the source-of-truth "
                        "content instead of inventing values.  Keys mirror the "
                        "ReportCardPayload schema (summary, methodology, key_findings, "
                        "recommendations, sections, kpis, insights, next_step)."
                    ),
                },
            },
            "required": ["format", "data", "title"],
        },
    },
}

registry.register(
    name="run_sandbox_skill",
    schema=RUN_SANDBOX_SKILL_SCHEMA,
    handler=_run_sandbox_skill,
    category="sandbox",
    enabled_by_default=False,
    description="Generate a file artifact (xlsx/pptx/html/pdf) from data via Docker sandbox.",
)
