"""Server-driven generation orchestrator.

Guarantees that a user request for a downloadable file (docx / pptx / pdf /
html) results in an actual artifact — never a bare "I'll create …" reply.

Two responsibilities:

1. **Marker fulfillment (async fix).** The chat runtime detects
   ``◤MD_DOCX◤`` / ``◤HTML_DOCX◤`` / ``◤PPTX◤`` markers in the assistant
   reply and routes them into :func:`_create_artifact_tool`. That handler is
   ``async``; the legacy call sites invoked it *without* ``await``, producing
   a never-awaited coroutine — so no artifact was ever created. This module
   awaits it properly.

2. **Doc-request fallback.** When the turn-action router determined the user
   asked for a file (``doc_format`` is set) but the LLM produced *neither* a
   marker *nor* a successful ``create_artifact`` tool call, the orchestrator
   synthesizes a minimal ReportCard payload from the assistant's prose and
   creates the artifact server-side. The user always receives a downloadable
   file or a clear, logged error — never silence.

This is the Q1 (server-driven orchestrator) decision from the end-to-end fix.
It is best-effort and non-fatal: any failure is logged and swallowed so the
chat response itself is never broken.
"""

from __future__ import annotations

import html as _html
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.models.artifact import Artifact
from app.services.artifact_markers import find_markers
from app.services.synexia.intent_router import FileFormat

logger = logging.getLogger(__name__)

# Cap embedded data so a runaway query never produces a 100 MB HTML artifact.
_DASHBOARD_MAX_ROWS = 500
_DASHBOARD_MAX_HTML_BYTES = 2 * 1024 * 1024  # 2 MB cap on file read
_DASHBOARD_TITLE_MAX = 120

def _create_artifact_tool(args, db=None, context=None):  # noqa: ANN001
    """Lazy resolve the real async handler.

    Kept as a module-level indirection so tests can ``patch.object`` the
    symbol without importing the heavy exporter stack at module load time.
    The real handler is async; this wrapper simply returns its coroutine so
    callers ``await`` it as usual.
    """
    from app.services.tool_handlers.artifact_tool import _create_artifact_tool as _impl

    return _impl(args=args, db=db, context=context)


# Marker kinds we know how to route into _create_artifact_tool.
_MARKER_KIND_TO_TYPE = {
    "MD_DOCX": "docx",
    "HTML_DOCX": "docx",
    "PPTX": "pptx",
    "DASHBOARD": "html",
}

# FileFormat values that map to a create_artifact ``type``. Formats that the
# exporter pipeline cannot produce (xlsx, md) are intentionally absent — we do
# not fabricate artifacts we cannot render. ``dashboard`` is the one synthetic
# format that IS renderable: it produces a self-contained interactive HTML
# dashboard artifact (Chart.js + KPI cards + sortable table).
_FORMAT_TO_ARTIFACT_TYPE = {
    "docx": "docx",
    "pptx": "pptx",
    "pdf": "pdf",
    "html": "html",
    "dashboard": "html",
}


def _artifact_matches_requested_format(artifact_id: str, doc_format: str, db) -> bool:  # noqa: ANN001
    """True only when the stored Artifact's type/payload signal matches the
    explicitly requested doc_format (pptx/docx/xlsx/dashboard). Report cards
    and other html/text artifacts never satisfy an explicit file request."""
    expected = _FORMAT_TO_ARTIFACT_TYPE.get(doc_format)
    if expected is None:
        # xlsx / md are not renderable by this pipeline; no artifact can be
        # considered a match, so the fallback keeps trying.
        return False
    try:
        artifact = db.get(Artifact, artifact_id)
    except Exception:  # noqa: BLE001 — db is best-effort here
        artifact = None
    if artifact is None:
        return False
    atype = (artifact.artifact_type or "").lower().strip()
    # dashboard requests are satisfied by html artifacts (the orchestrator
    # fallback itself produces type=html), never by html_report report cards.
    return atype == expected


def _artifact_satisfies_deliverable(artifact_id: str, doc_format: str, db) -> bool:  # noqa: ANN001
    """Goal-Contract content gate: True only when the stored Artifact both
    matches the requested format AND carries a non-empty, non-failed payload
    (a build actually produced it).

    A report card (html_report) never satisfies an explicit doc deliverable,
    and a ``failed`` artifact or one with no built version (``current_version_id``
    is None) is treated as "not delivered" so the fallback still produces the
    user's deliverable instead of a dead file. Flag-gated by
    ``GOAL_CONTRACT_ENABLED`` at the call site.
    """
    if not _artifact_matches_requested_format(artifact_id, doc_format, db):
        return False
    try:
        artifact = db.get(Artifact, artifact_id)
    except Exception:  # noqa: BLE001 — db is best-effort here
        return False
    if artifact is None:
        return False
    if (artifact.status or "").lower() == "failed":
        return False
    # No current version ⇒ nothing was actually built (empty deliverable).
    return bool(artifact.current_version_id)


def _marker_to_artifact_args(kind: str, payload: dict, filename: str) -> Optional[dict]:
    """Translate a parsed marker into ``_create_artifact_tool`` args.

    Returns ``None`` for unknown kinds. Note: the current
    ``_payload_to_reportcard`` inside the handler reads ReportCard-shaped
    keys, so we forward the source path *and* a summary hint; the handler's
    ReportCard mapping is the single place that converts the payload.
    """
    artifact_type = _MARKER_KIND_TO_TYPE.get(kind)
    if artifact_type is None:
        return None
    # Forward the file path the skill wrote (outputs/...) so a future
    # path-aware renderer can pick it up, plus a ReportCard title/summary so
    # the current renderer produces a non-empty skeleton.
    fwd_payload = dict(payload)
    fwd_payload.setdefault("title", filename or f"{artifact_type}-artifact")
    # DASHBOARD markers carry an html_path; consume it here so the html
    # artifact handler receives html_content directly (the marker-path
    # key was previously forwarded but not consumed by any renderer).
    if kind == "DASHBOARD":
        html_content = _read_dashboard_html(payload.get("html_path"))
        if html_content is None:
            logger.warning(
                "orchestrator: DASHBOARD marker has no readable html_path=%r; "
                "falling back to no-content artifact",
                payload.get("html_path"),
            )
        else:
            fwd_payload["html_content"] = html_content
        title = str(fwd_payload.pop("title", None) or filename or "Dashboard")
    else:
        title = filename or f"{artifact_type}-artifact"
    return {
        "type": artifact_type,
        "title": title,
        "payload": fwd_payload,
        "skill": kind.lower(),
    }


def _read_dashboard_html(html_path: Any) -> Optional[str]:
    """Safely read a dashboard HTML file produced by the skill body.

    Returns ``None`` if the path is missing, the file does not exist, is
    larger than ``_DASHBOARD_MAX_HTML_BYTES``, or fails to read. Caps the
    read so a runaway / hostile file cannot produce a 100 MB artifact.
    """
    if not html_path or not isinstance(html_path, str):
        return None
    try:
        path = Path(html_path)
        if not path.is_absolute():
            # Marker paths are relative to the agent working dir (backend/).
            cwd = Path.cwd()
            # Prefer cwd, but fall back to the file's literal path which is
            # what the LLM actually wrote under outputs/.
            candidates = [cwd / path, Path(__file__).resolve().parents[2] / path]
            resolved = next((p for p in candidates if p.exists() and p.is_file()), None)
            if resolved is None:
                return None
            path = resolved
        if not path.exists() or not path.is_file():
            return None
        size = path.stat().st_size
        if size <= 0 or size > _DASHBOARD_MAX_HTML_BYTES:
            logger.warning(
                "orchestrator: dashboard html %s size=%d outside (0, %d]; skipping",
                path, size, _DASHBOARD_MAX_HTML_BYTES,
            )
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        logger.warning("orchestrator: failed to read dashboard html %r: %s", html_path, exc)
        return None


