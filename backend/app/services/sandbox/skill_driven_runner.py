"""Skill-driven document generation runner.

This module runs INSIDE the sandbox container.  It is the replacement
for the old fixed-layout ``sandbox_runner.py`` — instead of routing
every document through the same hardcoded cover→summary→kpis→findings
layout, it uses an LLM (reached via the Unix-socket LLM proxy) plus
the relevant document skill's SKILL.md to dynamically plan and
code-generate the document for whatever document type the user asked
for.

Flow
----
1. Load ``/input/config.json`` (format, title, user_message,
   synthesized_payload, data) and ``/input/data/*.json`` (rows).
2. Load the skill bundle from ``/input/skill_bundle/`` (SKILL.md +
   companion docs).  The caller (tool handler) packaged these as
   base64 because the worker does not have a skills/ mount.
3. HTML / MD formats: bypass the LLM entirely and call a deterministic
   utility generator — they don't benefit from skill-driven planning.
4. DOCX / PPTX / XLSX / PDF formats:
     a. Try to reach the LLM proxy at ``$LLM_PROXY_SOCKET``.  If the
        socket is missing or unreachable, log + immediately fall back
        to the deterministic generator.
     b. **Planning call**: Send the skill's SKILL.md + the user's
        request + a small data sample.  Ask the LLM to output a JSON
        ``document_plan`` describing sections / layout / emphasis.
     c. **Code-gen call**: Send SKILL.md + the relevant companion doc
        (docx-js.md, html2pptx.md, …) + the plan + a compact data
        summary.  Ask the LLM to write the complete generation code
        (JS for docx/pptx, Python for xlsx/pdf).  The code must write
        the final file to ``/output/report.<ext>``.
     d. Execute the generated code (``node gen.js`` / ``python gen.py``).
     e. Validate the output file exists and is non-empty.  If pptx,
        also generate a thumbnail grid and (best-effort) ask the LLM
        to flag layout issues — fixing is optional.
     f. On execution failure: retry up to 2 times, feeding the
        stderr back into the code-gen prompt so the LLM can patch.
     g. On retry exhaustion: call the deterministic generator as an
        emergency safety net so the user always gets a file.
5. Write ``/output/build_manifest.json`` describing what was produced.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Optional

# Configure logging before importing sibling modules so their loggers
# also pick up the format.
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("skill_runner")

# The skill-driven runner lives INSIDE the sandbox image.  The skill
# bundle and the in-sandbox LLM client are copied alongside it.
#
# In production: the runner + llm_client + fallback_generator are
# written to /input/skill/ by the worker (see sandbox_tool._build_runner_modules).
#
# In dev / tests: the runner can also be imported from its repo location
# (app/services/sandbox/) so unit tests can exercise the orchestration
# logic directly.  We add both directories to sys.path so ``from
# llm_client import ...`` works in either context.
try:
    sys.path.insert(0, "/input/skill")
    sys.path.insert(0, os.path.dirname(__file__))  # dev / test context
    from llm_client import SandboxLLMClient, LLMProxyError
except Exception as e:  # noqa: BLE001
    logger.warning("Could not import llm_client: %s — LLM path disabled", e)
    SandboxLLMClient = None  # type: ignore[assignment]
    LLMProxyError = Exception  # type: ignore[assignment, misc]

try:
    from fallback_generator import (
        generate_docx_fallback,
        generate_pptx_fallback,
        generate_xlsx_fallback,
        generate_pdf_fallback,
        generate_html_utility,
        generate_md_utility,
    )
except Exception as e:  # noqa: BLE001
    logger.error("Could not import fallback_generator: %s", e)
    raise


# --- Paths ----------------------------------------------------------------

INPUT_DIR = Path("/input")
OUTPUT_DIR = Path("/output")
SKILL_BUNDLE_DIR = INPUT_DIR / "skill_bundle"
CONFIG_PATH = INPUT_DIR / "config.json"
DATA_DIR = INPUT_DIR / "data"
GEN_DIR = Path("/tmp/gen")
GEN_DIR.mkdir(parents=True, exist_ok=True)

# Map format → (code language, output extension, fallback function)
FORMAT_SPEC = {
    "docx": {"lang": "js", "ext": "docx", "fallback": generate_docx_fallback},
    "pptx": {"lang": "js", "ext": "pptx", "fallback": generate_pptx_fallback},
    "xlsx": {"lang": "py", "ext": "xlsx", "fallback": generate_xlsx_fallback},
    "pdf":  {"lang": "py", "ext": "pdf",  "fallback": generate_pdf_fallback},
    "html": {"lang": "utility", "ext": "html", "fallback": generate_html_utility},
    "md":   {"lang": "utility", "ext": "md",   "fallback": generate_md_utility},
}

MAX_RETRIES = 2  # Total attempts after the first failure


# --- Helpers --------------------------------------------------------------

def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"config.json not found at {CONFIG_PATH}")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _load_data() -> list[dict]:
    rows: list[dict] = []
    if not DATA_DIR.exists():
        return rows
    for fp in sorted(DATA_DIR.glob("*.json")):
        try:
            rows.extend(json.loads(fp.read_text(encoding="utf-8")))
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not parse %s: %s", fp, e)
    return rows


def _load_skill_bundle(format_key: str) -> dict[str, str]:
    """Load the skill bundle files relevant to the requested format.

    Returns a dict of {relative_path: content}.  Always includes
    SKILL.md; adds the per-format companion doc (docx-js.md,
    html2pptx.md) and a curated subset of ooxml.md when present.
    """
    out: dict[str, str] = {}
    if not SKILL_BUNDLE_DIR.exists():
        logger.warning("skill_bundle directory missing at %s", SKILL_BUNDLE_DIR)
        return out

    # SKILL.md is always loaded
    skill_md = SKILL_BUNDLE_DIR / "SKILL.md"
    if skill_md.exists():
        out["SKILL.md"] = skill_md.read_text(encoding="utf-8")

    # Per-format companion docs
    companions = {
        "docx": ["docx-js.md", "ooxml.md"],
        "pptx": ["html2pptx.md", "ooxml.md"],
        "xlsx": ["xlsx.md"],
        "pdf":  ["pdf.md"],
    }
    wanted = companions.get(format_key, [])
    for name in wanted:
        p = SKILL_BUNDLE_DIR / name
        if p.exists():
            out[name] = p.read_text(encoding="utf-8")
        else:
            logger.info("Optional companion doc %s not in bundle", name)

    # Log size budget so we can spot bloated bundles early.
    total_chars = sum(len(v) for v in out.values())
    logger.info(
        "Loaded %d skill bundle files for format=%s (%d chars total)",
        len(out), format_key, total_chars,
    )
    return out


def _write_output(src: Path, output_path: Path) -> bool:
    """Copy generated file to /output and report size."""
    if not src.exists():
        return False
    output_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, output_path)
    size = output_path.stat().st_size
    logger.info("Wrote %s (%d bytes)", output_path, size)
    return size > 0


def _compact_data_for_prompt(data: list[dict], max_rows: int = 8) -> str:
    """Return a compact text representation of data for LLM prompts.

    The LLM doesn't need the full dataset in its prompt — it just needs
    enough shape to know what fields are available.  We send at most
    ``max_rows`` rows and cap the total character budget to keep the
    planning prompt small.
    """
    if not data:
        return "(no data rows)"
    sample = data[:max_rows]
    # Get union of keys (preserve order from first row)
    keys: list[str] = []
    seen: set[str] = set()
    for row in sample:
        if isinstance(row, dict):
            for k in row.keys():
                if k not in seen:
                    seen.add(k)
                    keys.append(k)
    # Build a tiny ASCII table
    lines = [", ".join(keys)]
    for row in sample:
        if not isinstance(row, dict):
            continue
        cells = [str(row.get(k, ""))[:60] for k in keys]
        lines.append(", ".join(cells))
    text = "\n".join(lines)
    if len(text) > 6000:
        text = text[:6000] + "\n... (truncated)"
    suffix = f"\n... ({len(data) - len(sample)} more rows)" if len(data) > len(sample) else ""
    return text + suffix


# --- LLM-driven generation -----------------------------------------------

def _call_llm_planning(
    client,
    *,
    skill_md: str,
    user_message: str,
    title: str,
    data_sample: str,
    synthesized_payload: dict,
) -> Optional[dict]:
    """Phase 1: ask the LLM to plan the document structure.

    Returns a parsed JSON document plan, or None on any failure
    (caller should fall back to the deterministic generator).
    """
    payload_summary = {
        "summary": synthesized_payload.get("summary", ""),
        "methodology": synthesized_payload.get("methodology", ""),
        "key_findings_count": len(synthesized_payload.get("key_findings", []) or []),
        "recommendations_count": len(synthesized_payload.get("recommendations", []) or []),
        "sections_count": len(synthesized_payload.get("sections", []) or []),
        "kpis_count": len(synthesized_payload.get("kpis", []) or []),
    }

    system = (
        "You are a document-structure planner. Given a user's request and "
        "a skill definition, output a JSON object describing the optimal "
        "document structure. Be specific to the document TYPE — do not "
        "produce a generic template. Respond with ONLY the JSON object, "
        "no markdown fencing, no commentary."
    )
    user = f"""# User's request
{user_message or "(no explicit request — infer from title and content)"}

