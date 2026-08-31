"""Universal Self-Evaluation & Re-Planning verification gate.

Pure, TOTAL module (never raises) that runs at the synthesis boundary of the
agent loop. It combines:

1. Deterministic detectors over summarized tool results + the draft answer
   (metadata-only answers, empty results, degenerate values, lexical dimension
   coverage, placeholder non-answers). Zero domain knowledge: everything is
   lexical/structural — no hardcoded table or column names.
2. A single optional LLM strict-inspector call (one per turn, at the synthesis
   boundary only) that applies the 4-check rubric and returns JSON.

Any LLM failure/timeout falls back to the deterministic verdict. When re-plan
attempts are exhausted or the finish line is near, an INCOMPLETE verdict is
escalated to IMPOSSIBLE so the loop discloses gaps instead of nudging again.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.config import settings
from app.services.goal_contract import pending_action_phrase

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VERIFICATION_PROMPT = """\
You are a strict response inspector. Verify whether the assistant's draft answer
completely satisfies the user's request using this 4-point checklist:

1. Completeness — every dimension/measure the user asked for (prices, volumes,
   counts, categories, dates, etc.) is actually present in the answer with real
   values, not just mentioned.
2. Quality — the answer contains real data (numbers, names, facts) rather than
   metadata, schema listings, row counts, or placeholder boilerplate.
3. Source coverage — the answer is grounded in the available tool results and
   does not invent data the tools never returned.
4. Plausibility — numbers are internally consistent and not obviously fabricated.

User request:
{user_message}

Tool results (summarized):
{tool_results_json}

Draft answer:
{assistant_text}

Respond with STRICT JSON only, no prose, in exactly this shape:
{{"status": "COMPLETE"|"INCOMPLETE"|"IMPOSSIBLE", "gaps": ["..."], "suggested_fix": "..."}}
- COMPLETE: the answer fully satisfies the request.
- INCOMPLETE: some requested dimension or quality bar is missing and it is worth
  trying an alternative approach.
- IMPOSSIBLE: the data cannot be obtained with any reasonable alternative
  approach (use sparingly; only when no re-plan could possibly help).