async def fulfill_markers(
    assistant_content: str,
    db: Session,
    context: dict,
) -> tuple[str, list[dict]]:
    """Detect markers in the reply, create an artifact for each, strip markers.

    Args:
        assistant_content: The raw assistant text (may contain markers).
        db:                Active DB session.
        context:           ``{conversation_id, agent_app_id}`` for the tool.

    Returns:
        ``(cleaned_content, created_artifacts)`` where ``created_artifacts``
        is a list of successful ``_create_artifact_tool`` result dicts.
    """
    created: list[dict] = []
    try:
        markers = find_markers(assistant_content)
    except Exception as exc:  # find_markers is defensive, but never break chat
        logger.warning("orchestrator: marker scan failed (non-fatal): %s", exc)
        return assistant_content, created

    for m in markers:
        args = _marker_to_artifact_args(m.kind, m.payload, m.filename)
        if args is None:
            continue
        try:
            result = await _create_artifact_tool(args=args, db=db, context=context)
            if isinstance(result, dict) and result.get("success"):
                created.append(result)
            else:
                logger.warning(
                    "orchestrator: marker %s did not produce an artifact: %s",
                    m.kind, (result or {}).get("error") if isinstance(result, dict) else result,
                )
        except Exception as exc:
            logger.warning("orchestrator: marker %s execution failed (non-fatal): %s", m.kind, exc)

    from app.services.artifact_markers import strip_markers
    cleaned = strip_markers(assistant_content)
    return cleaned, created


# Procedural narrator chatter the LLM emits while working ("I'm going to
# build …", "Let me load the PPTX skill …"). These sentences describe the
# assistant's own process — they must never become a report title or summary.
_CHATTER_RE = re.compile(
    r"^(?:"
    r"i['’]m going to|i am going to|i['’]ll|i will|i need to|i must|"
    r"let me|let['’]s|first[,\s]|next[,\s]i|now i|i['’]m now|"
    r"i have (?:now )?(?:loaded|created|generated|built)|"
    r"loading\b|please wait|one moment|hold on|"
    r"sure[!,.]|certainly[!,.]|of course[!,.]|great[!,.]|"
    r"here(?:'| i)s? (?:the|a|your)|as requested"
    r")",
    re.IGNORECASE,
)


# Generic heading patterns that must NEVER become a deliverable title.
# These are the first sentence a model often emits ("Quarterly Sales
# Report", "Monthly Performance", "Q3 Update") — useful as section
# labels but useless as the artifact's headline. The blocklist also
# catches time-only fragments ("Q3", "Q4 2026", "July", "August") that
# leak into titles when the LLM starts with a date anchor.
#
# NOTE: The blocklist is intentionally narrow — it rejects:
#   1. Pure time fragments (Q1-Q4, month names, bare years)
#   2. Pure single-word section headers ("Report", "Summary", "Overview")
#   3. Time-period + report-type boilerplate ("Quarterly Report",
#      "Monthly Update")
# It does NOT reject legitimate title prefixes like "Executive
# Overview", "July Overview", or "Sales Performance" — those carry
# real content and are valid as headlines.
_GENERIC_HEADING_BLOCKLIST = re.compile(
    r"^(?:"
    # Pure time fragments
    r"q[1-4](?:\s+\d{4})?$|"
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)(?:\s+\d{4})?$|"
    r"\d{4}$|"
    # Pure single-word section headers
    r"report$|summary$|overview$|update$|review$|brief$|"
    r"analysis$|insights?$|snapshot$|recap$|notes?$|dashboard$|"
    r"wrap[\s-]?up$|check[\s-]?in$|status$|"
    # Exact section-header labels (the boilerplate that opens every
    # business report — "Executive Summary" is always a section, never
    # a title).
    r"executive summary$|"
    # Time-period + report-type boilerplate
    r"(?:quarterly|monthly|weekly|annual|yearly|daily|periodic)\s+"
    r"(?:report|summary|overview|update|review|brief|"
    r"analysis|recap|wrap[\s-]?up)$"
    r")",
    re.IGNORECASE,
)


def _is_meta_chatter(sentence: str) -> bool:
    """True when ``sentence`` is procedural narrator chatter, not content."""
    s = (sentence or "").strip()
    if not s:
        return True
    return bool(_CHATTER_RE.match(s))


def _split_sentences(text: str) -> list[str]:
    """Split prose into sentences, stripping markdown artifacts."""
    parts = re.split(r"(?<=[.!?。！？])\s+", (text or "").strip())
    return [re.sub(r"[#*_>`]", "", p).strip() for p in parts if p and p.strip()]


def _validate_artifact_data_quality(rows: list[dict] | None, user_message: str) -> dict:
    """Validate that the data is business-meaningful before creating an artifact.

    Returns {"valid": True/False, "reason": "..."}.

    Garbage data includes:
    - Empty rows (no data at all)
    - Rows containing ONLY internal ID columns (FID, FENTRYID, etc.)
    - Rows with no business-meaningful columns
    """
    if not rows:
        return {"valid": False, "reason": "no rows returned from query"}
    if not isinstance(rows, list) or len(rows) == 0:
        return {"valid": False, "reason": "empty row list"}

    first = rows[0] if isinstance(rows[0], dict) else {}
    if not first:
        return {"valid": False, "reason": "rows contain no column data"}

    cols = set(first.keys())

    # Check if ALL columns are internal IDs
    if cols and all(_is_internal_id_column(c) for c in cols):
        return {"valid": False, "reason": f"all columns are internal IDs: {', '.join(sorted(cols))}"}

    # Check if any column contains business-meaningful data
    has_business = any(_is_business_column(c) for c in cols)
    if not has_business:
        return {"valid": False, "reason": f"no business-meaningful columns found: {', '.join(sorted(cols))}"}

    return {"valid": True, "reason": ""}


def _prose_to_summary(text: str, max_chars: int = 4000) -> str:
    """Condense assistant prose into a ReportCard summary string.

    Procedural chatter sentences (``_is_meta_chatter``) are dropped first so
    the summary carries actual content rather than the assistant narrating
    its own process.
    """
    if not text:
        return ""
    sentences = [s for s in _split_sentences(text) if not _is_meta_chatter(s)]
    body = re.sub(r"\s+", " ", " ".join(sentences)).strip()
    if not body:
        return ""
    if len(body) > max_chars:
        truncated = body[: max_chars - 1]
        # Prefer breaking on a word boundary, but never exceed max_chars.
        if " " in truncated:
            truncated = truncated.rsplit(" ", 1)[0]
        body = truncated + "…"
    return body