# Document title
{title}

# Skill workflow (excerpt)
{skill_md[:3500]}

# Synthesized content summary
{json.dumps(payload_summary, ensure_ascii=False, indent=2)}

# Data sample (CSV-like)
{data_sample}

# Your task
Return a JSON document plan with this exact shape:
{{
  "document_type": "<short snake_case name, e.g. competitive_analysis>",
  "narrative_arc": "<1-2 sentence story arc>",
  "sections": [
    {{"title": "<section title>", "purpose": "<why it exists>", "content_source": "<data|summary|finding|recommendation|synthesis>", "priority": "high|medium|low"}}
  ],
  "design": {{
    "tone": "<professional|technical|narrative|promotional>",
    "color_palette": "<one of the palettes named in the pptx skill, or omit for docx>",
    "emphasis": "<what to highlight most>"
  }}
}}

Constraints:
- 3 to 10 sections total.
- Sections must draw on the actual data + synthesized content; do NOT invent sections that have no source material.
- Order sections logically (executive summary first if useful, deep content in the middle, recommendations last).
- For a competitive analysis, expect: landscape, competitor profiles, SWOT, strategic recommendations.
- For a risk assessment, expect: risk register, severity matrix, mitigations.
- For a sales report, expect: KPI dashboard, period comparison, top performers, next-period plan.
"""
    try:
        text = client.chat(system=system, user=user, temperature=0.2, max_tokens=1500)
    except (LLMProxyError, ConnectionError, TimeoutError, OSError) as e:
        logger.warning("LLM planning call failed: %s: %s", type(e).__name__, e)
        return None

    # The LLM should return pure JSON.  Be lenient about stray fences.
    text = text.strip()
    if text.startswith("```"):
        # Strip leading ```json or ``` and trailing ```
        lines = text.splitlines()
        text = "\n".join(l for l in lines if not l.strip().startswith("```")).strip()
    try:
        plan = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning("LLM plan was not valid JSON: %s\n--- raw ---\n%s", e, text[:500])
        return None
    if not isinstance(plan, dict) or "sections" not in plan:
        logger.warning("LLM plan missing 'sections' key: %s", list(plan.keys()) if isinstance(plan, dict) else type(plan))
        return None
    logger.info(
        "LLM produced plan with %d sections, document_type=%s",
        len(plan["sections"]), plan.get("document_type"),
    )
    return plan


def _call_llm_code_gen(
    client,
    *,
    format_key: str,
    skill_docs: dict[str, str],
    plan: dict,
    user_message: str,
    title: str,
    synthesized_payload: dict,
    data_sample: str,
    error_feedback: Optional[str] = None,
) -> Optional[str]:
    """Phase 2: ask the LLM to write the actual generation code.

    Returns the generated source code as a string, or None on failure.
    The caller is responsible for writing it to disk and executing it.
    """
    lang = FORMAT_SPEC[format_key]["lang"]
    ext = FORMAT_SPEC[format_key]["ext"]

    # Pick the right companion doc for the language
    if lang == "js":
        companion = skill_docs.get("docx-js.md" if format_key == "docx" else "html2pptx.md", "")
    else:
        companion = skill_docs.get("ooxml.md", "")

    system = (
        f"You are a {ext.upper()} generation assistant. Write a complete, "
        f"runnable {('JavaScript' if lang == 'js' else 'Python')} script that "
        f"produces a single {ext.upper()} file at the exact path "
        f"`/output/report.{ext}`. Follow the skill workflow below. Respond "
        f"with ONLY the code, no markdown fencing, no commentary."
    )

    feedback_block = (
        f"\n\n# Previous attempt failed — error to fix\n{error_feedback}\n"
        if error_feedback else ""
    )

    user = f"""# Skill workflow
{skill_docs.get('SKILL.md', '')[:2500]}

