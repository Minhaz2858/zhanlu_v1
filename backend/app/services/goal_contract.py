"""Goal-Contract Architecture for the shared v3 agent loop.

A machine-checkable "goal contract" is built once per turn from the user's
message and updated at runtime from tool results. Before ANY loop exit the
contract is checked; unmet criteria trigger forced remediation steps
(artifact tool, re-query with actual distinct values, announced tool),
bounded by a force budget.

Constraints:
- Agent-agnostic and database-agnostic (no schema/vendor/language literals).
- ``normalize_deliverable_intent`` is the single typo-tolerant normalizer.
- Flag-off = current behavior: callers gate on settings.GOAL_CONTRACT_ENABLED.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple


# ── Intent normalization (single source of truth) ────────────────────────

_DASHBOARD_RE = re.compile(
    r"(dashbo|dash[- ]?board|dashbord|dahsboard|"
    r"看板|数据看板|仪表盘|仪表板|数据面板|大屏)",
    re.IGNORECASE,
)

_DELIVERABLE_PATTERNS: List[Tuple[str, "re.Pattern"]] = [
    ("pptx", re.compile(r"(?<![a-zA-Z])pptx?(?![a-zA-Z])|powerpoint|slide[-\s]?deck|演示文稿|幻灯片", re.IGNORECASE)),
    ("docx", re.compile(r"\bdocx?\b|word\s*document|word\s*doc|word文档|公文|文档", re.IGNORECASE)),
    ("xlsx", re.compile(r"\bxlsx\b|\bxls\b|excel|电子表格|\b表格文件\b", re.IGNORECASE)),
    ("html", re.compile(r"\bhtml\b|\bhtm\b|web\s*page|网页", re.IGNORECASE)),
    ("md", re.compile(r"\bmarkdown\b|\.md\b|md文件", re.IGNORECASE)),
    ("pdf", re.compile(r"\bpdf\b|pdf文件", re.IGNORECASE)),
]

_DATA_QUESTION_RE = re.compile(
    r"(total|sum|average|avg|trend|forecast|sales|revenue|shipment|volume|"
    r"how\s+(many|much)|kpi|metrics?|breakdown|compare|statistics?|analysis|"
    r"report|reports|review|performance|ranking|top\s*\d+|dashboard|"
    r"数据|销量|销售额|趋势|预测|报表|统计|分析|多少|汇总|报告|回顾|排行|排名)",
    re.IGNORECASE,
)

# 2026-08-26: report-style requests (the user explicitly asked for a
# "report", "review", "performance", etc.) require an EXTENSIVE written
# narrative around the data, not just the data card. Detected separately
# so we can inject a stronger directive than the standard data-turn one.
_REPORT_REQUEST_RE = re.compile(
    r"\b(report|reports|review|reviews|performance|dashboard|"
    r"breakdown|summary|monthly\s+report|weekly\s+report|"
    r"kpi|metrics|trends|analysis|ranking|top\s*\d+|"
    r"give\s+me|generate\s+a|write\s+a|prepare\s+a|show\s+me\s+the)\b"
    r"|(报告|报表|总结|回顾|分析报告|月度|周报|排行|表现|业绩)",
    re.IGNORECASE,
)


def is_report_request(user_message: str | None) -> bool:
    """True when the user is asking for a written report (not just data).

    Conservative: requires a report-related keyword. Pure data queries
    ("how many customers") are NOT flagged — only requests that look
    like they want a written analysis (report/review/summary/etc.).
    """
    return bool(_REPORT_REQUEST_RE.search(str(user_message or "")))

_PENDING_RE = re.compile(
    r"\b(let me|i['']?ll|i will|i['']m going to|i am going to|let['']s|"
    r"i would like to|i['']d like to|allow me to)\b",
    re.IGNORECASE,
)

_PENDING_OFFER_RE = re.compile(
    r"\b(let me know|feel free|don'?t hesitate|want me to)\b",
    re.IGNORECASE,
)

_PENDING_PAST_RE = re.compile(
    r"\b(already|checked|generated|finished|completed|found|ran|built|"
    r"did|updated|created|prepared)\b",
    re.IGNORECASE,
)

_GREETING_RE = re.compile(
    r"^\s*(hi|hello|hey|yo|hiya|greetings|good\s*(morning|afternoon|evening)"
    r"|how\s+are\s+you|nice\s+to\s+meet\s+you|thanks|thank\s+you|"
    r"你好|您好|早上好|下午好|晚上好|嗨|哈喽|在吗|谢谢)[\s!.,。！？]*$",
    re.IGNORECASE,
)

QUERY_TOOLS = frozenset(
    {"execute_query", "run_sql", "query_database", "execute_sql", "ask_data_agent"}
)
ARTIFACT_TOOLS = frozenset({"create_artifact", "run_sandbox_skill", "finalize_into_artifact"})
DASHBOARD_TOOLS = frozenset({"create_fullstack_dashboard", "create_dashboard"})

# Result-quality levels for GoalContract.record_tool_executed.
RESULT_QUALITY_ASSUMED_OK = "assumed_ok"  # default / unknown quality (backward compat)
RESULT_QUALITY_NO_DATA = "no_data"        # query returned no usable rows


def normalize_deliverable_intent(text: Optional[str]) -> Optional[str]:
    """Canonical deliverable kind requested by ``text``, or None.  Single
    typo-tolerant source of truth (dashboard first: fuzzy/typo-tolerant)."""
    if not text:
        return None
    # ── READ / ANALYZE guard (2026-08-31) ─────────────────────────────
    # Same discrimination as detect_file_intent: "read this docx" points at
    # an existing file — no deliverable kind is being requested, so return
    # None instead of routing to the format's creation skill.  Lazy import
    # avoids a module cycle (intent_router imports goal_contract lazily).
    try:
        from app.services.synexia.intent_router import is_file_read_request
        if is_file_read_request(text):
            return None
    except Exception:
        pass
    low = re.sub(r"\s+", " ", text.lower()).strip()
    if not low:
        return None
    if _DASHBOARD_RE.search(low):
        return "dashboard"
    for fmt, pat in _DELIVERABLE_PATTERNS:
        if pat.search(low):
            return fmt
    return None


def is_greeting(text: Optional[str]) -> bool:
    return bool(text and _GREETING_RE.match(text.strip()))


def looks_like_data_question(text: Optional[str]) -> bool:
    if not text:
        return False
    return bool(_DATA_QUESTION_RE.search(text))


def pending_action_phrase(text: Optional[str]) -> Optional[str]:
    """Conservative first-person future marker on the final sentence, or None.
    Past-tense / quoted text is excluded."""
    if not text or not text.strip():
        return None
    sentences = re.split(r"(?<=[.!?。！？])\s+", text.strip())
    last = sentences[-1].strip() if sentences else text.strip()
    if not last:
        return None
    # Exclude quoted material.
    if '"' in last:
        return None
    m = _PENDING_RE.search(last)
    if not m:
        return None
    if _PENDING_PAST_RE.search(last):
        return None
    if _PENDING_OFFER_RE.search(last):
        return None
    return last


def extract_text_filters(sql: Optional[str]) -> List[Tuple[str, str]]:
    """Extract (column, literal) pairs from text-filter predicates in ``sql``.
    Generic: LIKE '%x%' and = 'x' string literals only — no schema specifics."""
    if not sql:
        return []
    filters: List[Tuple[str, str]] = []
    like_re = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)\s+LIKE\s+'%([^']+)%'", re.IGNORECASE)
    eq_re = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*'([^']+)'", re.IGNORECASE)
    for col, lit in like_re.findall(sql):
        filters.append((col.lower(), lit.strip()))
    for col, lit in eq_re.findall(sql):
        if (col.lower(), lit.strip()) not in filters:
            filters.append((col.lower(), lit.strip()))
    return filters


def is_effective_empty(rows: Optional[Sequence[dict]]) -> bool:
    """True when ``rows`` carries no usable data: empty, OR every row is a
    header-only snapshot where every cell is None / "" / 0 (numeric zero).

    Conservative by design: ANY non-null, non-zero, non-empty value in ANY
    row means the result has signal. This intentionally does not grade
    quality — it only detects *clearly* empty payloads so the contract can
    treat "rows returned but all measures null/zero" like a zero-row query.
    """
    rows = rows or []
    if not rows:
        return True
    for row in rows:
        if not isinstance(row, dict):
            # Non-dict rows are unusual; assume signal so we never block on a
            # shape we cannot inspect.
            return False
        for val in row.values():
            if val is None:
                continue
            if isinstance(val, str) and val.strip() == "":
                continue
            if isinstance(val, (int, float)) and val == 0:
                continue
            # Numeric zero as a string ("0", "0.0") — common in CSV snapshots.
            if isinstance(val, str):
                try:
                    if float(val) == 0:
                        continue
                except ValueError:
                    pass
            return False
    return True


_METADATA_ONLY_COLUMN_RE = re.compile(
    r"^(?:"
    r"(?:min|max|count)"                    # bare word: count (psycopg2 unaliased)
    r"|(?:min|max|count)_[a-z0-9_]*"        # prefix: min_date, max_fdate, count_rows
    r"|[a-z0-9_]*_(?:min|max|count)"        # suffix: row_count, ship_date_min
    r"|(?:min|max|count)\s*\([^)]*\)"       # function: min(x), max(FDATE), count(*), count(1)
    r")$",
    re.IGNORECASE,
)


def is_metadata_only_rows(rows: Optional[Sequence[dict]]) -> bool:
    """True when ``rows`` has the "metadata-only" shape: 1-2 rows where EVERY
    column name is a MIN/MAX/COUNT aggregate (bare, ``min_*`` prefix, or
    ``min(...)`` function form).

    This catches `SELECT MIN(FDATE), MAX(FDATE) FROM t` — a query that only
    returned the data's date range / row count instead of business rows —
    without knowing what ``FDATE`` means (shape-only, no business content).

    Conservative by design (Rule 2): ALL columns must match the aggregate
    shape AND the row count must be <= 2. A result like
    ``{product_name: "X", total_revenue: 100}`` is never flagged, and any
    mixed aggregate + measure result is never flagged either.
    """
    rows = rows or []
    if not rows or len(rows) > 2:
        return False
    saw_column = False
    for row in rows:
        if not isinstance(row, dict):
            # Non-dict rows are unusual; assume signal so we never block on a
            # shape we cannot inspect (mirrors is_effective_empty).
            return False
        for col in row.keys():
            saw_column = True
            if not _METADATA_ONLY_COLUMN_RE.match(str(col).strip()):
                return False
    return saw_column


def extract_tables_from_sql(sql: Optional[str]) -> List[str]:
    """Table names referenced by ``FROM`` / ``JOIN`` clauses, in order,
    deduplicated case-insensitively. Schema-qualified names are reduced to
    the bare table name. Pure text extraction — no DB I/O."""
    if not sql:
        return []
    from_re = re.compile(
        r"\b(?:FROM|JOIN)\s+([a-zA-Z_][a-zA-Z0-9_.\"`]*)",
        re.IGNORECASE,
    )
    tables: List[str] = []
    for m in from_re.finditer(sql):
        name = m.group(1).strip().strip('"`')
        # Reduce schema.table → table (and strip trailing alias keywords if any).
        name = name.split(".")[-1].strip().strip('"`')
        name = re.split(r"\s+(?:AS\s+)?\w+$", name)[0].strip().strip('"`')
        if not name:
            continue
        if name.lower() not in (t.lower() for t in tables):
            tables.append(name)
    return tables


def catalog_oracle_feedback(
    filters: Sequence[Tuple[str, str]],
    tables: Sequence[str],
    catalog_meta: Optional[dict] = None,
    distinct_executor: Optional[Callable[[str], Sequence[str]]] = None,
    table_executor: Optional[Callable[[str], str]] = None,
    cap: int = 50,
) -> List[str]:
    """Unified remediation hints: distinct values for the filtered columns
    PLUS live table-coverage feedback (e.g. MAX(date), row counts) for the
    candidate tables the query referenced. Both sources are optional and
    degrade gracefully to empty when unavailable."""
    lines = list(
        distinct_values_feedback(
            filters,
            catalog_meta=catalog_meta,
            executor=distinct_executor,
            cap=cap,
        )
    )
    if not filters and tables and table_executor:
        for table in tables:
            try:
                cov = table_executor(table)
            except Exception:  # noqa: BLE001 — coverage is best-effort
                cov = ""
            if cov and str(cov).strip():
                lines.append(f'Table "{table}" live coverage: {str(cov).strip()}.')
    return lines


def distinct_values_feedback(
    filters: Sequence[Tuple[str, str]],
    catalog_meta: Optional[dict] = None,
    executor: Optional[Callable[[str], Sequence[str]]] = None,
    cap: int = 50,
) -> List[str]:
    """Human-readable lines listing actual distinct values for filtered
    columns.  Catalog ``value_samples`` first; live executor fallback."""
    lines: List[str] = []
    seen_cols = set()
    for col, literal in filters:
        if col in seen_cols:
            continue
        seen_cols.add(col)
        values: List[str] = []
        if catalog_meta and isinstance(catalog_meta, dict):
            samples = catalog_meta.get(col) or catalog_meta.get(col.lower())
            if samples:
                values = [str(v) for v in samples if str(v).strip()]
        if not values and executor:
            try:
                live = executor(col)
                if live:
                    values = [str(v) for v in live if str(v).strip()]
            except Exception:
                values = []
        if not values:
            continue
        values = values[:cap]
        joined = ", ".join(values)
        if len(joined) > 3500:
            joined = joined[:3500] + "…"
        lines.append(
            f'Column "{col}" (filtered with "{literal}") contains values like: {joined}.'
        )
    return lines


# ── GoalContract ─────────────────────────────────────────────────────────


@dataclass
class UnmetCriterion:
    code: str  # "deliverable" | "zero_rows" | "metadata_only" | "pending_action"
    message: str
    force_tool: Optional[str] = None
    force_synthesis: bool = False


@dataclass
class GoalContract:
    deliverable: Optional[str] = None
    requires_data: bool = False
    expects_rows: bool = False
    text_filters_used: List[Tuple[str, str]] = field(default_factory=list)
    zero_row_events: int = 0
    metadata_only_events: int = 0
    artifacts_produced: List[dict] = field(default_factory=list)
    collected_datasets: List[dict] = field(default_factory=list)
    pending_action_phrase: Optional[str] = None
    forces_used: int = 0
    max_forces: int = 3
    catalog_meta: Optional[dict] = None
    distinct_executor: Optional[Callable[[str], Sequence[str]]] = None
    candidate_tables: List[str] = field(default_factory=list)
    table_executor: Optional[Callable[[str], str]] = None
    _seq: int = field(default=0, repr=False)
    _armed_seq: int = field(default=0, repr=False)
    _executed_seq: int = field(default=0, repr=False)
    _armed_by: Optional[str] = field(default=None, repr=False)  # "user" | "model"
    _usable_results: int = field(default=0, repr=False)

    # ── runtime updates ──────────────────────────────────────────────────

    def record_artifact(self, kind: str, ok: bool, rows: Optional[int] = None) -> None:
        self.artifacts_produced.append({"kind": kind, "ok": bool(ok), "rows": rows})

    def record_query_result(self, rows: Optional[Sequence[dict]], sql: Optional[str] = None) -> None:
        if sql:
            for entry in extract_text_filters(sql):
                if entry not in self.text_filters_used:
                    self.text_filters_used.append(entry)
            for table in extract_tables_from_sql(sql):
                if table.lower() not in (t.lower() for t in self.candidate_tables):
                    self.candidate_tables.append(table)
        if is_effective_empty(rows):
            # Empty rows AND all-null/all-zero snapshots both count as
            # "no usable data" — a header-only result must not reset the
            # counter just because it returned 3 rows of None.
            self.zero_row_events += 1
        else:
            self.zero_row_events = 0
        if is_metadata_only_rows(rows):
            # A pure MIN/MAX/COUNT snapshot (date range / row count) is a
            # distinct degradation: it carries signal, so it is NOT counted as
            # empty, but it is also NOT business data — it gets its own
            # counter so the exit checker can force a real query.
            self.metadata_only_events += 1
        else:
            self.metadata_only_events = 0

    # ── deferred deliverable: dataset collection ─────────────────────────

    def record_dataset(
        self,
        *,
        rows: Optional[Sequence[dict]],
        sql: Optional[str] = None,
        source_name: Optional[str] = None,
        source_id: Optional[str] = None,
        purpose: Optional[str] = None,
        tool_call_id: Optional[str] = None,
    ) -> None:
        """Collect a query result during the loop.

        Only ``answer``-tagged datasets may later feed the deliverable;
        ``probe`` / ``auxiliary`` results are recorded for audit but never
        finalized. The deliverable is built ONCE post-loop from the
        answer-tagged set (deferred single emission).
        """
        self.collected_datasets.append(
            {
                "rows": list(rows or []),
                "sql": sql,
                "source_name": source_name,
                "source_id": source_id,
                "purpose": purpose,
                "tool_call_id": tool_call_id,
            }
        )

    def answer_datasets(self) -> List[dict]:
        """The answer-tagged datasets — the only ones eligible for the
        deliverable. Latest query wins on synthesis (later queries refine
        earlier ones); ordering matches record order."""
        return [d for d in self.collected_datasets if d.get("purpose") == "answer"]

    def has_answer_data(self) -> bool:
        """True when >= 1 answer-tagged dataset carries usable rows."""
        for d in self.answer_datasets():
            if not is_effective_empty(d.get("rows")):
                return True
        return False

    def collection_complete(self) -> bool:
        """Deterministic post-loop readiness: data criteria met AND >= 1
        answer-tagged dataset exists. Never derived from model prose.

        The deliverable criterion is intentionally NOT checked here — the
        artifact is deferred to the post-loop deliverable phase, so it does
        not exist yet when this runs. Structural (finalize moved post-loop)
        + data criteria + answer data = complete.
        """
        if self.requires_data:
            if not self.has_answer_data():
                return False
            if self.expects_rows and self.zero_row_events:
                return False
            if self.expects_rows and self.metadata_only_events:
                return False
        # An announced-but-unexecuted action means the agent still owes an
        # answer step before the deliverable can be considered collected.
        if self.pending_action_phrase and self._armed_seq > self._executed_seq:
            return False
        return True

    def record_tool_executed(
        self, tool_name: Optional[str], result_quality: str = RESULT_QUALITY_ASSUMED_OK
    ) -> None:
        """Record that ``tool_name`` ran.

        ``result_quality`` defaults to ``"assumed_ok"`` for backward compat.
        Pass ``RESULT_QUALITY_NO_DATA`` when the tool result carried no usable
        rows (empty / all-null / metadata-only): the announced action was NOT
        actually fulfilled, so ``_executed_seq`` stays unchanged and the
        pending-action remediation can still fire on exit.
        """
        tool = (tool_name or "").lower()
        if tool in QUERY_TOOLS or tool in ARTIFACT_TOOLS or tool in DASHBOARD_TOOLS:
            if result_quality != RESULT_QUALITY_NO_DATA:
                self._seq += 1
                self._executed_seq = self._seq
                self._usable_results += 1

    def record_force(self) -> None:
        self.forces_used += 1

    def refresh_pending_action(self, assistant_text: Optional[str]) -> None:
        """Re-evaluate the pending-action marker from the assistant's latest
        prose (the announce-but-don't-execute pattern only surfaces in the
        model's own text, not the user's). A fresh first-person-future marker
        overrides the turn-start value; past-tense closing statements leave
        the marker untouched (the announcement is still unexecuted).

        Sequence-stamp semantics: arming stamps _armed_seq; if the model
        produces clean prose (no pending marker) after a model-armed phrase,
        the phrase is disarmed (prevents force loops after successful forced
        synthesis). User-armed phrases are never disarmed by prose.
        """
        phrase = pending_action_phrase(assistant_text)
        if phrase:
            if phrase != self.pending_action_phrase:
                self._seq += 1
                self._armed_seq = self._seq
                self._armed_by = "model"
            self.pending_action_phrase = phrase
        else:
            # No pending phrase in the model's prose.
            if self._armed_by == "model":
                # Model chose to deliver; disarm to prevent force loops.
                self.pending_action_phrase = None
                self._armed_by = None

    # ── exit checker ─────────────────────────────────────────────────────

    def _deliverable_produced(self) -> bool:
        if not self.deliverable:
            return False
        for art in self.artifacts_produced:
            if art.get("kind") == self.deliverable and art.get("ok"):
                rows = art.get("rows")
                if rows is None or rows > 0:
                    return True
        return False

    def _force_tool_for_deliverable(self, tools: set) -> Optional[str]:
        if self.deliverable == "dashboard":
            for t in ("create_fullstack_dashboard", "create_dashboard"):
                if t in tools:
                    return t
            if "create_artifact" in tools:
                return "create_artifact"
            return None
        if "create_artifact" in tools:
            return "create_artifact"
        if "run_sandbox_skill" in tools:
            return "run_sandbox_skill"
        return None

    @staticmethod
    def _pick_query_tool(tools: set) -> Optional[str]:
        for t in ("execute_query", "ask_data_agent", "run_sql", "execute_sql", "query_database"):
            if t in tools:
                return t
        return None

    def _unmet_deliverable(self, tools: set) -> List[UnmetCriterion]:
        if self.deliverable and not self._deliverable_produced():
            tool = self._force_tool_for_deliverable(tools)
            if tool:
                msg = (
                    f"The user asked for a {self.deliverable} deliverable, but no matching "
                    f"artifact was produced yet. Use the `{tool}` tool now to create it."
                )
                return [UnmetCriterion("deliverable", msg, force_tool=tool)]
        return []

    def _unmet_zero_rows(self, tools: set) -> List[UnmetCriterion]:
        if not (self.requires_data and self.expects_rows and self.zero_row_events):
            return []
        tool = self._pick_query_tool(tools)
        if not tool:
            return []
        hints = catalog_oracle_feedback(
            self.text_filters_used,
            self.candidate_tables,
            catalog_meta=self.catalog_meta,
            distinct_executor=self.distinct_executor,
            table_executor=self.table_executor,
        )
        hint_text = " ".join(hints) if hints else ""
        if self.text_filters_used:
            literals = ", ".join(f'"{lit}"' for _col, lit in self.text_filters_used)
            if not hint_text:
                hint_text = "The text filter may not match any stored value."
            msg = (
                f"A query with text filter(s) {literals} returned zero usable rows. "
                f"{hint_text} "
                f"Re-run the query with a value from that list (or drop the text filter)."
            )
        else:
            if not hint_text:
                hint_text = (
                    "Verify the data source: the queried table may be stale, empty, "
                    "or missing the requested time period."
                )
            msg = (
                "A data query returned zero usable rows (empty or all-null/all-zero "
                f"snapshot). {hint_text} "
                "Re-run the query against a live table (e.g. probe MAX(date) per "
                "candidate table) to confirm what data actually exists before building."
            )
        return [UnmetCriterion("zero_rows", msg, force_tool=tool)]

    def _unmet_metadata_only(self, tools: set) -> List[UnmetCriterion]:
        if not (self.requires_data and self.expects_rows and self.metadata_only_events):
            return []
        tool = self._pick_query_tool(tools)
        if not tool:
            return []
        msg = (
            "A data query returned only metadata (date range / row-count "
            "aggregates such as MIN/MAX/COUNT) without business data rows. "
            f"Re-run the query with the `{tool}` tool returning actual "
            "dimension + measure rows (SELECT the business columns directly "
            "instead of wrapping them in MIN/MAX/COUNT)."
        )
        return [UnmetCriterion("metadata_only", msg, force_tool=tool)]

    def _unmet_pending_action(self, tools: set) -> List[UnmetCriterion]:
        if not self.pending_action_phrase:
            return []
        # Announcement-scoped: the promise fires iff it was armed AFTER the
        # last qualifying execution. This fixes the turn-scoped bug where
        # _announced_executed was set True by an EARLIER query.
        if self._armed_seq <= self._executed_seq:
            return []
        # When usable data already exists, force synthesis (answer with
        # existing data) instead of a wasteful re-query.
        if self._usable_results > 0:
            msg = (
                f"You announced: \"{self.pending_action_phrase}\" — but you "
                f"already have the data. Write the final answer now using the "
                f"retrieved data. Do not announce future actions."
            )
            return [UnmetCriterion("pending_action", msg, force_tool=None, force_synthesis=True)]
        tool = self._pick_query_tool(tools)
        if tool:
            msg = (
                f"You announced: \"{self.pending_action_phrase}\" — execute it now "
                f"with the `{tool}` tool instead of stopping."
            )
            return [UnmetCriterion("pending_action", msg, force_tool=tool)]
        return []

    def unmet(self, granted_tools: Optional[Iterable[str]] = None) -> List[UnmetCriterion]:
        """Unmet criteria at exit, priority: deliverable > zero-rows >
        metadata-only > pending.  Returns [] when the force budget is
        exhausted."""
        if self.forces_used >= self.max_forces:
            return []
        tools = set(granted_tools or [])
        for fn in (
            self._unmet_deliverable,
            self._unmet_zero_rows,
            self._unmet_metadata_only,
            self._unmet_pending_action,
        ):
            crits = fn(tools)
            if crits:
                return crits
        return []

    def satisfied(self, granted_tools: Optional[Iterable[str]] = None) -> bool:
        return not self.unmet(granted_tools)


def build_goal_contract(
    user_content: Optional[str],
    agent_config: Optional[dict] = None,
    catalog_meta: Optional[dict] = None,
    distinct_executor: Optional[Callable[[str], Sequence[str]]] = None,
    table_executor: Optional[Callable[[str], str]] = None,
    max_forces: int = 3,
) -> GoalContract:
    """Build the GoalContract for a turn.  Greetings yield an empty contract
    (trivially satisfied → immediate text answer, no forcing)."""
    text = (user_content or "").strip()
    contract = GoalContract(
        max_forces=max_forces,
        catalog_meta=catalog_meta,
        distinct_executor=distinct_executor,
        table_executor=table_executor,
    )
    if is_greeting(text):
        return contract
    contract.deliverable = normalize_deliverable_intent(text)
    if contract.deliverable:
        contract.requires_data = True
        contract.expects_rows = True
    else:
        contract.requires_data = looks_like_data_question(text)
        contract.expects_rows = contract.requires_data
    contract.pending_action_phrase = pending_action_phrase(text)
    if contract.pending_action_phrase:
        contract._seq += 1
        contract._armed_seq = contract._seq
        contract._armed_by = "user"
    return contract