def _title_from_prose(text: str, fallback: str) -> str:
    """Best-effort title: first markdown H1, else first *non-chatter*
    sentence, else fallback.

    Generic heading sentences (e.g. "Quarterly Sales Report", "Q3",
    "Executive Summary") are skipped so the deliverable title carries
    the user's actual content rather than a section label. The
    ``_GENERIC_HEADING_BLOCKLIST`` covers common time-only fragments
    and boilerplate report labels.

    When the H1 is blocklisted, the H1 line is stripped from the
    prose before sentence extraction so the blocklisted prefix doesn't
    leak into the first sentence.
    """
    if not text:
        return fallback
    h1 = re.search(r"^\s*#\s+(.+)$", text, flags=re.MULTILINE)
    if h1:
        candidate = h1.group(1).strip()[:120]
        if candidate and not _GENERIC_HEADING_BLOCKLIST.match(candidate):
            return candidate
        # H1 was blocklisted — strip the H1 line from the prose so
        # the blocklisted prefix doesn't contaminate the first
        # sentence in the next pass.
        text = re.sub(r"^\s*#\s+.+$", "", text, count=1, flags=re.MULTILINE)
    # Split on newlines first, then on sentence terminators within
    # each line, so multi-line prose (e.g. "Q3 2026\nExecutive
    # Summary") doesn't get joined into one "sentence" that defeats
    # the blocklist.
    candidates: list[str] = []
    for line in (text or "").splitlines():
        candidates.extend(_split_sentences(line))
    for sentence in candidates:
        if not sentence:
            continue
        if _is_meta_chatter(sentence):
            continue
        if _GENERIC_HEADING_BLOCKLIST.match(sentence):
            continue
        return sentence[:120]
    return fallback


# ── Meaningful-title generation (2026-08-24) ────────────────────────────
# Previously the deliverable title echoed the user's raw query verbatim
# ("i want July 2026 sales report (volume, revenue, margin, inventory) in
# docx file"). Now we strip conversational wrappers and file-format suffixes,
# pull date/period entities to the front, capitalize, and clamp.

_PREFIXES_TO_STRIP = re.compile(
    r"^\s*(i\s+want|i\s+need|please\s+(give|make|generate|create)\s+me|"
    r"can\s+you\s+(please\s+)?(give|make|generate|create|show)\s+(me\s+)?|"
    r"show\s+me|make\s+me|generate|create|write)\b\s*",
    re.IGNORECASE,
)

_SUFFIXES_TO_STRIP = re.compile(
    r"\s+(in|as|using)\s+(a\s+)?(docx|word|pdf|pptx|powerpoint|excel|xlsx|"
    r"markdown|md|html|file|document|deck|spreadsheet)"
    r"\s*(file|document|deck|spreadsheet)?\s*[\.\?]?\s*$",
    re.IGNORECASE,
)

_METRIC_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*")

# (pattern, format). The format is applied with str.format(*groups) unless it
# contains a "%" (a strftime format for the month-year pattern).
_PERIOD_PATTERNS = [
    (re.compile(
        r"\b(january|february|march|april|may|june|july|august|september|"
        r"october|november|december)\s+(\d{4})\b", re.I), "%B %Y"),
    (re.compile(r"\bq([1-4])\s*(\d{4})\b", re.I), "Q{0} {1}"),
    (re.compile(r"\blast\s+(\d+)\s+(days?|weeks?|months?)\b", re.I), "Last {0} {1}"),
    (re.compile(r"\bthis\s+(week|month|quarter|year)\b", re.I), "This {0}"),
]

_PERIOD_CONNECTOR_TAIL_RE = re.compile(r"\s+(for|of|in|on|to|with)\s*$")


def _split_period(text: str) -> tuple[str, str]:
    """Return (normalized_period, remainder) from the first period match.

    Example: ("july 2026 sales report", …) -> ("July 2026", " sales report").
    """
    for pattern, fmt in _PERIOD_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        raw = m.group(0)
        if "%" in fmt:
            period = datetime.strptime(raw.title(), "%B %Y").strftime(fmt)
        else:
            period = fmt.format(*m.groups())
        period = period[0].upper() + period[1:]
        remainder = text[: m.start()] + " " + text[m.end():]
        return period, remainder
    return "", text


def _extract_period(text: str) -> str:
    """Return a normalized period descriptor ("July 2026", "Q3 2026",
    "Last 30 days", "This month") found in the text, else ""."""
    return _split_period(text)[0]


def _generate_meaningful_title(
    user_message: str,
    assistant_content: str = "",
    fallback: str = "Data report",
) -> str:
    """Build a clean, creative deliverable title from the user's request.

    1. Strip file-format suffixes ("in docx file", "as a Word document", …).
    2. Strip conversational prefixes ("i want", "please give me", "generate", …).
    3. Drop parenthetical metric lists ("(volume, revenue, margin, inventory)").
    4. If empty/short afterwards, fall back to prose (H1 → first sentence) then
       to the caller-supplied fallback.
    5. Move any date/period entity ("July 2026", "Q3 2026") to the front.
    6. Capitalize the first letter, collapse whitespace, clamp to 60 chars.
    """
    text = re.sub(r"\s+", " ", (user_message or "").strip())
    text = _SUFFIXES_TO_STRIP.sub("", text).strip()
    text = _PREFIXES_TO_STRIP.sub("", text).strip()
    text = _METRIC_PAREN_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip().rstrip(".?!")
    if not text or len(text) < 3:
        return _title_from_prose(assistant_content, fallback=fallback)

    period, remainder = _split_period(text)
    if period:
        remainder = _PERIOD_CONNECTOR_TAIL_RE.sub("", remainder).strip()
        text = f"{period} {remainder}".strip() if remainder else period

    text = text[0].upper() + text[1:].lower()
    if len(text) > 60:
        cut = text[:57].rsplit(" ", 1)[0]
        text = cut.rstrip(" ,-") + "…"
    return text


def _title_from_user_request(user_message: str, fallback: str) -> str:
    """Deprecated alias kept for compatibility; use _generate_meaningful_title."""
    return _generate_meaningful_title(user_message, fallback=fallback)


def _mine_ask_data_result(
    tool_calls_for_frontend: list[dict], *, skip_superseded: bool = True,
) -> Optional[dict]:
    """Return the richest non-superseded ``ask_data_agent`` result dict.

    Prefers a result carrying this turn's synthesized ``report_card_payload``;
    otherwise the non-superseded result with the most rows (the overview is
    more likely to carry the deliverable's verified payload than a drill-down
    with fewer rows/columns). Returns None when no data call happened this
    turn.

    ``skip_superseded=True`` (default) drops results marked ``__superseded``
    by the v3 loop (an empty/error query replaced by a later re-query on the
    same bound KB). Superseded results must never shape a deck/card because
    they cite data that was already discarded.
    """
    if not tool_calls_for_frontend:
        return None
    best: Optional[dict] = None
    best_rows = -1
    best_cols = -1
    for tc in tool_calls_for_frontend:
        if tc.get("name") != "ask_data_agent":
            continue
        if skip_superseded and tc.get("__superseded"):
            continue
        res = tc.get("results") or {}
        if not isinstance(res, dict):
            continue
        if isinstance(res.get("report_card_payload"), dict) and res["report_card_payload"]:
            return res  # jackpot: this turn's own synthesized payload
        rows = res.get("rows")
        n_rows = len(rows) if isinstance(rows, list) else 0
        n_cols = (
            len(rows[0]) if (
                isinstance(rows, list) and rows and isinstance(rows[0], dict)
            ) else 0
        )
        if n_rows or res.get("answer"):
            # Prefer the overview (more rows/columns) over a drill-down so
            # the closing card reflects the deliverable, not the last query.
            if best is None or (n_rows, n_cols) > (best_rows, best_cols):
                best = res
                best_rows, best_cols = n_rows, n_cols
    return best