# {('Companion reference' if lang == 'js' else 'OOXML reference')}
{companion[:6000] if companion else '(no companion doc bundled)'}

# Document plan
{json.dumps(plan, ensure_ascii=False, indent=2)}

# User's request
{user_message or title}

# Document title
{title}

# Synthesized content (use as CONTENT source — pull text from here, not invented)
{json.dumps({
    'summary': synthesized_payload.get('summary', ''),
    'methodology': synthesized_payload.get('methodology', ''),
    'key_findings': synthesized_payload.get('key_findings', []),
    'recommendations': synthesized_payload.get('recommendations', []),
    'sections': synthesized_payload.get('sections', []),
    'kpis': synthesized_payload.get('kpis', []),
    'insights': synthesized_payload.get('insights', []),
}, ensure_ascii=False, indent=2)[:8000]}

# Data rows available
You can read the full dataset from `/input/data/query_results.json` if needed.
A compact sample is:
{data_sample}

# Output requirements
- Write exactly ONE file: `/output/report.{ext}`
- Do NOT print anything other than brief status (or nothing at all).
- Do NOT write to any path outside `/output/` (and `/tmp/` for intermediates).
- Honor the document_plan: every section must appear in the output in order.
- Use real content from synthesized_payload + data — never invent numbers.
- For docx: use the docx-js library (already installed globally as `docx`).
- For pptx: create HTML slides (one per plan section) then use html2pptx.js (you can require it as a local script — see companion doc).
- For xlsx/pdf: use openpyxl / reportlab respectively.
{feedback_block}