"""

# Placeholder / non-answer phrasing. Matches the "I had trouble putting it
# together" / "I found 42,993 materials across 6 tables" family of answers.
_PLACEHOLDER_PATTERNS = [
    re.compile(r"trouble putt\w+ it (all )?together", re.IGNORECASE),
    re.compile(r"across\s+\d+[\d,]*\s+(tables|sources|databases|documents|columns)", re.IGNORECASE),
    re.compile(r"found\s+\d+[\d,]*\s+(materials|records|rows|items|products|documents|entries)", re.IGNORECASE),
    re.compile(r"i gathered some information", re.IGNORECASE),
    re.compile(r"had difficulty (answering|finding|retrieving)", re.IGNORECASE),
    re.compile(r"could not (find|locate|retrieve) (the )?(data|information|numbers)", re.IGNORECASE),
    re.compile(r"no data (was|is|were) (found|returned|available)", re.IGNORECASE),
]

# Tokens that look like requested "aspects" but are generic query/quantifier
# words — excluded from lexical dimension coverage to cut false positives.
_ASPECT_STOPWORDS = frozenset({
    "latest", "current", "average", "total", "overall", "summary", "daily",
    "monthly", "weekly", "yearly", "annual", "first", "last", "top", "bottom",
    "list", "show", "find", "give", "need", "want", "please", "tell", "answer",
    "data", "info", "information", "query", "sql", "table", "database", "all",
    "any", "every", "much", "many", "how", "what", "which", "when", "where",
    "why", "who", "etc", "kind", "type", "value", "values", "number", "amount",
    "count", "status", "detail", "details", "trend", "trends", "breakdown",
    "split", "sample", "full", "new", "old", "please", "help", "result",
    "results", "report", "reports", "metric", "metrics", "figure", "figures",
    # Automation task metadata — describe the TASK or its DELIVERY, not a data
    # dimension. The "Daily Sales Data Sync" prompt contains words like
    # "incremental", "anomaly", "alerts", "outcome" that previously triggered
    # phantom missing-dimension flags, nudging the model to re-emit the entire
    # report (2026-08-20).
    "outcome", "incremental", "anomaly", "anomalies", "alert", "alerts",
    "alerting", "successful", "success", "failure", "failures", "error",
    "errors", "ok", "pending", "running", "scheduled",
})

# Generic container/category words — not content dimensions. Excluded so a
# request like "for each material" doesn't false-positive when the answer
# enumerates materials by name without repeating the word "material".
_CONTAINER_STOPWORDS = frozenset({
    "material", "materials", "product", "products", "item", "items",
    "record", "records", "entry", "entries", "row", "rows", "column",
    "columns", "field", "fields", "doc", "docs", "document", "documents",
    "file", "files", "source", "sources", "table", "tables", "category",
    "categories", "group", "groups", "section", "sections", "name", "names",
    "title", "titles", "label", "labels",
})

# Action verbs that describe the REQUEST, not a data dimension. "make/show the
# sales by region" asks for a breakdown — the verb itself is never a dimension
# the results must enumerate (Fix 1a: cuts "make a sales overview PPT" phantom
# flags on the verb).
_VERB_STOPWORDS = frozenset({
    "make", "create", "show", "give", "build", "generate", "tell", "produce",
    "provide", "run", "running", "sync", "syncing", "synced", "automate",
    "automating", "automated", "schedule", "scheduled", "scheduling",
    "execute", "executing", "trigger", "triggered", "triggering",
    "write", "pull", "fetch", "get", "prepare", "compile",
    "compute", "display", "present", "render", "draft", "deliver", "output",
    "list", "find", "need", "want", "please", "help", "answer", "explain",
    "summarize", "analyze", "calculate", "count",
})

# Output-format / deliverable words — the container of the answer, not a
# content dimension. "make a sales overview PPT" must not flag "ppt".
_FORMAT_STOPWORDS = frozenset({
    "ppt", "pptx", "excel", "xlsx", "xls", "csv", "dashboard", "html", "file",
    "deck", "report", "overview", "summary", "presentation", "slide", "slides",
    "document", "docx", "pdf", "spreadsheet", "chart", "charts", "graph",
    "graphs", "table", "tables", "export", "attachment",
    # Format-adjacent words that appear in automation format guidance (e.g.
    # "Markdown table inside the HTML report") and in user prompts ("create a
    # web page", "deliverable is the artifact"). Previously passed through and
    # were flagged as missing dimensions (2026-08-20).
    "markdown", "web", "page", "pages", "artifact", "artifacts", "deliverable",
    "deliverables", "format",
})

# Time-window words — the query's temporal scope, not a dimension that results
# must enumerate. "for last month" must not flag "month" (Fix 1a).
_TIME_STOPWORDS = frozenset({
    "month", "months", "last", "year", "years", "week", "weeks", "daily",
    "weekly", "monthly", "yearly", "annual", "quarter", "quarters", "quarterly",
    "recent", "yesterday", "today", "tomorrow", "date", "dates", "period",
    "time", "now", "current", "latest",
})

# Automation-task metadata words. The "Run Automation Task" prompt template
# embeds runtime descriptors ("Data Sync", "Output format: Web page (html)",
# "incremental updates", "anomaly alerts", "running") that the dimension
# detector previously flagged as missing dimensions, triggering a re-iteration
# that duplicated the entire report (2026-08-20). These are NEVER data
# dimensions in any catalog. Note: most of these are also caught by
# _ASPECT_STOPWORDS / _VERB_STOPWORDS / _FORMAT_STOPWORDS via the -ing
# normalization fix; this set is a second-line filter for the specific
# automation-context words that don't fit those categories.
_AUTOMATION_STOPWORDS = frozenset({
    "automation", "automated", "task", "sync", "pipeline",
    "schedule", "scheduled", "cron", "trigger",
    "incremental", "delta", "deltas", "update", "updates",
    "alert", "alerts", "alerting", "anomaly", "anomalies",
    "outcome", "successful", "failure",
    # From the live automation wrapper template ("Run Automation Task:"):
    # these wrapper words describe the task envelope, not data dimensions.
    "data_sync", "type", "name", "description", "project",
    "business", "appropriate", "track", "watch", "use",
    "erp",
})

# Rebuttal detection (Fix 1c). When the assistant explicitly rebuts a
# candidate dimension ("no such column in the catalog", "not available"),
# the token is NOT a phantom flag — the model already explained why the
# dimension cannot be returned. Suppresses only the rebutted tokens; it
# never invents new flags.
_REBUTTAL_RE = re.compile(
    r"\b(not\s+in\s+(?:the\s+)?catalog|no\s+such\s+(?:dimension|column|field|attribute)"
    r"|(?:doesn'?t|does\s+not)\s+exist|not\s+available|not\s+present|none\s+of)\b",
    re.IGNORECASE,
)
_REBUTTAL_WINDOW_CHARS = 40


def _is_rebutted(missing_tok: str, assistant_text: str) -> bool:
    """True when ``assistant_text`` contains a rebuttal pattern within a short
    window around ``missing_tok`` (case-insensitive).

    The window keeps the match tight: "no such column" two sentences away from
    the token is not a rebuttal of that token. Only proximity counts.
    """
    if not missing_tok or not assistant_text:
        return False
    text_l = assistant_text.lower()
    tok_l = missing_tok.lower()
    for m in _REBUTTAL_RE.finditer(text_l):
        start = max(0, m.start() - _REBUTTAL_WINDOW_CHARS)
        end = min(len(text_l), m.end() + _REBUTTAL_WINDOW_CHARS)
        if tok_l in text_l[start:end]:
            return True
    return False


# Overscope detection (Entity Master Filter). User asks for a specific subset
# (category/type/quantity/qualified noun) but the tool results look like a
# full-table dump of a fact table with no filtering evidence.
_SCOPE_QUALIFIER_RE = re.compile(
    r"(category|categories|type(?:s)?\b|segments?|grades?|classes?|groups?|"
    r"kinds?|variants?|subset|specific|particular|only|just|"
    r"top\s+\d+|first\s+\d+|list\s+\d+|exact\s+\d+)",
    re.IGNORECASE,
)
_SCOPE_QUALIFIED_NOUN_RE = re.compile(
    r"\b\w[\w\-/.]+\s+(product|material|item|customer|client|supplier|vendor|"
    r"warehouse|region|employee|user)s?\b",
    re.IGNORECASE,
)
_OVERSCOPE_ROW_THRESHOLD = 200

# Name-role column detection (structural — mirrors catalog_indexer's boundary +
# suffix-anchored patterns). No vendor prefixes, no hardcoded names.
_NAME_ROLE_RE = re.compile(
    r"(^|_)(name|fname|title|label)(_|$)|(name|title|label)$",
    re.IGNORECASE,
)

# Total-labeled cell detection (generic EN + CJK, no domain vocabulary).
# A label counts as a total when it carries one of these tokens; CJK is
# matched verbatim (no word boundary exists for CJK), ASCII tokens are
# boundary-anchored so "sum" cannot match "assumption".
_TOTAL_LABEL_RE = re.compile(
    r"(合计|总计|总收入|总金额|总额|"
    r"(?<![a-z0-9])(total|grand\s*total|overall|sum)(?![a-z0-9]))",
    re.IGNORECASE,
)

# "stated total" extraction from the draft answer: "<total-token> ... <number>"
# within a short window (e.g. "Total sales was 1,100 dollars").
_STATED_TOTAL_RE = re.compile(
    r"(?:total|合计|总计|总收入|总金额|总额)[^\d\n]{0,80}?"
    r"([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)

# id-role column detection — id columns are never measures, so plausibility
# detectors skip them (prevents sequential-id false positives).
_ID_COL_RE = re.compile(r"(^|_)(id|ids)(_|$)", re.IGNORECASE)

_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_]{2,}")
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "this", "that", "from", "have", "has", "are",
    "was", "were", "will", "would", "should", "could", "can", "you", "your",
    "their", "them", "they", "there", "here", "these", "those", "what", "which",
    "when", "where", "why", "who", "how", "not", "but", "our", "its", "also",
    "into", "about", "each", "both", "more", "most", "than", "then", "upon",
    "over", "under", "such", "only", "just", "out", "per", "via", "using",
    "used", "based", "according", "across", "between", "through", "during",
})


def _normalize_token(tok: str) -> str:
    t = tok.lower().strip("_")
    if t.endswith("ies"):
        t = t[:-3] + "y"
    elif t.endswith("ss"):
        pass
    elif t.endswith("ing") and len(t) > 5:
        # Strip -ing suffix so inflected verbs ("running", "syncing") map to
        # their stem ("run", "sync") and hit the stopword sets. Only strip
        # when the stem is >= 3 chars to avoid over-stripping ("king" → "k").
        stem = t[:-3]
        # Undo doubled-final-consonant pattern: running → runn → run,
        # swimming → swimmi → swim, getting → getti → get. If the stem has
        # a doubled consonant (last char == second-to-last), drop one.
        if len(stem) >= 3 and stem[-1] == stem[-2]:
            t = stem[:-1]
        else:
            t = stem
    elif t.endswith("s") and len(t) > 3 and not t.endswith("ss"):
        # Strip plural -s but preserve words ending in -ss (class, business,
        # status) and short words (≤ 3 chars: "as", "is", "us"). Pre-existing
        # behaviour (2026-08-20): "runs" → "run", "tables" → "table".
        t = t[:-1]
    return t


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class VerificationResult:
    """Verdict of the verification gate."""

    status: str = "COMPLETE"  # COMPLETE | INCOMPLETE | IMPOSSIBLE
    gaps: list[str] = field(default_factory=list)
    suggested_fix: str = ""
    signals: list[str] = field(default_factory=list)  # detector names that fired
    source: str = "deterministic"  # deterministic | llm | heuristic


# ---------------------------------------------------------------------------
# Summarizer (loop gate should pass summarized payloads, never raw dumps)
# ---------------------------------------------------------------------------


def summarize_tool_result(raw: dict | None, max_rows: int = 5, max_chars: int = 1200) -> dict:
    """Compact a raw tool result dict into a detector-friendly summary."""
    out: dict[str, Any] = {
        "tool": "",
        "columns": None,
        "rows": [],
        "row_count": 0,
        "empty": False,
        "text": "",
        "sql": "",
    }
    if not isinstance(raw, dict):
        return out
    out["tool"] = str(
        raw.get("tool") or raw.get("tool_name") or raw.get("name") or ""
    )
    rows = raw.get("rows")
    if rows is None:
        rows = raw.get("data")
    if isinstance(rows, list):
        out["empty"] = len(rows) == 0
        out["rows"] = rows[:max_rows]
    # Honor an explicit total (the raw result may carry the full dump size
    # even when `rows` is truncated to a sample) — this is what lets the
    # overscope detector see a 42k-record dump whose preview shows 5 rows.
    rc = raw.get("row_count")
    if not isinstance(rc, int):
        rc = raw.get("total")
    if not isinstance(rc, int):
        rc = len(rows) if isinstance(rows, list) else 0
    out["row_count"] = rc
    columns = raw.get("columns") or raw.get("column_names")
    if columns is None and out["rows"] and isinstance(out["rows"][0], dict):
        columns = list(out["rows"][0].keys())
    if isinstance(columns, list):
        out["columns"] = [str(c) for c in columns]
    txt = raw.get("text") or raw.get("content") or raw.get("summary") or raw.get("result")
    if isinstance(txt, str) and txt:
        out["text"] = txt[:max_chars]
    sql = raw.get("sql")
    if isinstance(sql, str) and sql:
        out["sql"] = sql[:500]
    return out


def summarize_tool_results(raw_list: list[dict] | None) -> list[dict]:
    if not raw_list:
        return []
    return [summarize_tool_result(r) for r in raw_list]


# ---------------------------------------------------------------------------
# Deterministic detectors
# ---------------------------------------------------------------------------


def _detect_metadata_only(results: list[dict]) -> bool:
    """Schema/catalog-shaped results (columns but zero data rows)."""
    if not results:
        return False
    for r in results:
        if r.get("columns") and r.get("row_count", 0) == 0:
            return True
    return False


def _detect_empty_results(results: list[dict]) -> bool:
    """Every tool result came back empty (0 rows, no text payload)."""
    if not results:
        return False
    return all(r.get("empty") and not r.get("text") for r in results)


def _iter_row_values(row: Any):
    if isinstance(row, dict):
        yield from row.values()
    elif isinstance(row, (list, tuple)):
        yield from row


def _detect_degenerate_values(results: list[dict]) -> bool:
    """Rows exist but every value is NULL/empty/zero."""
    for r in results:
        rows = r.get("rows") or []
        if not rows:
            continue
        values = [v for row in rows for v in _iter_row_values(row)]
        if not values:
            continue
        non_degenerate = [
            v for v in values
            if v is not None and v != "" and v != 0 and v != "0" and v != 0.0
        ]
        if not non_degenerate:
            return True
    return False


def _detect_placeholder_text(assistant_text: str) -> bool:
    if not assistant_text:
        return False
    return any(p.search(assistant_text) for p in _PLACEHOLDER_PATTERNS)


def _payload_text(results: list[dict]) -> str:
    parts: list[str] = []
    for r in results:
        if r.get("columns"):
            parts.append(" ".join(r["columns"]))
        for row in r.get("rows") or []:
            if isinstance(row, dict):
                parts.append(" ".join(f"{k} {v}" for k, v in row.items()))
            elif isinstance(row, (list, tuple)):
                parts.append(" ".join(str(v) for v in row))
        if r.get("text"):
            parts.append(r["text"])
        if r.get("sql"):
            parts.append(r["sql"])
    return " ".join(parts)


def _detect_dimension_coverage(
    user_message: str, results: list[dict], assistant_text: str,
    *,
    catalog_meta: dict | None = None,
) -> list[str]:
    """Return user-requested aspect tokens absent from tool results + answer.

    ``catalog_meta`` (optional) maps catalog column name -> list of sample
    values, e.g. ``{"region": ["east", "west"]}``. When provided, a candidate
    token is only kept if it maps to an actual catalog column name
    (case-insensitive) OR appears inside a column's ``value_samples`` —
    unmappable tokens (usually hallucinated or non-data words) are silently
    dropped instead of phantom-flagging the turn.
    """
    if not results or not user_message:
        return []
    requested = {
        _normalize_token(tok)
        for tok in _TOKEN_RE.findall(user_message)
        if tok.lower() not in _STOPWORDS
        and _normalize_token(tok) not in _ASPECT_STOPWORDS
        and _normalize_token(tok) not in _CONTAINER_STOPWORDS
        and _normalize_token(tok) not in _VERB_STOPWORDS
        and _normalize_token(tok) not in _FORMAT_STOPWORDS
        and _normalize_token(tok) not in _TIME_STOPWORDS
        and _normalize_token(tok) not in _AUTOMATION_STOPWORDS
    }
    if not requested:
        return []
    if catalog_meta:
        requested = {
            tok for tok in requested
            if _maps_to_catalog(tok, catalog_meta)
        }
        if not requested:
            return []
    corpus = _normalize_token(_payload_text(results) + " " + (assistant_text or ""))
    missing = [tok for tok in sorted(requested) if tok not in corpus]
    return missing


def _maps_to_catalog(token: str, catalog_meta: dict) -> bool:
    """True when ``token`` names a catalog column or appears in value_samples."""
    token_l = token.lower()
    for col_name, samples in catalog_meta.items():
        if str(col_name).lower() == token_l:
            return True
        for s in samples or []:
            if s is None:
                continue
            if token_l == str(s).strip().lower():
                return True
    return False


def _is_blank_value(value: Any) -> bool:
    """Blank = None | "" | whitespace-only string. Numeric zero is NOT blank."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def _detect_blank_dimension_columns(results: list[dict]) -> list[str]:
    """Return the FIRST name-role column per result that is 100% blank/NULL
    across all summarized rows (None | "" | whitespace only).

    Only the first (primary) name-role column of a result is checked — a later
    secondary blank name column does not fire (avoids over-triggering). Numeric
    columns never fire because they are not name-role. Returns [] when nothing
    qualifies.
    """
    fired: list[str] = []
    for r in results:
        rows = r.get("rows") or []
        # Real QueryService payloads (main loop + data-agent sub-loop) carry
        # rows dicts with NO "columns" key — derive from the first row when
        # absent, otherwise the detector is dead code in production.
        columns = r.get("columns") or (
            list(rows[0].keys()) if rows and isinstance(rows[0], dict) else []
        )
        if not columns or not rows:
            continue
        name_cols = [c for c in columns if _NAME_ROLE_RE.search(str(c))]
        if not name_cols:
            continue
        primary = name_cols[0]
        try:
            idx = columns.index(primary)
        except ValueError:
            continue
        values: list[Any] = []
        for row in rows:
            if isinstance(row, dict):
                values.append(row.get(primary))
            elif isinstance(row, (list, tuple)) and idx < len(row):
                values.append(row[idx])
            else:
                values.append(None)
        if not values:
            continue
        if all(_is_blank_value(v) for v in values):
            fired.append(primary)
            break  # one firing result is enough to flag the whole answer
    return fired