def _mine_enterprise_payload(
    tool_calls_for_frontend: list[dict], *, skip_superseded: bool = True,
) -> Optional[dict]:
    """Return the EnterpriseReport ``payload`` from this turn's
    ``collect_enterprise_data`` call (if any).

    The enterprise pipeline tool produces a 6-section executive
    payload that already carries ``enterprise_report_kind ==
    "executive"``. When present, this payload is the authoritative
    artifact content — the docx exporter will detect the marker and
    delegate to ``render_enterprise_docx`` to produce the full
    executive document (cover, KPI grid, breakdowns, drivers, risks,
    actions, lineage appendix).

    Returns ``None`` when no enterprise tool call happened (the
    caller falls through to the generic ReportCard path).

    ``skip_superseded=True`` (default) drops results marked
    ``__superseded`` so a failed/empty enterprise run never
    contaminates the artifact.
    """
    if not tool_calls_for_frontend:
        return None
    for tc in tool_calls_for_frontend:
        if tc.get("name") != "collect_enterprise_data":
            continue
        if skip_superseded and tc.get("__superseded"):
            continue
        res = tc.get("results") or {}
        if not isinstance(res, dict):
            continue
        if res.get("success") is not True:
            continue
        if (res.get("enterprise_report_kind") or "").lower().strip() != "executive":
            continue
        payload = res.get("payload")
        if not isinstance(payload, dict) or not payload:
            continue
        return payload
    return None


def _mine_ask_data_rows(
    tool_calls_for_frontend: list[dict], *, skip_superseded: bool = True,
) -> list[dict]:
    """Pull row dicts out of non-superseded data-producing tool calls.

    Scans ALL tools in ``DATA_PRODUCING_TOOLS`` (ask_data_agent, execute_query,
    ask_erp_kpi, etc.).  Legacy structured-data tools (ask_erp_kpi, etc.) have
    their results normalized into rows by ``_normalize_tool_result_to_rows``
    at the v3 stream level, so by the time we see them here they already
    contain a ``rows`` key.

    Honors the cap the platform documents for embedded data: anything beyond
    ``_DASHBOARD_MAX_ROWS`` is dropped (a fully pre-aggregated sample is
    preferable to a 100 MB HTML file).

    ``skip_superseded=True`` (default) drops results marked ``__superseded``
    so a stale empty/error query never contaminates the artifact payload.
    """
    from app.routers.agents import DATA_PRODUCING_TOOLS as _DATA_TOOLS

    if not tool_calls_for_frontend:
        return []
    rows: list[dict] = []
    for tc in tool_calls_for_frontend:
        if tc.get("name") not in _DATA_TOOLS:
            continue
        if skip_superseded and tc.get("__superseded"):
            continue
        res = tc.get("results") or {}
        if not isinstance(res, dict):
            continue
        # ask_data_agent returns {"answer": ..., "rows": [...], "sql": ...,
        # "source_name": ...}. The results can also be nested under
        # "data" or "result" depending on the call site; accept both.
        candidate = res.get("rows")
        if not isinstance(candidate, list):
            nested = res.get("data") or res.get("result")
            if isinstance(nested, dict):
                candidate = nested.get("rows")
        if not isinstance(candidate, list):
            continue
        for r in candidate:
            if isinstance(r, dict):
                rows.append(r)
            if len(rows) >= _DASHBOARD_MAX_ROWS:
                return rows
    return rows


def _iter_historical_data_results(
    conversation_messages: list[dict],
):
    """Yield ``(tc_or_msg, result_dict)`` for every data-producing tool
    result found in conversation history, **newest turn first**.

    Scans ALL tools in ``DATA_PRODUCING_TOOLS`` (ask_data_agent, execute_query,
    ask_erp_kpi, etc.), not just ask_data_agent.

    Handles BOTH persisted shapes:

    * **assistant messages** with a ``tool_calls`` array — the persistence
      layer saves tool results inline in each tool_call's ``results`` key
      (this is the shape stored in ``conv.messages``);
    * ``role: "tool"`` messages with a JSON-string ``content`` — the
      LLM-facing reconstruction produced when history is re-loaded for the
      model.

    Yields are in reverse-chronological order so callers can prefer the
    freshest data.
    """
    from app.routers.agents import DATA_PRODUCING_TOOLS as _DATA_TOOLS

    if not conversation_messages:
        return
    for msg in reversed(conversation_messages):
        role = msg.get("role")
        if role == "assistant":
            for tc in (msg.get("tool_calls") or []):
                if not isinstance(tc, dict):
                    continue
                if tc.get("name") not in _DATA_TOOLS:
                    continue
                res = tc.get("results")
                if isinstance(res, str):
                    try:
                        res = json.loads(res)
                    except Exception:
                        continue
                if isinstance(res, dict):
                    yield tc, res
        elif role == "tool":
            content = msg.get("content")
            if not isinstance(content, str):
                continue
            try:
                payload = json.loads(content)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            result = payload.get("result") or payload
            if isinstance(result, dict):
                yield msg, result


# Backward-compatible alias — old name still used by some call sites.
_iter_historical_ask_data_results = _iter_historical_data_results


def _mine_historical_ask_data_result(
    conversation_messages: list[dict], *, skip_superseded: bool = True,
) -> Optional[dict]:
    """Return the richest data-producing tool result dict from PREVIOUS turns.

    Scans ALL tools in ``DATA_PRODUCING_TOOLS`` (not just ask_data_agent).

    Same ranking as ``_mine_ask_data_result``: a synthesized
    ``report_card_payload`` wins outright; otherwise the result with the
    most rows/columns.  Returns ``None`` when no earlier turn fetched data.

    Handles both persisted shapes (see ``_iter_historical_data_results``).
    """
    best: Optional[dict] = None
    best_rows = -1
    best_cols = -1
    for tc_or_msg, res in _iter_historical_ask_data_results(conversation_messages):
        if skip_superseded and (
            res.get("__superseded") or tc_or_msg.get("__superseded")
        ):
            continue
        rcp = res.get("report_card_payload")
        if isinstance(rcp, dict) and rcp:
            return res  # jackpot — a synthesized payload from an earlier turn
        rows = res.get("rows")
        if not isinstance(rows, list):
            nested = res.get("data") or res.get("result")
            if isinstance(nested, dict):
                rows = nested.get("rows")
        n_rows = len(rows) if isinstance(rows, list) else 0
        n_cols = (
            len(rows[0]) if (
                isinstance(rows, list) and rows and isinstance(rows[0], dict)
            ) else 0
        )
        if n_rows or res.get("answer"):
            if best is None or (n_rows, n_cols) > (best_rows, best_cols):
                best = res
                best_rows, best_cols = n_rows, n_cols
    return best


def _mine_historical_answer_rows(
    conversation_messages: list[dict], *, skip_superseded: bool = True,
) -> list[dict]:
    """Pull row dicts out of data-producing tool results from PREVIOUS
    turns in the conversation history.

    Scans ALL tools in ``DATA_PRODUCING_TOOLS`` (not just ask_data_agent).

    When a follow-up turn (e.g. "give me in docx formate") re-queries the
    database and gets 0 rows, we can fall back to data that was successfully
    fetched in an earlier turn instead of producing a useless "no data" file.

    FIX 2026-08-24: rewritten to handle both persisted shapes —
    ``assistant.tool_calls[].results`` (the persistence layer's actual
    format) *and* ``role: "tool"`` JSON content (the LLM-facing
    reconstruction).  Previously only the latter was checked, which meant
    the function **always returned []** on persisted conversation history
    because the persistence layer never stores standalone tool messages.
    """
    rows: list[dict] = []
    for tc_or_msg, res in _iter_historical_ask_data_results(conversation_messages):
        if skip_superseded and (
            res.get("__superseded") or tc_or_msg.get("__superseded")
        ):
            continue
        candidate = res.get("rows")
        if not isinstance(candidate, list):
            nested = res.get("data") or res.get("result")
            if isinstance(nested, dict):
                candidate = nested.get("rows")
        if not isinstance(candidate, list):
            continue
        for r in candidate:
            if isinstance(r, dict):
                rows.append(r)
            if len(rows) >= _DASHBOARD_MAX_ROWS:
                return rows
    return rows