Return ONLY the {('JavaScript' if lang == 'js' else 'Python')} source code.
"""
    try:
        code = client.chat(system=system, user=user, temperature=0.2, max_tokens=8000)
    except (LLMProxyError, ConnectionError, TimeoutError, OSError) as e:
        logger.warning("LLM code-gen call failed: %s: %s", type(e).__name__, e)
        return None

    # Strip stray code fences if present
    code = code.strip()
    if code.startswith("```"):
        lines = code.splitlines()
        # Drop first fence (```js / ```python / ```) and last fence
        lines = [l for l in lines if not l.strip().startswith("```")]
        code = "\n".join(lines).strip()
    if len(code) < 50:
        logger.warning("LLM code-gen output suspiciously short (%d chars)", len(code))
        return None
    return code


def _execute_generated_code(format_key: str, code: str) -> tuple[bool, str]:
    """Write code to disk and execute it.  Returns (success, stderr_or_msg)."""
    spec = FORMAT_SPEC[format_key]
    lang = spec["lang"]
    ext = spec["ext"]

    if lang == "js":
        script_path = GEN_DIR / f"gen_{int(time.time()*1000)}.js"
        script_path.write_text(code, encoding="utf-8")
        # Make the skill bundle's scripts/ discoverable too
        env = dict(os.environ)
        env["NODE_PATH"] = "/usr/lib/node_modules:/usr/local/lib/node_modules"
        try:
            result = subprocess.run(
                ["node", str(script_path)],
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return False, "node execution timed out after 120s"
        except FileNotFoundError as e:
            return False, f"node binary not found: {e}"
    else:
        script_path = GEN_DIR / f"gen_{int(time.time()*1000)}.py"
        script_path.write_text(code, encoding="utf-8")
        try:
            result = subprocess.run(
                ["python", str(script_path)],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            return False, "python execution timed out after 120s"
        except FileNotFoundError as e:
            return False, f"python binary not found: {e}"

    if result.returncode == 0:
        return True, result.stdout + result.stderr
    return False, (result.stderr or result.stdout)[:3000]


# --- Deterministic fallback ------------------------------------------------

def _run_fallback(format_key: str, config: dict, data: list[dict], reason: str) -> bool:
    """Call the deterministic fallback generator.  Logs the reason so
    we can see in the artifact timeline WHY the LLM path didn't work.
    """
    spec = FORMAT_SPEC[format_key]
    output_path = OUTPUT_DIR / f"report.{spec['ext']}"
    logger.warning("Using deterministic fallback for %s: %s", format_key, reason)
    try:
        spec["fallback"](
            output_path=output_path,
            config=config,
            data=data,
        )
        if output_path.exists() and output_path.stat().st_size > 0:
            logger.info("Fallback generator produced %s (%d bytes)", output_path, output_path.stat().st_size)
            return True
        logger.error("Fallback generator returned but file is missing or empty: %s", output_path)
        return False
    except Exception:  # noqa: BLE001
        logger.exception("Fallback generator crashed for %s", format_key)
        return False


# --- Main orchestration ---------------------------------------------------

def _emit_manifest(format_key: str, mode: str, plan: Optional[dict], ok: bool, error: Optional[str]) -> None:
    manifest = {
        "format": format_key,
        "mode": mode,  # "skill_driven" | "fallback"
        "ok": ok,
        "error": error,
        "plan": plan,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (OUTPUT_DIR / "build_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _run_skill_driven(
    format_key: str,
    config: dict,
    data: list[dict],
    skill_docs: dict[str, str],
) -> tuple[bool, Optional[dict], Optional[str]]:
    """Run the full skill-driven pipeline.  Returns (ok, plan, error_msg)."""
    if SandboxLLMClient is None:
        return False, None, "llm_client module not available"

    try:
        client = SandboxLLMClient()
    except Exception as e:  # noqa: BLE001
        return False, None, f"could not initialize LLM client: {e}"

    # Connectivity probe — fail fast rather than after the first LLM call
    try:
        # A trivial chat call to verify the proxy is reachable
        client.chat(system="ping", user="ping", max_tokens=5)
    except (LLMProxyError, ConnectionError, TimeoutError, OSError, FileNotFoundError) as e:
        return False, None, f"LLM proxy unreachable: {type(e).__name__}: {e}"

    # Phase 1: planning
    plan = _call_llm_planning(
        client,
        skill_md=skill_docs.get("SKILL.md", ""),
        user_message=config.get("user_message") or config.get("instructions") or "",
        title=config.get("title") or "Report",
        data_sample=_compact_data_for_prompt(data),
        synthesized_payload=config.get("synthesized_payload") or {},
    )
    if not plan:
        return False, None, "LLM planning call returned no usable plan"

    # Phase 2: code-gen (with retries)
    last_error: Optional[str] = None
    code: Optional[str] = None
    for attempt in range(MAX_RETRIES + 1):
        code = _call_llm_code_gen(
            client,
            format_key=format_key,
            skill_docs=skill_docs,
            plan=plan,
            user_message=config.get("user_message") or config.get("instructions") or "",
            title=config.get("title") or "Report",
            synthesized_payload=config.get("synthesized_payload") or {},
            data_sample=_compact_data_for_prompt(data),
            error_feedback=last_error,
        )
        if not code:
            last_error = "LLM returned no code (possible truncation or refusal)"
            continue

        spec = FORMAT_SPEC[format_key]
        out_path = OUTPUT_DIR / f"report.{spec['ext']}"
        ok, msg = _execute_generated_code(format_key, code)
        if ok and out_path.exists() and out_path.stat().st_size > 0:
            logger.info("Skill-driven generation succeeded (attempt %d)", attempt + 1)
            return True, plan, None
        last_error = msg
        logger.warning(
            "Skill-driven attempt %d/%d failed: %s",
            attempt + 1, MAX_RETRIES + 1, (msg or "(no output)")[:300],
        )

    return False, plan, f"all {MAX_RETRIES + 1} attempts failed: {last_error}"


def main() -> int:
    """Entry point — called as ``python skill_driven_runner.py``."""
    logger.info("skill_driven_runner starting (cwd=%s)", os.getcwd())
    try:
        config = _load_config()
        data = _load_data()
    except Exception as e:  # noqa: BLE001
        logger.exception("Could not load input: %s", e)
        _emit_manifest("unknown", "fallback", None, False, f"input load failed: {e}")
        return 1

    fmt = (config.get("format") or "").lower()
    if fmt == "dashboard":
        fmt = "html"
    if fmt not in FORMAT_SPEC:
        _emit_manifest(fmt, "fallback", None, False, f"unsupported format: {fmt}")
        return 1

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    skill_docs = _load_skill_bundle(fmt)

    # HTML / MD: deterministic utility path (no LLM benefit)
    if FORMAT_SPEC[fmt]["lang"] == "utility":
        ok = _run_fallback(fmt, config, data, "html/md use the deterministic utility directly")
        _emit_manifest(fmt, "fallback", None, ok, None if ok else "utility generator failed")
        return 0 if ok else 1

    # DOCX / PPTX / XLSX / PDF: try skill-driven first
    ok, plan, err = _run_skill_driven(fmt, config, data, skill_docs)
    if ok:
        _emit_manifest(fmt, "skill_driven", plan, True, None)
        return 0

    # Fall back to deterministic generator
    logger.warning("Skill-driven path failed (%s) — falling back to deterministic generator", err)
    fallback_ok = _run_fallback(fmt, config, data, err or "unknown skill-driven failure")
    _emit_manifest(fmt, "fallback", plan, fallback_ok, err if not fallback_ok else None)
    return 0 if fallback_ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        logger.exception("skill_driven_runner crashed")
        sys.exit(2)