def _detect_overscope_filter(user_message: str, results: list[dict]) -> bool:
    """Detect unfiltered fact-table dumps despite a subset-specific request.

    Structural only — zero domain keywords, zero hardcoded table/column names.
    Fires when ALL of the following hold:

      1. The user asks for a specific subset: a category/type/segment word, a
         quantity limit ("top N", "only", "just"), or a qualified noun
         ("C5/C9 products" — slash/hyphen qualifier + entity noun).
      2. At least one tool result returned a large row count (>= 200 rows) —
         the shape of a full fact-table scan rather than a scoped answer.
      3. No tool result's SQL shows filtering evidence (WHERE / JOIN / IN(...)),
         so the large dump was NOT deliberately scoped.

    This is the verification-gate enforcement of the Entity Master Filter
    pattern: when the agent answers a category-specific question by dumping the
    whole fact table, the gate flags it INCOMPLETE and nudges a master-first
    re-plan.
    """
    if not user_message or not results:
        return False
    has_scope_signal = bool(
        _SCOPE_QUALIFIER_RE.search(user_message)
        or _SCOPE_QUALIFIED_NOUN_RE.search(user_message)
    )
    if not has_scope_signal:
        return False
    has_large_dump = any(
        (r.get("row_count") or 0) >= _OVERSCOPE_ROW_THRESHOLD for r in results
    )
    if not has_large_dump:
        return False
    # Give the benefit of the doubt when ANY executed SQL shows scoping.
    for r in results:
        sql = r.get("sql", "")
        if sql and re.search(r"\b(WHERE|JOIN|IN\s*\()", sql, re.IGNORECASE):
            return False
    return True