# Internal ID columns that should NEVER appear as KPI/chart values.
# These are surrogate keys (FID, FENTRYID, etc.) that carry no business
# meaning — showing them as KPIs produces garbage like "Total FENTRYID: 100102.0".
# Pattern: F-prefixed uppercase ending in ID, or common generic ID names.
_INTERNAL_ID_PATTERNS = frozenset({
    "id", "ID", "rowid", "ROWID", "uid", "UID", "uuid",
    "pk", "PK",
})

# Business-meaningful column name fragments for KPI detection.
# These are generic terms found in business databases across industries.
_BUSINESS_COL_FRAGMENTS = frozenset({
    "amount", "revenue", "quantity", "qty", "price", "cost", "profit",
    "margin", "name", "product", "material", "customer", "region",
    "date", "total", "sum", "count", "volume", "weight", "unit",
    "tax", "discount", "subtotal", "grand", "net", "gross",
    "sales", "order", "invoice", "payment", "receipt",
    "inventory", "stock", "warehouse", "shipment", "delivery",
})


def _is_internal_id_column(col: str) -> bool:
    """Return True if the column name looks like an internal ID/surrogate key.

    Detects:
    - Common generic ID names (id, rowid, uid, pk, etc.)
    - F-prefixed internal IDs (FMATERIALID, FENTRYID — common in ERP systems)tems)
    - Any column ending in _id or _ID (order_id, customer_id, etc.) when short
    - Columns ending in just "ID" in uppercase (FID, FSTOCKID, etc.)
    """
    if col in _INTERNAL_ID_PATTERNS:
        return True
    # F-prefixed uppercase: common in Kingdee/ERP systems (FMATERIALID, FID, etc.)
    if len(col) > 2 and col[0] == "F" and col[1].isupper() and col.endswith("ID"):
        return True
    # Any column ending in _id when it looks like a technical key
    if col.endswith("_id") or col.endswith("_ID"):
        return True
    return False


def _is_business_column(col: str) -> bool:
    """Return True if the column name contains a business-meaningful fragment."""
    return any(frag in col.lower() for frag in _BUSINESS_COL_FRAGMENTS)


def _rows_to_kpis(rows: list[dict], max_kpis: int = 4) -> list[dict]:
    """Pick up to ``max_kpis`` numeric columns and compute sum/avg/max KPIs.

    Each KPI is ``{label, value, display}``. Columns are scanned in stable
    column order (insertion order of the first row) so the dashboard reads
    deterministically. Up to two aggregates per column (sum + max) keeps the
    tile count small even with many numeric columns.

    Internal ID columns (FID, FENTRYID, etc.) are excluded — they carry no
    business meaning and produce garbage KPIs like "Total FENTRYID: 100102.0".
    """
    if not rows:
        return []
    cols: list[str] = []
    seen: set[str] = set()
    for r in rows:
        if not isinstance(r, dict):
            continue
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                cols.append(k)
    kpis: list[dict] = []
    for col in cols:
        # Skip internal ID columns — they carry no business meaning
        if _is_internal_id_column(col):
            continue
        # Prefer business-meaningful columns over generic numeric columns
        nums: list[tuple[Any, float]] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            v = r.get(col)
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                nums.append((r.get(col), float(v)))
            elif isinstance(v, str):
                # accept "1,234.56" and "12%" shapes
                cleaned = v.replace(",", "").rstrip("%").strip()
                try:
                    nums.append((v, float(cleaned)))
                except ValueError:
                    pass
        if not nums:
            continue
        s = sum(n for _, n in nums)
        m = max(nums, key=lambda x: x[1])
        unit = "%" if isinstance(m[0], str) and str(m[0]).endswith("%") else ""
        kpis.append({
            "label": f"Total {col}",
            "value": s,
            "display": f"{s:,.2f}{unit}" if abs(s) >= 1 else f"{s:.4f}{unit}",
        })
        if len(kpis) >= max_kpis:
            break
        kpis.append({
            "label": f"Max {col}",
            "value": m[1],
            "display": f"{m[1]:,.2f}{unit}" if abs(m[1]) >= 1 else f"{m[1]:.4f}{unit}",
        })
        if len(kpis) >= max_kpis:
            break
    return kpis[:max_kpis]


def _pick_chart_columns(rows: list[dict]) -> tuple[Optional[str], Optional[str]]:
    """Choose (label_col, value_col) for a single Chart.js bar chart.

    Heuristic: first non-numeric column as labels, first numeric column as
    values. Returns ``(None, None)`` if no suitable pair is found.

    Internal ID columns (FID, FENTRYID, etc.) are excluded — they produce
    meaningless charts with IDs on the axes.
    """
    if not rows:
        return None, None
    cols: list[str] = []
    seen: set[str] = set()
    for r in rows:
        if not isinstance(r, dict):
            continue
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                cols.append(k)
    label_col: Optional[str] = None
    value_col: Optional[str] = None
    for col in cols:
        # Skip internal ID columns — they produce meaningless charts
        if _is_internal_id_column(col):
            continue
        sample = next((r.get(col) for r in rows if isinstance(r, dict)), None)
        if sample is None:
            continue
        if isinstance(sample, (int, float)) and not isinstance(sample, bool):
            if value_col is None:
                value_col = col
        elif label_col is None:
            label_col = col
        if label_col and value_col:
            break
    return label_col, value_col


def _dashboard_theme_vars(theme) -> dict[str, str]:
    """Map a DeckTheme onto the dashboard's CSS variables.

    Falls back to the legacy dark dashboard palette when ``theme`` is
    None (no brand kit configured), so existing output is unchanged.
    """
    if theme is None:
        return {
            "bg": "#0f172a", "panel": "#111827", "ink": "#e5e7eb",
            "muted": "#94a3b8", "accent": "#60a5fa", "grid": "#1f2937",
        }
    h = theme.as_hex_dict()
    return {
        "bg": h["slide_bg"],
        "panel": h["surface"],
        "ink": h["text"],
        "muted": h["muted"],
        "accent": h["primary"],
        "grid": h["border"],
    }


