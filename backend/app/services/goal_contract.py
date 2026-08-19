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
    ("docx", re.compile(r"\bdocx\b|word\s*document|word\s*doc|word文档|公文", re.IGNORECASE)),
    ("xlsx", re.compile(r"\bxlsx\b|\bxls\b|excel|电子表格|\b表格文件\b", re.IGNORECASE)),
    ("html", re.compile(r"\bhtml\b|\bhtm\b|web\s*page|网页", re.IGNORECASE)),
    ("md", re.compile(r"\bmarkdown\b|\.md\b|md文件", re.IGNORECASE)),
    ("pdf", re.compile(r"\bpdf\b|pdf文件", re.IGNORECASE)),
]

_DATA_QUESTION_RE = re.compile(
    r"(total|sum|average|avg|trend|forecast|sales|revenue|shipment|volume|"
    r"how\s+(many|much)|kpi|metrics?|breakdown|compare|statistics?|analysis|"
    r"数据|销量|销售额|趋势|预测|报表|统计|分析|多少|汇总)",
    re.IGNORECASE,
)

_PENDING_RE = re.compile(
    r"\b(let me|i['’]?ll|i will|i['’]m going to|i am going to|let['’]s|"
    r"i would like to|i['’]d like to|allow me to)\b",
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


def normalize_deliverable_intent(text: Optional[str]) -> Optional[str]:
    """Canonical deliverable kind requested by ``text``, or None.  Single
    typo-tolerant source of truth (dashboard first: fuzzy/typo-tolerant)."""
    if not text:
        return None
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
    code: str  # "deliverable" | "zero_rows" | "pending_action"
    message: str
    force_tool: Optional[str] = None


@dataclass
class GoalContract:
    deliverable: Optional[str] = None
    requires_data: bool = False
    expects_rows: bool = False
    text_filters_used: List[Tuple[str, str]] = field(default_factory=list)
    zero_row_events: int = 0
    artifacts_produced: List[dict] = field(default_factory=list)
    pending_action_phrase: Optional[str] = None
    forces_used: int = 0
    max_forces: int = 3
    catalog_meta: Optional[dict] = None
    distinct_executor: Optional[Callable[[str], Sequence[str]]] = None
    candidate_tables: List[str] = field(default_factory=list)
    table_executor: Optional[Callable[[str], str]] = None
    _announced_executed: bool = field(default=False, repr=False)

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

    def record_tool_executed(self, tool_name: Optional[str]) -> None:
        tool = (tool_name or "").lower()
        if tool in QUERY_TOOLS or tool in ARTIFACT_TOOLS or tool in DASHBOARD_TOOLS:
            self._announced_executed = True

    def record_force(self) -> None:
        self.forces_used += 1

    def refresh_pending_action(self, assistant_text: Optional[str]) -> None:
        """Re-evaluate the pending-action marker from the assistant's latest
        prose (the announce-but-don't-execute pattern only surfaces in the
        model's own text, not the user's). A fresh first-person-future marker
        overrides the turn-start value; past-tense closing statements leave
        the marker untouched (the announcement is still unexecuted)."""
        phrase = pending_action_phrase(assistant_text)
        if phrase:
            self.pending_action_phrase = phrase

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

    def _unmet_pending_action(self, tools: set) -> List[UnmetCriterion]:
        if self.pending_action_phrase and not self._announced_executed:
            tool = self._pick_query_tool(tools)
            if tool:
                msg = (
                    f"You announced: \"{self.pending_action_phrase}\" — execute it now "
                    f"with the `{tool}` tool instead of stopping."
                )
                return [UnmetCriterion("pending_action", msg, force_tool=tool)]
        return []

    def unmet(self, granted_tools: Optional[Iterable[str]] = None) -> List[UnmetCriterion]:
        """Unmet criteria at exit, priority: deliverable > zero-rows > pending.
        Returns [] when the force budget is exhausted."""
        if self.forces_used >= self.max_forces:
            return []
        tools = set(granted_tools or [])
        for fn in (self._unmet_deliverable, self._unmet_zero_rows, self._unmet_pending_action):
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
    return contract