# ---------------------------------------------------------------------------
# LLM strict-inspector (one call max; TOTAL via try/except)
# ---------------------------------------------------------------------------


def _sync_llm_call(prompt: str) -> str:
    """Run the configured LLM synchronously (safe to call inside to_thread)."""
    from app.services.llm_service import call_llm

    messages = [
        {"role": "system", "content": "You are a strict response inspector. Reply with JSON only."},
        {"role": "user", "content": prompt},
    ]
    result = call_llm(prompt=prompt, messages=messages, temperature=0, task_type="simple_chat")
    if not asyncio.iscoroutine(result):
        return result
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(result)
    finally:
        try:
            loop.close()
        except Exception:
            pass


def _parse_eval_json(raw: str) -> tuple[str, list[str], str] | None:
    text = raw.strip()
    if "{" not in text:
        return None
    text = text[text.index("{"):]
    if "}" in text:
        text = text[: text.rindex("}") + 1]
    try:
        payload = json.loads(text)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    status = str(payload.get("status", "")).upper()
    if status not in {"COMPLETE", "INCOMPLETE", "IMPOSSIBLE"}:
        return None
    gaps_raw = payload.get("gaps") or []
    gaps = [str(g) for g in gaps_raw if isinstance(g, str)][:6]
    fix = str(payload.get("suggested_fix") or "")[:300]
    return status, gaps, fix