def _dashboard_chart_type(labels: list[str]) -> str:
    """Pick a chart type from the data shape (P2.2 chart intelligence).

    * date-like labels (ISO dates, year-month, quarter) → line
    * everything else → bar (ranking/category comparisons)
    """
    import re as _re

    date_pat = _re.compile(
        r"^(\d{4}[-/]\d{1,2}([-/]\d{1,2})?|\d{1,2}[-/]\d{4}|\d{4}\s?[Qq][1-4]|"
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{2,4})"
    )
    if labels:
        hits = sum(1 for l in labels[:12] if date_pat.match(str(l).strip()))
        if hits >= max(1, len(labels[:12]) // 2):
            return "line"
    return "bar"


def _synthesize_dashboard_html(
    title: str,
    rows: list[dict],
    prose_summary: str,
    source_label: str = "ask_data_agent",
    theme=None,
) -> str:
    """Build a self-contained interactive HTML dashboard from query rows.

    Always returns a valid, browser-renderable HTML document. When ``rows``
    is empty, still produces an "empty state" shell (title + prose summary
    + an "no data" panel) so the user gets a visible artifact rather than
    silence. Chart.js is loaded from jsDelivr with an SRI hash; styles
    follow the chart.js + CSS Grid card layout that the
    ``artifacts-builder`` skill recommends.

    P1.1 unified tokens: when ``theme`` (a DeckTheme, e.g. resolved from
    the tenant brand kit) is provided, the dashboard palette comes from
    the same design tokens as the PPTX/DOCX renderers instead of the
    hardcoded dark palette.
    """
    safe_title = _html.escape((title or "Dashboard")[:_DASHBOARD_TITLE_MAX])
    kpis = _rows_to_kpis(rows)
    label_col, value_col = _pick_chart_columns(rows)
    chart_payload: dict[str, Any] = {"labels": [], "values": []}
    if rows and label_col and value_col:
        chart_payload = {
            "labels": [str(r.get(label_col, "")) for r in rows[:50] if isinstance(r, dict)],
            "values": [
                float(r.get(value_col, 0)) if isinstance(r.get(value_col, 0), (int, float))
                else 0.0
                for r in rows[:50] if isinstance(r, dict)
            ],
            "label_col": label_col,
            "value_col": value_col,
        }
    embedded_rows = [
        {str(k): _html.escape(str(v)[:200]) for k, v in r.items() if isinstance(r, dict)}
        for r in rows[:_DASHBOARD_MAX_ROWS]
    ]
    embedded_json = json.dumps({
        "rows": embedded_rows,
        "source": source_label,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kpis": kpis,
        "chart": chart_payload,
    }, ensure_ascii=False, default=str)

    kpi_html = "".join(
        f'<div class="kpi"><div class="kpi-label">{_html.escape(k["label"])}</div>'
        f'<div class="kpi-value">{_html.escape(k["display"])}</div></div>'
        for k in kpis
    ) or '<div class="kpi"><div class="kpi-label">No data</div><div class="kpi-value">—</div></div>'

    # Build the table (≤50 rows for initial render; full data is in
    # the embedded JSON for the "show all" toggle below).
    if embedded_rows and rows:
        header_cells = "".join(f"<th>{_html.escape(c)}</th>" for c in embedded_rows[0].keys())
        body_rows = "".join(
            "<tr>" + "".join(f"<td>{v}</td>" for v in r.values()) + "</tr>"
            for r in embedded_rows[:50]
        )
        table_html = (
            f'<table id="dash-table"><thead><tr>{header_cells}</tr></thead>'
            f'<tbody>{body_rows}</tbody></table>'
        )
    else:
        _no_data_msg = (
            prose_summary
            or "No rows returned by the query. Ask the agent to relax the filter "
               "or pick a different source."
        )
        table_html = (
            f'<div class="empty">{_html.escape(_no_data_msg)}</div>'
        )

    has_chart = bool(chart_payload.get("labels"))
    chart_block = (
        f'<div class="chart-wrap"><canvas id="dash-chart" '
        f'aria-label="Chart of {safe_title}"></canvas></div>'
        if has_chart else ""
    )

    # Unified design tokens (P1.1) + data-driven chart type (P2.2).
    tv = _dashboard_theme_vars(theme)
    chart_type = _dashboard_chart_type(chart_payload.get("labels") or [])
    accent_rgb = tuple(int(tv["accent"].lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{safe_title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js"
        integrity="sha384-8dbf0940c6cca015338166ad7dee823800a2da58dc0dd650d8ec5ccc60376c635e59efba0c284c4c125e99e77b667bc9"
        crossorigin="anonymous"></script>
<style>
  :root {{ --bg: {tv["bg"]}; --panel: {tv["panel"]}; --ink: {tv["ink"]}; --muted: {tv["muted"]};
          --accent: {tv["accent"]}; --grid: {tv["grid"]}; --radius: 12px; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 24px; background: var(--bg); color: var(--ink);
         font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
               "Helvetica Neue", Arial, sans-serif; }}
  h1 {{ margin: 0 0 4px; font-size: 22px; font-weight: 600; }}
  .meta {{ color: var(--muted); font-size: 12px; margin-bottom: 18px; }}
  .kpis {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
          gap: 12px; margin-bottom: 18px; }}
  .kpi {{ background: var(--panel); border: 1px solid var(--grid); border-radius: var(--radius);
         padding: 14px 16px; }}
  .kpi-label {{ color: var(--muted); font-size: 12px; text-transform: uppercase;
               letter-spacing: 0.05em; }}
  .kpi-value {{ font-size: 22px; font-weight: 600; margin-top: 4px; }}
  .chart-wrap, .table-wrap {{ background: var(--panel); border: 1px solid var(--grid);
                             border-radius: var(--radius); padding: 16px; margin-bottom: 18px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--grid); }}
  th {{ color: var(--muted); font-weight: 500; cursor: pointer; user-select: none; }}
  tr:hover td {{ background: rgba(96,165,250,0.08); }}
  .empty {{ padding: 28px; text-align: center; color: var(--muted); }}
  .toolbar {{ display: flex; gap: 8px; align-items: center; margin-bottom: 10px;
             color: var(--muted); font-size: 12px; }}
  .toolbar input {{ background: var(--bg); color: var(--ink); border: 1px solid var(--grid);
                   border-radius: 6px; padding: 4px 8px; }}
  @media print {{ body {{ background: #fff; color: #000; }} .kpi, .chart-wrap, .table-wrap
                 {{ background: #fff; border-color: #ddd; }} }}
</style>
</head>
<body>
<h1>{safe_title}</h1>
<div class="meta">Source: {_html.escape(source_label)} · generated {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}</div>
<div class="kpis">{kpi_html}</div>
{chart_block}
<div class="table-wrap">
  <div class="toolbar">Filter: <input id="dash-filter" placeholder="Search rows…"></div>
  {table_html}
</div>
<script id="dashboard-data" type="application/json">{embedded_json}</script>
<script>
(function () {{
  const data = JSON.parse(document.getElementById('dashboard-data').textContent);
  const hasChart = {str(has_chart).lower()};
  if (hasChart && window.Chart) {{
    const ctx = document.getElementById('dash-chart').getContext('2d');
    const ACCENT = 'rgba({accent_rgb[0]},{accent_rgb[1]},{accent_rgb[2]},1)';
    const ACCENT_SOFT = 'rgba({accent_rgb[0]},{accent_rgb[1]},{accent_rgb[2]},0.6)';
    new Chart(ctx, {{
      type: '{chart_type}',
      data: {{
        labels: data.chart.labels,
        datasets: [{{ label: data.chart.value_col, data: data.chart.values,
                     backgroundColor: ACCENT_SOFT, borderColor: ACCENT, borderWidth: 1,
                     fill: {str(chart_type == "line").lower()}, tension: 0.25 }}]
      }},
      options: {{ responsive: true, plugins: {{ legend: {{ labels: {{ color: '{tv["ink"]}' }} }} }},
                  scales: {{ x: {{ ticks: {{ color: '{tv["muted"]}' }} }},
                             y: {{ ticks: {{ color: '{tv["muted"]}' }}, grid: {{ color: '{tv["grid"]}' }} }} }} }}
    }});
  }}
  const filter = document.getElementById('dash-filter');
  if (filter) {{
    filter.addEventListener('input', function () {{
      const q = this.value.toLowerCase();
      document.querySelectorAll('#dash-table tbody tr').forEach(function (tr) {{
        tr.style.display = tr.textContent.toLowerCase().indexOf(q) >= 0 ? '' : 'none';
      }});
    }});
  }}
  document.querySelectorAll('#dash-table th').forEach(function (th, idx) {{
    th.addEventListener('click', function () {{
      const tbody = th.closest('table').querySelector('tbody');
      const rows = Array.from(tbody.querySelectorAll('tr'));
      const asc = th.dataset.asc !== '1';
      rows.sort(function (a, b) {{
        const av = a.children[idx].textContent, bv = b.children[idx].textContent;
        const an = parseFloat(av), bn = parseFloat(bv);
        if (!isNaN(an) && !isNaN(bn)) return asc ? an - bn : bn - an;
        return asc ? av.localeCompare(bv) : bv.localeCompare(av);
      }});
      rows.forEach(function (r) {{ tbody.appendChild(r); }});
      th.dataset.asc = asc ? '1' : '0';
    }});
  }});
}})();
</script>
</body>
</html>
"""


async def ensure_artifact_for_doc_request(
    doc_format: Optional[FileFormat],
    assistant_content: str,
    already_created: list[dict],
    tool_calls_for_frontend: list[dict],
    db: Session,
    context: dict,
    artifact_ids: Optional[list] = None,
) -> Optional[dict]:
    """Guarantee an artifact exists when the user asked for a file.

    Only acts when ALL of the following hold:
      * ``doc_format`` is a renderable artifact type (docx/pptx/pdf/html);
      * no marker already produced an artifact (``already_created`` empty);
      * no successful ``create_artifact`` / ``run_sandbox_skill`` tool call
        is already recorded;
      * the data-path finalize did not already attach an artifact
        (``artifact_ids`` empty).

    Returns the created artifact result dict, or ``None`` if no fallback was
    needed or possible.
    """
    if not doc_format:
        return None
    artifact_type = _FORMAT_TO_ARTIFACT_TYPE.get(doc_format)
    if artifact_type is None:
        # xlsx / md / dashboard — not renderable by the current pipeline.
        logger.info(
            "orchestrator: doc_format=%s has no artifact renderer; skipping fallback",
            doc_format,
        )
        return None

    if already_created:
        if not settings.GOAL_CONTRACT_ENABLED:
            return None  # a marker already produced one (legacy)
        # Contract mode: only an artifact of the requested kind with a
        # non-empty, non-failed payload counts as satisfaction. A report card
        # or a failed artifact never satisfies an explicit doc deliverable.
        expected = _FORMAT_TO_ARTIFACT_TYPE.get(doc_format)
        if any(
            isinstance(a, dict)
            and bool(a.get("success"))
            and expected is not None
            and (a.get("type") or "").lower().strip() == expected
            for a in already_created
        ):
            return None
        # None matched — fall through so the user still gets the deliverable.

    if artifact_ids:
        if not settings.DOC_REQUEST_STRICT_ARTIFACT_MATCH_ENABLED:
            # Legacy behavior: the ask_data_agent finalize path (rich or
            # no-data) already produced an artifact this turn — creating
            # another would be a user-visible duplicate.
            return None
        # Strict mode: only artifacts whose stored type matches the
        # requested format count as satisfaction. A report card (html_report)
        # never satisfies an explicit pptx/docx/xlsx/dashboard request.
        if settings.GOAL_CONTRACT_ENABLED:
            # Contract mode adds the content gate: the payload must actually
            # exist (non-empty, non-failed build) — not just match by type.
            if any(
                _artifact_satisfies_deliverable(aid, doc_format, db)
                for aid in artifact_ids
            ):
                return None
        else:
            if any(
                _artifact_matches_requested_format(aid, doc_format, db)
                for aid in artifact_ids
            ):
                return None
        # None matched — fall through to the fallback so the user still gets
        # the deliverable they asked for.

    for tc in tool_calls_for_frontend or []:
        if tc.get("name") not in ("create_artifact", "run_sandbox_skill"):
            continue
        res = tc.get("results") or {}
        if isinstance(res, dict) and res.get("success") and res.get("artifact_id"):
            return None  # an artifact engine already made one

    # Dashboard fallback: mine ask_data_agent rows (if any) and synthesize a
    # real interactive HTML dashboard. This is the guarantee path: the user
    # asked for a dashboard, so the user gets one — even when the LLM
    # produced neither a DASHBOARD marker nor a create_artifact tool call.
    if doc_format == "dashboard":
        rows = _mine_ask_data_rows(tool_calls_for_frontend or [])
        title = _title_from_prose(assistant_content, fallback="Dashboard")
        # If prose is empty but rows are present, use a content-derived title.
        if not assistant_content.strip() and rows:
            cols = list(rows[0].keys()) if isinstance(rows[0], dict) else []
            title = f"Dashboard — {', '.join(cols[:3])}" if cols else "Dashboard"
        # P1.1/P1.2: resolve the tenant brand kit so the dashboard shares
        # the same design tokens as decks/docs (falls back to the legacy
        # dark palette when no kit is configured).
        dash_theme = None
        try:
            from app.services.artifacts.brand_kit import get_brand_kit
            from app.services.artifacts.exporters._theme import theme_from_brand_kit

            kit = get_brand_kit(
                db,
                org_id=(context or {}).get("org_id") or "default-org",
                app_id=(context or {}).get("app_id") or "default-app",
            )
            dash_theme = theme_from_brand_kit(kit) if kit else None
        except Exception as _theme_exc:
            logger.debug("orchestrator: brand kit resolution skipped: %s", _theme_exc)
        html_content = _synthesize_dashboard_html(
            title=title,
            rows=rows,
            prose_summary=_prose_to_summary(assistant_content, max_chars=400),
            source_label="ask_data_agent + orchestrator fallback" if rows else "orchestrator fallback",
            theme=dash_theme,
        )
        payload = {
            "title": title,
            "html_content": html_content,
            "source": "orchestrator-fallback",
            "user_signal": "dashboard",
            "row_count": len(rows),
        }
        try:
            result = await _create_artifact_tool(
                args={
                    "type": "html",
                    "title": title,
                    "payload": payload,
                    "skill": "dashboard",
                    "description": (
                        f"Auto-generated dashboard from {len(rows)} query row(s) "
                        "(orchestrator fallback)."
                    ),
                },
                db=db,
                context=context,
            )
            if isinstance(result, dict) and result.get("success"):
                logger.info(
                    "orchestrator: dashboard fallback created html artifact %s "
                    "(rows=%d, doc_format=%s)",
                    result.get("artifact_id"), len(rows), doc_format,
                )
                return result
            logger.warning(
                "orchestrator: dashboard fallback artifact creation failed: %s",
                (result or {}).get("error") if isinstance(result, dict) else result,
            )
        except Exception as exc:
            logger.warning(
                "orchestrator: dashboard fallback raised (non-fatal): %s", exc,
            )
        return None

    # Fallback: build the export payload data-first.
    #
    # Priority order:
    #   1. This turn's own synthesized report-card payload (richest — the
    #      in-chat card and the exported file then tell the same story);
    #   2. This turn's raw ask_data rows (KPIs + chart + the agent's own
    #      natural-language answer as the summary);
    #   3. The assistant's prose, with narrator chatter filtered out.
    #
    # The title prefers the turn payload, then the user's request, then
    # non-chatter prose — never "I'm going to build …" meta-sentences.
    user_message = (context or {}).get("user_message") or ""
    fallback_title = f"{doc_format}-export"
    # Fix 2a/3: only non-superseded ask_data_agent results shape the artifact
    # payload — a stale empty/error query (replaced by a later re-query on the
    # same bound KB) must never leak rows, citations, or methodology.
    data_result = _mine_ask_data_result(tool_calls_for_frontend or [], skip_superseded=True)

    payload: dict[str, Any] | None = None
    title: Optional[str] = None

    # Phase 1C: Enterprise pipeline short-circuit. When the
    # `collect_enterprise_data` tool produced a full EnterpriseReport
    # payload this turn, use IT as the artifact payload — the
    # ``docx_export.render`` will detect ``enterprise_report_kind ==
    # "executive"`` and delegate to ``render_enterprise_docx``,
    # producing a 6-section executive DOCX (cover, exec summary with
    # citations, KPI grid, breakdown, drivers, risks, actions, lineage
    # appendix) instead of a generic ReportCard.
    enterprise_payload = _mine_enterprise_payload(
        tool_calls_for_frontend or [], skip_superseded=True,
    )
    if isinstance(enterprise_payload, dict) and enterprise_payload:
        payload = dict(enterprise_payload)
        title = payload.get("title") or None
        logger.info(
            "orchestrator: using enterprise pipeline payload (title=%.60s, "
            "kind=%s)",
            (title or ""), payload.get("enterprise_report_kind"),
        )

    turn_rcp = (data_result or {}).get("report_card_payload")
    if isinstance(turn_rcp, dict) and turn_rcp and payload is None:
        # Reuse this turn's payload verbatim so the exported file mirrors
        # the in-chat report card (including an honest "no data" story).
        payload = dict(turn_rcp)
        title = payload.get("title") or None

    if payload is None:
        rows = _mine_ask_data_rows(tool_calls_for_frontend or [], skip_superseded=True)
        answer = ((data_result or {}).get("answer") or "").strip()
        summary = answer or _prose_to_summary(assistant_content)
        source_name = (data_result or {}).get("source_name") or ""
        sql = ((data_result or {}).get("sql") or "").strip()

        # FIX 2026-08-24: follow-up export turns ("give me in docx")
        # regularly run ZERO data queries — the model answers from
        # conversation context because the data is already there.  Reuse
        # the richest ask_data_agent result from earlier turns instead of
        # shipping a "no rows" warning file.
        if not rows:
            _hist_result = _mine_historical_ask_data_result(
                (context or {}).get("messages") or [],
                skip_superseded=True,
            )
            if _hist_result:
                _hrcp = _hist_result.get("report_card_payload")
                if isinstance(_hrcp, dict) and _hrcp:
                    # Reuse the earlier turn's synthesized payload verbatim so
                    # the exported file mirrors the in-chat report card.
                    payload = dict(_hrcp)
                    title = payload.get("title") or None
                    logger.info(
                        "orchestrator: export reusing historical RCP "
                        "(title=%.60s)", (title or ""),
                    )
                else:
                    _h_rows = _hist_result.get("rows")
                    if not isinstance(_h_rows, list):
                        _nested = _hist_result.get("data") or _hist_result.get("result")
                        if isinstance(_nested, dict):
                            _h_rows = _nested.get("rows")
                    if isinstance(_h_rows, list):
                        rows = [r for r in _h_rows if isinstance(r, dict)][:_DASHBOARD_MAX_ROWS]
                    answer = answer or ((_hist_result.get("answer") or "").strip())
                    source_name = source_name or (_hist_result.get("source_name") or "")
                    sql = sql or ((_hist_result.get("sql") or "").strip())
                    if rows:
                        logger.info(
                            "orchestrator: export reusing historical rows "
                            "(rows=%d, source=%s)", len(rows), source_name,
                        )

        # Apply the "assistant-reply" default only AFTER historical mining
        # so that historical source_name/sql take precedence.
        if not source_name:
            source_name = "assistant-reply"

        # ── Data quality gate ────────────────────────────────────────────
        # Before building the artifact payload, validate that the data is
        # business-meaningful (not just internal IDs or empty).
        # FIX 2026-08-24: skip the DQ gate when a historical RCP was reused —
        # it was already validated when originally synthesized in the earlier
        # turn.
        if payload is None:
            _dq = _validate_artifact_data_quality(rows, user_message)
            if not _dq["valid"]:
                logger.warning(
                    "orchestrator: artifact data quality check failed: %s "
                    "(rows=%d, reason=%s)",
                    user_message[:80], len(rows), _dq["reason"],
                )
                # Return a data-quality-warning artifact instead of garbage
                payload = {
                    "summary": (
                        f"Data quality issue detected: {_dq['reason']}. "
                        f"The query returned {len(rows)} rows but they contain "
                        f"only internal ID columns (no business metrics). "
                        f"Please re-run with a proper aggregation query."
                    ),
                    "source": source_name,
                    "user_signal": "export",
                    "methodology": (
                        f"Data sourced from {source_name} (SQL: {str(sql)[:80]}…) "
                        f"returning {len(rows)} rows — DATA QUALITY WARNING: "
                        f"{_dq['reason']}"
                    ),
                    "data_quality_warning": True,
                }
                # Don't add KPIs or charts for garbage data
                if sql:
                    payload["sql"] = sql
                if not title:
                    title = user_message or fallback_title
            else:
                # Fix 2b: methodology cites only the final non-superseded source
                # (name + clipped SQL + row count), never a merged list that could
                # include a superseded query's dead source.
                methodology = (
                    f"Data sourced from {source_name}"
                    + (f" (SQL: {str(sql)[:80]}\u2026)" if sql else "")
                    + f" returning {len(rows)} rows."
                )
                payload = {
                    "summary": summary,
                    "source": source_name,
                    "user_signal": "export",
                    "methodology": methodology,
                }
                if rows:
                    kpis = _rows_to_kpis(rows)
                    if kpis:
                        payload["kpis"] = kpis
                    label_col, value_col = _pick_chart_columns(rows)
                    if label_col and value_col:
                        payload["chart"] = {
                            "type": "bar",
                            "title": f"{value_col} by {label_col}",
                            "x_key": label_col,
                            "y_keys": [value_col],
                            "data": rows[:50],
                        }
                if sql:
                    payload["sql"] = sql

    if not title:
        title = _generate_meaningful_title(
            user_message,
            assistant_content=assistant_content,
            fallback=fallback_title,
        )
    payload["title"] = title
    if not payload.get("summary"):
        payload["summary"] = title
    payload.setdefault("user_signal", "export")

    try:
        result = await _create_artifact_tool(
            args={
                "type": artifact_type,
                "title": title,
                "payload": payload,
                "skill": str(doc_format),
                "description": "Auto-generated from assistant reply (orchestrator fallback).",
            },
            db=db,
            context=context,
        )
        if isinstance(result, dict) and result.get("success"):
            logger.info(
                "orchestrator: fallback created %s artifact %s (doc_format=%s)",
                artifact_type, result.get("artifact_id"), doc_format,
            )
            return result
        logger.warning(
            "orchestrator: fallback artifact creation failed: %s",
            (result or {}).get("error") if isinstance(result, dict) else result,
        )
    except Exception as exc:
        logger.warning("orchestrator: fallback artifact creation raised (non-fatal): %s", exc)
    return None


__all__ = [
    "fulfill_markers",
    "ensure_artifact_for_doc_request",
]