def _run_llm_eval(
    user_message: str, results: list[dict], assistant_text: str
) -> tuple[str, list[str], str]:
    """One strict-inspector call. Raises on failure; caller falls back."""
    compact = []
    for r in results[:6]:
        item: dict[str, Any] = {"tool": r.get("tool", "")}
        if r.get("columns"):
            item["columns"] = r["columns"]
        if r.get("rows"):
            item["rows"] = r["rows"]
        if r.get("row_count") is not None:
            item["row_count"] = r["row_count"]
        if r.get("text"):
            item["text"] = r["text"][:400]
        compact.append(item)
    prompt = _VERIFICATION_PROMPT.format(
        user_message=user_message[:2000],
        tool_results_json=json.dumps(compact, ensure_ascii=False)[:2000],
        assistant_text=(assistant_text or "")[:4000],
    )
    result = _sync_llm_call(prompt)
    if isinstance(result, dict):
        raw = result.get("response", "") or ""
    else:
        raw = str(result or "")
    parsed = _parse_eval_json(raw)
    if parsed is None:
        raise ValueError("unparseable evaluator JSON")
    return parsed


# ---------------------------------------------------------------------------
# Hybrid entry point (TOTAL — never raises)
# ---------------------------------------------------------------------------


def evaluate_answer(
    user_message: str,
    tool_results: list[dict],
    assistant_text: str,
    *,
    attempts: int = 0,
    budget_remaining: int = 100,
    endpoint: str | None = None,
    catalog_meta: dict | None = None,
) -> VerificationResult:
    """Run the verification gate.

    Deterministic detectors first; if any signal fires the verdict is decided
    locally (with attempts/budget escalation to IMPOSSIBLE) and the LLM call is
    skipped. Otherwise one optional LLM strict-inspector call runs at the
    synthesis boundary. Any failure falls back to the deterministic verdict.
    """
    try:
        return _evaluate_answer_inner(
            user_message or "",
            tool_results or [],
            assistant_text or "",
            attempts=attempts,
            budget_remaining=budget_remaining,
            endpoint=endpoint,
            catalog_meta=catalog_meta,
        )
    except Exception as exc:  # TOTAL invariant
        logger.exception("answer_verification gate raised (non-fatal): %s", exc)
        return VerificationResult(status="COMPLETE", source="heuristic")


# ---------------------------------------------------------------------------
# Plausibility detectors (Fix 2 — part-vs-whole + cross-call total drift)
# ---------------------------------------------------------------------------


def _to_number(value: Any) -> float | None:
    """Best-effort numeric coercion (int/float/string with commas)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace(",", ""))
        except ValueError:
            return None
    return None


def _row_label_parts(row: dict) -> list[str]:
    """String cell values of a row — the label candidates for total detection."""
    return [str(v) for v in row.values() if isinstance(v, str)]


def _is_total_label(label: str) -> bool:
    return bool(_TOTAL_LABEL_RE.search(label))


def _detect_part_whole_inconsistency(
    results: list[dict], assistant_text: str
) -> list[str]:
    """Return ``["part_whole"]`` when a breakdown part (or the parts-sum)
    exceeds a total-labeled value by >2% — either within one result set (a
    "合计/total" row vs its sibling rows) or against a total stated in the
    draft answer. Flag-gated by ``ANSWER_PLAUSIBILITY_CHECK_ENABLED``.
    """
    if not getattr(settings, "ANSWER_PLAUSIBILITY_CHECK_ENABLED", False):
        return []
    parts_sums: list[float] = []
    for r in results:
        rows = r.get("rows") or []
        if not rows or not isinstance(rows[0], dict):
            continue
        columns = r.get("columns") or list(rows[0].keys())
        for col in columns:
            # id-role columns are never measures — skip (cuts stated-total
            # false positives on sequential ids).
            if _ID_COL_RE.search(str(col)):
                continue
            totals: list[float] = []
            parts: list[float] = []
            for row in rows:
                if not isinstance(row, dict) or col not in row:
                    continue
                num = _to_number(row.get(col))
                if num is None:
                    continue
                if any(_is_total_label(l) for l in _row_label_parts(row)):
                    totals.append(num)
                else:
                    parts.append(num)
            if totals:
                total = max(totals)
                if parts and (sum(parts) > total * 1.02 or max(parts) > total * 1.02):
                    return ["part_whole"]
            elif parts:
                # No in-result total for this column — the parts may still
                # contradict a total stated in the draft answer.
                parts_sums.append(sum(parts))
    if parts_sums:
        # Cross-check against totals stated in the draft answer.
        for m in _STATED_TOTAL_RE.finditer(assistant_text or ""):
            try:
                stated = float(m.group(1).replace(",", ""))
            except ValueError:
                continue
            if any(ps > stated * 1.02 for ps in parts_sums):
                return ["part_whole"]
    return []


def _detect_total_drift(results: list[dict]) -> list[str]:
    """Return ``["total_drift"]`` when two same-scope total-labeled values in
    this turn's results differ by >10% (same column name = same scope).
    Flag-gated by ``ANSWER_PLAUSIBILITY_CHECK_ENABLED``.
    """
    if not getattr(settings, "ANSWER_PLAUSIBILITY_CHECK_ENABLED", False):
        return []
    totals: list[tuple[str, float]] = []
    for r in results:
        rows = r.get("rows") or []
        if not rows or not isinstance(rows[0], dict):
            continue
        columns = r.get("columns") or list(rows[0].keys())
        for col in columns:
            for row in rows:
                if not isinstance(row, dict) or col not in row:
                    continue
                num = _to_number(row.get(col))
                if num is None:
                    continue
                if any(_is_total_label(l) for l in _row_label_parts(row)):
                    totals.append((col, num))
    if len(totals) < 2:
        return []
    for i in range(len(totals)):
        for j in range(i + 1, len(totals)):
            if totals[i][0] != totals[j][0]:
                continue
            a, b = totals[i][1], totals[j][1]
            if a == 0 or b == 0:
                continue
            if max(a, b) / min(a, b) > 1.10:
                return ["total_drift"]
    return []


# ---------------------------------------------------------------------------
# D4 (2026-08-20): category-subset coverage check
# ---------------------------------------------------------------------------
# The system prompt HARD RULE says to use unified tables for category /
# portfolio questions. LLMs can ignore prompt rules, so this deterministic
# post-hoc check catches the case: when the user asks about a whole category
# and the query results only cover a proper subset of the catalog's known
# members (per-product view instead of the unified table), flag INCOMPLETE.

_CATEGORY_REQUEST_RE = re.compile(
    r"\b(all|every|each|whole|full|entire|portfolio|family|category|"
    r"combined|consolidated|overview|complete|supply chain|dashboard|"
    r"product line|product lines)\b",
    re.IGNORECASE,
)


def _column_present_in_results(column: str, results: list[dict]) -> bool:
    """Case-insensitive presence check of a catalog column in the results."""
    lowered = column.lower()
    for r in results:
        for col in r.get("columns") or []:
            if str(col).lower() == lowered:
                return True
        rows = r.get("rows") or []
        if rows and isinstance(rows[0], dict):
            for key in rows[0].keys():
                if str(key).lower() == lowered:
                    return True
    return False


def _covered_member_values(column: str, results: list[dict]) -> set[str]:
    """Distinct, non-empty cell values for ``column`` across all results."""
    lowered = column.lower()
    covered: set[str] = set()
    for r in results:
        columns = [str(c) for c in (r.get("columns") or [])]
        idx = next(
            (i for i, c in enumerate(columns) if c.lower() == lowered), None
        )
        for row in r.get("rows") or []:
            if isinstance(row, dict):
                for key, val in row.items():
                    if str(key).lower() == lowered and val is not None:
                        s = str(val).strip()
                        if s:
                            covered.add(s)
            elif idx is not None and isinstance(row, (list, tuple)) and idx < len(row):
                s = str(row[idx]).strip()
                if s:
                    covered.add(s)
    return covered


def _detect_category_subset(
    user_message: str,
    results: list[dict],
    assistant_text: str,
    catalog_meta: dict | None = None,
) -> list[str]:
    """Return ``["category_subset"]`` when the user asked about a category /
    portfolio and the query results only cover a PROPER SUBSET of the
    catalog's known members for that category (from ``catalog_meta`` column
    sample_values — the member-enumerating name columns).

    Flag-gated by ``CATEGORY_SUBSET_CHECK_ENABLED`` (default False).
    """
    if not getattr(settings, "CATEGORY_SUBSET_CHECK_ENABLED", False):
        return []
    if not user_message or not results or not catalog_meta:
        return []
    lowered = user_message.lower()
    if not _CATEGORY_REQUEST_RE.search(lowered):
        return []
    for col_name, samples in catalog_meta.items():
        known = {
            str(s).strip().lower()
            for s in (samples or [])
            if s is not None and str(s).strip()
        }
        if len(known) < 2:
            continue  # not a member-enumerating category column
        if not _column_present_in_results(str(col_name), results):
            continue
        covered = {v.lower() for v in _covered_member_values(str(col_name), results)}
        if not covered:
            continue
        if covered < known:  # proper subset → partial portfolio coverage
            return ["category_subset"]
    return []


# ---------------------------------------------------------------------------
# D5 (2026-08-20): deterministic arithmetic-consistency gate
# ---------------------------------------------------------------------------
# Extract stated arithmetic claims from the draft answer, recompute each, and
# flag mismatches >2%. Pure regex + float math — no LLM call. Flag-gated by
# ARITHMETIC_CONSISTENCY_ENABLED (default False).

_ARITH_SUBTRACT_RE = re.compile(
    r"(\d[\d,.]*)\s+of\s+(\d[\d,.]*)\s+([^.;!?\n]{0,80}?)\s+leaving\s+(\d[\d,.]*)",
    re.IGNORECASE,
)
_ARITH_ADDITION_RE = re.compile(
    r"(\d[\d,.]*)\s*\+\s*(\d[\d,.]*)\s*=\s*(\d[\d,.]*)", re.IGNORECASE,
)
_ARITH_TOTAL_RE = re.compile(
    r"(\d[\d,.]*)\s+and\s+(\d[\d,.]*)\s+(?:total|add up to|sum to)\s+(\d[\d,.]*)",
    re.IGNORECASE,
)
_ARITH_PERCENT_OF_RE = re.compile(
    r"(\d[\d,.]*)\s*%\s+of\s+(\d[\d,.]*)\s*(?:is|equals|=)\s*(\d[\d,.]*)",
    re.IGNORECASE,
)
_ARITH_RATIO_MONTHS_RE = re.compile(
    r"(\d[\d,.]*)\s*(?:tons?|units?|items?|kg)?\s*[≈~]\s*(\d[\d,.]*)\s*months?",
    re.IGNORECASE,
)
_ARITH_RATE_RE = re.compile(
    r"(\d[\d,.]*)\s*(?:tons?|units?|items?|kg)?\s*(?:per\s+month|/month|a\s+month)",
    re.IGNORECASE,
)

_ARITH_MAX_REL_DIFF = 0.02


def _arith_num(text: str) -> float | None:
    try:
        return float(text.replace(",", ""))
    except (TypeError, ValueError):
        return None


def _detect_arithmetic_inconsistency(assistant_text: str) -> list[str]:
    """Return ``["arithmetic_inconsistency"]`` when any stated arithmetic
    claim in the draft answer contradicts its own recomputation by >2%.

    Verifies: subtraction ("A of B ... leaving C"), addition ("A + B = C",
    "A and B total C"), percentage ("A% of B is C") and coverage ratio
    ("A tons ≈ N months" against a stated "R per month" run rate).
    """
    if not getattr(settings, "ARITHMETIC_CONSISTENCY_ENABLED", False):
        return []
    if not assistant_text:
        return []

    run_rate: float | None = None
    rate_match = _ARITH_RATE_RE.search(assistant_text)
    if rate_match:
        run_rate = _arith_num(rate_match.group(1))

    claims: list[tuple[float, float, str]] = []  # (recomputed, claimed, label)

    for m in _ARITH_SUBTRACT_RE.finditer(assistant_text):
        x, y, z = (_arith_num(m.group(i)) for i in (1, 2, 4))
        if x is not None and y is not None and z is not None:
            claims.append((y - x, z, "subtraction"))

    for m in _ARITH_ADDITION_RE.finditer(assistant_text):
        x, y, z = (_arith_num(m.group(i)) for i in (1, 2, 3))
        if x is not None and y is not None and z is not None:
            claims.append((x + y, z, "addition"))

    for m in _ARITH_TOTAL_RE.finditer(assistant_text):
        x, y, z = (_arith_num(m.group(i)) for i in (1, 2, 3))
        if x is not None and y is not None and z is not None:
            claims.append((x + y, z, "addition"))

    for m in _ARITH_PERCENT_OF_RE.finditer(assistant_text):
        pct, base, claimed = (_arith_num(m.group(i)) for i in (1, 2, 3))
        if pct is not None and base is not None and claimed is not None:
            claims.append((pct / 100.0 * base, claimed, "percentage"))

    if run_rate and run_rate > 0:
        for m in _ARITH_RATIO_MONTHS_RE.finditer(assistant_text):
            qty, months = _arith_num(m.group(1)), _arith_num(m.group(2))
            if qty is not None and months is not None and months > 0:
                claims.append((qty / run_rate, months, "ratio"))

    for recomputed, claimed, _label in claims:
        denom = max(abs(recomputed), abs(claimed), 1e-9)
        if denom > 0 and abs(recomputed - claimed) / denom > _ARITH_MAX_REL_DIFF:
            return ["arithmetic_inconsistency"]
    return []


def _evaluate_answer_inner(
    user_message: str,
    tool_results: list[dict],
    assistant_text: str,
    *,
    attempts: int,
    budget_remaining: int,
    endpoint: str | None,
    catalog_meta: dict | None = None,
) -> VerificationResult:
    if not getattr(settings, "SELF_EVAL_REPLAN_ENABLED", False):
        return VerificationResult(status="COMPLETE", signals=["disabled"])

    results = summarize_tool_results(tool_results) if tool_results else []
    if not results:
        return VerificationResult(status="COMPLETE")

    signals: list[str] = []
    gaps: list[str] = []

    if _detect_metadata_only(results):
        signals.append("metadata")
        gaps.append("the query returned schema metadata but no data rows")
    if _detect_empty_results(results):
        signals.append("empty")
        gaps.append("all data source queries returned zero results")
    if _detect_degenerate_values(results):
        signals.append("degenerate")
        gaps.append("the returned values are all empty or zero")
    if _detect_placeholder_text(assistant_text):
        signals.append("placeholder")
        gaps.append("the draft answer is placeholder text, not real data")

    if pending_action_phrase(assistant_text):
        signals.append("pending_action")
        gaps.append(
            "the draft answer announces a future action instead of answering "
            "with the retrieved data"
        )

    missing = _detect_dimension_coverage(
        user_message, results, assistant_text, catalog_meta=catalog_meta,
    )
    # Fix 1c: suppress dimensions the assistant already rebutted (e.g. "no
    # such column"). Rebuttal only removes; it never adds new flags.
    if missing:
        missing = [tok for tok in missing if not _is_rebutted(tok, assistant_text)]
    if missing:
        signals.append("coverage")
        gaps.append(
            "requested dimensions not found in the data: " + ", ".join(missing[:5])
        )

    if _detect_overscope_filter(user_message, results):
        signals.append("overscope")
        gaps.append(
            "the query returned a large unfiltered dump from a fact table even "
            "though the request asked for a specific subset (category/type/quantity)"
        )

    # D4 (2026-08-20): category/portfolio requests must cover the WHOLE
    # category. Per-product/per-category views that only cover a subset of the
    # catalog's known members are flagged and nudged back to the unified table.
    if _detect_category_subset(
        user_message, results, assistant_text, catalog_meta=catalog_meta,
    ):
        signals.append("category_subset")
        gaps.append(
            "the query results only cover a subset of the category's known "
            "members even though the request asked about the whole category "
            "(portfolio/family) — re-query against the unified table that "
            "aggregates all members instead of a per-product/per-category view"
        )

    # D5 (2026-08-20): stated arithmetic claims must recompute correctly
    # (catches e.g. "5,565 of 11,028 delivered, leaving 6,183 outstanding" —
    # 11,028 − 5,565 = 5,463 ≠ 6,183, or "121.31 tons ≈ 2.3 months" when the
    # run rate implies ≈0.32 months).
    if _detect_arithmetic_inconsistency(assistant_text):
        signals.append("arithmetic_inconsistency")
        gaps.append(
            "the draft answer contains an arithmetic claim that contradicts "
            "its own numbers — recompute it from the tool results before "
            "presenting"
        )

    blank_cols = _detect_blank_dimension_columns(results)
    if blank_cols:
        signals.append("blank_dimension")
        gaps.append(
            "primary dimension column '{}' is 100% blank/NULL".format(blank_cols[0])
        )
        logger.info("answer_verification: blank_dimension on column %s", blank_cols[0])

    # Fix 2: numeric plausibility — a part exceeding its total, or two
    # same-scope totals contradicting each other, can never be a correct answer.
    if _detect_part_whole_inconsistency(results, assistant_text):
        signals.append("part_whole")
        gaps.append(
            "a breakdown part (or the sum of parts) exceeds the stated total "
            "by more than 2% — the numbers cannot all be correct"
        )
    if _detect_total_drift(results):
        signals.append("total_drift")
        gaps.append(
            "two same-scope period totals in this turn's query results differ "
            "by more than 10% — the numbers are internally contradictory"
        )

    max_replans = int(getattr(settings, "SELF_EVAL_MAX_REPLANS", 3))

    # 2026-08-25: pending_action downgrade. When the ONLY signal is
    # `pending_action` (the model ended with "Let me...") AND the agent
    # has produced substantive prose (>= SELF_EVAL_DATA_SUFFICIENT_MIN_PROSE
    # chars) AND there is at least one non-empty tool result, treat the
    # answer as COMPLETE. The trailing promise phrase will be stripped
    # post-loop by `pending_action_phrase` cleanup in agents.py
    # (lines ~12746-12762), so re-iterating to force a new answer is
    # unnecessary and only causes the "collapse" UX.
    if signals == ["pending_action"]:
        _min_prose = int(
            getattr(settings, "SELF_EVAL_DATA_SUFFICIENT_MIN_PROSE", 200)
        )
        _has_data = any(
            not (r.get("empty") and not r.get("text"))
            for r in (results or [])
        )
        if (
            _has_data
            and len((assistant_text or "").strip()) >= _min_prose
        ):
            logger.info(
                "answer_verification: pending_action-only signal downgraded to "
                "COMPLETE (prose=%d chars, has_data=True). Trailing promise "
                "phrase will be stripped post-loop.",
                len((assistant_text or "").strip()),
            )
            return VerificationResult(
                status="COMPLETE",
                source="deterministic",
                signals=["pending_action_downgraded"],
            )

    if signals:
        # Re-plan is only possible if we still have attempts and budget.
        if attempts >= max_replans or budget_remaining < 2:
            result = VerificationResult(
                status="IMPOSSIBLE",
                gaps=gaps,
                signals=signals,
                suggested_fix=_suggested_fix(signals, results),
                source="deterministic",
            )
        else:
            result = VerificationResult(
                status="INCOMPLETE",
                gaps=gaps,
                signals=signals,
                suggested_fix=_suggested_fix(signals, results),
                source="deterministic",
            )
        return result

    # Deterministic verdict is clean → optional LLM strict-inspector at the
    # synthesis boundary (one call per turn).
    llm_enabled = getattr(settings, "SELF_EVAL_LLM_GATE_ENABLED", True)
    if not llm_enabled:
        return VerificationResult(status="COMPLETE", source="deterministic")
    try:
        status, llm_gaps, fix = _run_llm_eval(user_message, results, assistant_text)
    except Exception as exc:
        logger.warning("answer_verification: llm eval failed (non-fatal): %s", exc)
        return VerificationResult(status="COMPLETE", source="deterministic")

    if status == "COMPLETE":
        return VerificationResult(status="COMPLETE", source="llm")

    gaps = llm_gaps or ["the answer does not fully satisfy the request"]
    if status == "IMPOSSIBLE" or attempts >= max_replans or budget_remaining < 2:
        return VerificationResult(
            status="IMPOSSIBLE", gaps=gaps, suggested_fix=fix,
            signals=["llm"], source="llm",
        )
    return VerificationResult(
        status="INCOMPLETE", gaps=gaps, suggested_fix=fix,
        signals=["llm"], source="llm",
    )


# ---------------------------------------------------------------------------
# "Try instead" guidance keyed by observed tool shape (never tool names)
# ---------------------------------------------------------------------------


def _suggested_fix(signals: list[str], results: list[dict]) -> str:
    hint = _infer_source_hint(results)
    if "empty" in signals:
        return (
            "Retry with a different data source or query shape (e.g. widen the "
            f"date range, drop filters, or try a {hint} alternative); then answer with the actual result."
        )
    if "metadata" in signals:
        return (
            "The schema is not data. Run a real data query that returns rows "
            f"of actual values from the {hint} source, then answer from those values."
        )
    if "degenerate" in signals:
        return (
            "The rows are empty/zero-filled. Re-plan with a different query "
            f"(different filters, columns, or {hint} source) and return real values."
        )
    if "placeholder" in signals:
        return (
            "Do not summarize row counts or schemas as the answer. Extract the "
            "requested values from the tool results and present them directly."
        )
    if "blank_dimension" in signals:
        return (
            "The primary dimension column returned 100% blank/NULL values. "
            "Identify the entity master table connected via this table's FK/id "
            "column in the schema graph (tagged table_role: entity_master), JOIN "
            "it, and re-query using the master's name column so the answer shows "
            "real entity names. This fix is deterministic from the schema graph — "
            "do not ask the user for permission."
        )
    if "coverage" in signals:
        return (
            "Query the columns that actually contain the requested dimensions "
            f"from the {hint} source, then answer with those values."
        )
    if "overscope" in signals:
        return (
            "The query scanned the whole fact table without entity filtering. "
            "Identify the entity master table for the requested entity (low row "
            "count, id+name+category columns, tagged table_role: entity_master), "
            "filter it to the relevant entity IDs, then re-query the fact table "
            "with WHERE <entity_id> IN (...) before answering."
        )
    if "category_subset" in signals:
        return (
            "The query results only cover a subset of the category's known "
            "members (e.g. a per-product/per-category view) even though the "
            "request asked about the whole category/portfolio. Re-query against "
            "the unified table that aggregates ALL members of the category, then "
            "answer with the complete portfolio picture (do not report a partial "
            "set as if it were the full category)."
        )
    if "arithmetic_inconsistency" in signals:
        return (
            "Recompute the arithmetic in the draft answer from the tool results "
            "before presenting: verify 'A of B leaving C' with C = B − A, "
            "'A% of B' with A% × B, and 'N months of cover' with N = quantity ÷ "
            "monthly run rate. Correct any number that does not reconcile; do "
            "not report unverified arithmetic."
        )
    if "part_whole" in signals or "total_drift" in signals:
        return (
            "Re-query with one source-of-truth aggregation and verify sums "
            "before presenting; if the numbers are still contradictory, "
            "disclose the discrepancy to the user."
        )
    return "Try an alternative approach to obtain the missing information."


def _infer_source_hint(results: list[dict]) -> str:
    """Structural (not name-based) inference of the dominant source type."""
    if any(r.get("columns") is not None or r.get("sql") for r in results):
        return "SQL/database"
    if any(r.get("text") for r in results):
        return "document/search"
    return "data source"


# ---------------------------------------------------------------------------
# Nudge / disclosure builders
# ---------------------------------------------------------------------------


def build_replan_nudge(result: VerificationResult) -> str:
    """One-sentence gap + alternative guidance injected into the loop."""
    if result.status == "COMPLETE":
        return ""
    gap_line = "; ".join(result.gaps) if result.gaps else (
        "the draft answer does not answer the user's request"
    )
    fix = result.suggested_fix or (
        "Try an alternative approach to obtain the missing information."
    )
    return (
        "VERIFICATION GAP (self-evaluation): the draft answer has not fully "
        f"addressed the request. Gap: {gap_line}. {fix} "
        "Re-plan and try again with an alternative approach before answering."
    )


def build_gap_disclosure(result: VerificationResult) -> str:
    """Suffix appended to best-effort answers when re-planning is impossible."""
    if result.status == "COMPLETE":
        return ""
    gap_line = "; ".join(result.gaps) if result.gaps else (
        "some requested information could not be retrieved"
    )
    fix = result.suggested_fix or (
        "Please rephrase the request or provide additional details."
    )
    return (
        "\n\n[Gap disclosure] I was unable to fully answer this request: "
        f"{gap_line}. {fix}"
    )
