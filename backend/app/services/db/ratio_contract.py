"""Ratio metric contracts: a ratio is only publishable when both sides are measured on the same basis.

The measured failure this module exists for: a fulfilment-rate query ran
``SUM(out_fqty) / COUNT(DISTINCT FID)`` — a shipped QUANTITY divided by a count
of contract DOCUMENTS — and returned ≈17,957%. The SQL executed, the column
existed, the value was non-zero, so every guard passed and the figure was
published as a percentage.

A ratio is not a number the data source can vouch for by itself: it is a CLAIM
about two measurements being commensurable. So the claim is made explicit — a
:class:`RatioContract` records both sides, the grain each is measured at, their
units, the window, the dimensions it is valid over, what a zero denominator
means, and the range the metric is allowed to occupy — and then checked before
the SQL is written and again before the figure is published.

Three rules this module follows:

* **Grain is inferred from the aggregation, not from a table name.** ``SUM(x)``
  is a quantity measured per row; ``COUNT(*)`` is a count of rows;
  ``COUNT(DISTINCT key)`` is a count of entities. Dividing across those families
  is the defect, wherever the columns come from.
* **Nothing is clamped.** A ratio above 100% may be entirely legitimate (a
  coverage index, a cumulative factor, a ratio whose contract declares it), so a
  large value is a question to be *proven*, never a number to be trimmed.
* **Unknown is not valid.** A ratio whose grain, units or window cannot be
  established is reported as unverified, not as a confident percentage.

The contract extends the platform's existing metric vocabulary
(``semantic_store.GlossaryTerm`` / ``project_metric``) rather than duplicating
it: :func:`contract_from_term` reads the sides from the metric's own
``sql_expression``/``definition``/``bindings`` and falls back to inference, and
:func:`contract_from_expressions` builds the same object from SQL the model
actually wrote. One contract type, two readers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

# ── grains ─────────────────────────────────────────────────────────────────

#: How a side is MEASURED. Two sides of a ratio must be measured the same way.
GRAIN_ROW_COUNT = "row_count"  # COUNT(*) — rows/documents
GRAIN_ENTITY_COUNT = "entity_count"  # COUNT(DISTINCT key) — distinct entities
GRAIN_QUANTITY = "quantity"  # SUM/AVG of a quantity measure (tons, pieces)
GRAIN_AMOUNT = "amount"  # SUM/AVG of a money measure
GRAIN_RATE = "rate"  # a ratio itself
GRAIN_UNKNOWN = "unknown"

GRAINS = {
    GRAIN_ROW_COUNT: "a count of rows/documents",
    GRAIN_ENTITY_COUNT: "a count of distinct entities",
    GRAIN_QUANTITY: "a summed quantity measure",
    GRAIN_AMOUNT: "a summed money measure",
    GRAIN_RATE: "a ratio",
    GRAIN_UNKNOWN: "not established",
}

#: Counting vs measuring: mixing these two families is the 17,957% defect.
COUNT_GRAINS = frozenset({GRAIN_ROW_COUNT, GRAIN_ENTITY_COUNT})
MEASURE_GRAINS = frozenset({GRAIN_QUANTITY, GRAIN_AMOUNT})

#: What a zero (or null) denominator means for the metric.
ZERO_DENOMINATOR_BEHAVIORS = {
    "unsupported": "a zero denominator means the ratio cannot be computed here",
    "null": "the ratio is reported as null, never as 0",
    "zero": "a zero numerator over a zero denominator IS a real 0 for this metric",
}

#: Units that make a ratio unitless by construction.
RATIO_UNITS = frozenset({"", "%", "percent", "比例", "率", "ratio", "x", "倍"})

#: Every way a ratio can fail validation. Each is disclosed in business language.
RATIO_REASONS = {
    "grain_mismatch": "the two sides are measured at different grains",
    "quantity_over_count": "a summed quantity is divided by a count of documents",
    "unknown_grain": "the grain of one side could not be established",
    "unit_mismatch": "the two sides carry incompatible units",
    "zero_denominator": "the denominator is zero or null",
    "range_unproven": "the value lies outside the range a part-of-whole ratio can occupy",
    "range_out_of_contract": "the value lies outside the range this metric declares",
    "source_scope_mismatch": "the two sides come from different data sources",
    "date_scope_mismatch": "the two sides cover different periods",
    "date_scope_missing": "the period asked about is not applied to both sides",
    "dimension_not_allowed": "the ratio is grouped by a dimension it is not defined over",
    "numerator_unsupported": "the numerator is not a supported measurement",
    "denominator_unsupported": "the denominator is not a supported measurement",
}

#: Reasons that make a figure SUSPECT rather than invalid. The measurement
#: relationship is sound (the two sides ARE commensurable); what is doubtful is
#: the number itself (out of band) or its absence (nothing to divide by). These
#: are disclosed as warnings — never used to withhold a figure that is legitimate,
#: and never clamped.
WARNING_ONLY_REASONS = frozenset({"range_unproven", "zero_denominator"})

#: The checks requirement-wise every ratio must pass, in the order they run.
RATIO_CHECKS = (
    "numerator_supported",
    "denominator_supported",
    "source_scope",
    "date_scope",
    "grain",
    "units",
    "denominator_nonzero",
    "range",
    "dimensions",
)

_AGG = re.compile(
    r"^\s*(?P<fn>count|sum|avg|min|max)\s*\(\s*(?P<distinct>distinct\s+)?(?P<arg>[^)]*)\)\s*$",
    re.IGNORECASE,
)
_ANY_AGG = re.compile(r"\b(count|sum|avg|min|max)\s*\(", re.IGNORECASE)
_DATE_PREDICATE = re.compile(
    r"\b(?P<column>\w*(?:date|day|time|dt|ymd|period|_at|_ts|月份|日期|时间)\w*)\s*"
    r"(?P<op>>=|<=|>|<|between)\s*'?(?P<value>[0-9]{4}-[0-9]{2}-[0-9]{2}[^'\s,)]*)'?",
    re.IGNORECASE,
)
_RATIO_SPLIT = re.compile(r"\s*(?:/|÷|／)\s*")
_PER = re.compile(r"\s+(?:per|每)\s+", re.IGNORECASE)
_MONEY_UNITS = frozenset({"元", "万元", "rmb", "cny", "usd", "money", "amount"})

#: A metric that READS as a share of a whole. A value outside 0-100% for one of
#: these is a claim needing evidence (never a number to be clamped).
_PERCENT_CUE = re.compile(r"率|百分|占比|percent|rate|ratio|margin|share", re.IGNORECASE)


def reads_as_percentage(*texts: str) -> bool:
    """Do the metric's own names say this is a share of a whole?"""
    return any(_PERCENT_CUE.search(str(t or "")) for t in texts)


def _unit_grain(unit: str) -> str:
    return GRAIN_AMOUNT if (unit or "").strip().lower() in _MONEY_UNITS else GRAIN_QUANTITY


def infer_grain(expression: str, unit: str = "") -> str:
    """The grain a side is measured at, from its AGGREGATION shape.

    Generic on purpose: it reads ``COUNT(*)`` / ``COUNT(DISTINCT k)`` /
    ``SUM(x)`` — the shape of the aggregation — never a table or column name, so
    it holds for any source bound at runtime.
    """
    text = str(expression or "").strip()
    if not text:
        return GRAIN_UNKNOWN
    # A CTE's own projection reads ``SUM(shipped_quantity) AS shipped_qty``: the
    # alias is not part of the measurement.
    text = re.sub(r"\s+as\s+[`\"\[]?[\w\u4e00-\u9fff]+[`\"\]]?\s*$", "", text, flags=re.I)
    if not text:
        return GRAIN_UNKNOWN
    if not _ANY_AGG.search(text):
        return GRAIN_RATE if "/" in text else GRAIN_UNKNOWN
    if "/" in text:
        return GRAIN_RATE
    match = _AGG.match(_strip_outer_parens(text))
    if match is None:
        return GRAIN_UNKNOWN
    fn = match.group("fn").lower()
    if fn == "count":
        return GRAIN_ENTITY_COUNT if match.group("distinct") else GRAIN_ROW_COUNT
    if fn == "sum":
        return _unit_grain(unit)
    if fn == "avg":
        return _unit_grain(unit)
    return GRAIN_UNKNOWN


def _strip_outer_parens(text: str) -> str:
    out = text.strip()
    while out.startswith("(") and out.endswith(")") and _balanced(out[1:-1]):
        out = out[1:-1].strip()
    return out


def _balanced(text: str) -> bool:
    depth = 0
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def split_ratio(text: str) -> tuple[str, str] | None:
    """``(numerator, denominator)`` for a ratio-shaped expression — else ``None``.

    Scans for a top-level division, so ``SUM(a) / (SUM(b) / SUM(c))`` splits at
    the first slash and a division inside a string literal cannot fool it.
    """
    body = _strip_outer_parens(str(text or "").strip().rstrip(";"))
    if not body:
        return None
    depth = 0
    quote = ""
    for index, ch in enumerate(body):
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch in "/／÷" and depth == 0:
            left, right = _clean_side(body[:index]), _clean_side(body[index + 1 :])
            if left and right:
                return left, right
            return None
    return None


def _clean_side(text: str) -> str:
    """Drop a conditional wrapper from one side of a division.

    ``CASE WHEN d = 0 THEN NULL ELSE SUM(x) END / SUM(y)`` must read as
    ``SUM(x)`` over ``SUM(y)``: the guard clause is not the measurement.
    """
    out = str(text or "").strip()
    if re.search(r"\bcase\b", out, re.IGNORECASE):
        tail = re.split(r"\belse\b", out, flags=re.IGNORECASE)
        if len(tail) > 1:
            out = tail[-1]
    out = re.sub(r"\bend\b\s*$", "", out, flags=re.IGNORECASE).strip()
    # Only whitespace: stripping parentheses here would eat the closing paren of
    # ``SUM(x)`` and make a readable measurement look unknown.
    return out.strip()


def sides_from_definition(definition: str) -> tuple[str, str] | None:
    """``(numerator, denominator)`` from a prose definition — ``发货数量 / 合同数量``."""
    text = str(definition or "").strip()
    if not text:
        return None
    parts = _RATIO_SPLIT.split(text, maxsplit=1)
    if len(parts) < 2:
        parts = _PER.split(text, maxsplit=1)
    if len(parts) < 2:
        return None
    left, right = parts[0].strip(" 　:：、,，"), parts[1].strip(" 　:：、,，")
    if not left or not right:
        return None
    # a definition may carry a parenthetical explanation after the denominator
    right = re.split(r"[（(。;；]", right, maxsplit=1)[0].strip()
    return (left, right) if left and right else None


# ── the contract ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RatioContract:
    """What a ratio metric MEANS, so its arithmetic can be checked.

    ``metric_id`` identifies the contract (the metric's own name when the
    vocabulary declares it, otherwise the result column it was inferred for).
    ``required_grain`` is the grain the division is valid at: both sides must be
    measured there, and it is what the SQL model is told to hold.
    """

    metric_id: str
    numerator: str
    denominator: str
    numerator_grain: str = GRAIN_UNKNOWN
    denominator_grain: str = GRAIN_UNKNOWN
    required_grain: str = GRAIN_UNKNOWN
    numerator_unit: str = ""
    denominator_unit: str = ""
    output_unit: str = ""
    date_scope: str = "same_window"
    allowed_dimensions: tuple[str, ...] = ()
    zero_denominator: str = "unsupported"
    valid_range: tuple[float, float] | None = None
    evidence: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    tier: str = "inferred"  # declared | inferred

    # ── derived ───────────────────────────────────────────────────────────
    @property
    def compatible_grain(self) -> bool:
        return (
            self.numerator_grain == self.denominator_grain
            and self.numerator_grain != GRAIN_UNKNOWN
        )

    @property
    def mixes_count_with_measure(self) -> bool:
        left = self.numerator_grain in MEASURE_GRAINS
        right = self.denominator_grain in COUNT_GRAINS
        return (left and right) or (
            self.numerator_grain in COUNT_GRAINS and self.denominator_grain in MEASURE_GRAINS
        )

    def allows_value(self, value: float | None) -> bool:
        """Does the DECLARED range of this metric cover the value?

        ``None`` means "no declared range": the caller must then decide, and for
        a part-of-whole percentage that means the value is unproven rather than
        wrong. A declared range is the only thing that can licence >100%.
        """
        if self.valid_range is None or value is None:
            return False
        low, high = self.valid_range
        return low <= value <= high

    def declared(self) -> bool:
        return self.tier == "declared"

    # ── serialization ─────────────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        return {
            "metric_id": self.metric_id,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "numerator_grain": self.numerator_grain,
            "denominator_grain": self.denominator_grain,
            "required_grain": self.required_grain,
            "numerator_unit": self.numerator_unit,
            "denominator_unit": self.denominator_unit,
            "output_unit": self.output_unit,
            "date_scope": self.date_scope,
            "allowed_dimensions": list(self.allowed_dimensions),
            "zero_denominator": self.zero_denominator,
            "valid_range": list(self.valid_range) if self.valid_range else None,
            "evidence": list(self.evidence),
            "source_ids": list(self.source_ids),
            "tier": self.tier,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "RatioContract":
        data = dict(payload or {})
        valid_range = data.get("valid_range")
        return cls(
            metric_id=str(data.get("metric_id") or ""),
            numerator=str(data.get("numerator") or ""),
            denominator=str(data.get("denominator") or ""),
            numerator_grain=str(data.get("numerator_grain") or GRAIN_UNKNOWN),
            denominator_grain=str(data.get("denominator_grain") or GRAIN_UNKNOWN),
            required_grain=str(data.get("required_grain") or GRAIN_UNKNOWN),
            numerator_unit=str(data.get("numerator_unit") or ""),
            denominator_unit=str(data.get("denominator_unit") or ""),
            output_unit=str(data.get("output_unit") or ""),
            date_scope=str(data.get("date_scope") or "same_window"),
            allowed_dimensions=tuple(data.get("allowed_dimensions") or ()),
            zero_denominator=str(data.get("zero_denominator") or "unsupported"),
            valid_range=(tuple(valid_range) if valid_range else None),  # type: ignore[arg-type]
            evidence=tuple(data.get("evidence") or ()),
            source_ids=tuple(data.get("source_ids") or ()),
            tier=str(data.get("tier") or "inferred"),
        )


def validate_contract(contract: RatioContract) -> list[str]:
    """Internal consistency of a contract — a contract nobody can check is a bug."""
    v: list[str] = []
    if not contract.metric_id:
        v.append("ratio contract without a metric id")
    if not contract.numerator or not contract.denominator:
        v.append(f"ratio {contract.metric_id!r} needs both a numerator and a denominator")
    if contract.numerator == contract.denominator:
        v.append(f"ratio {contract.metric_id!r} divides a value by itself")
    for name, grain in (
        ("numerator_grain", contract.numerator_grain),
        ("denominator_grain", contract.denominator_grain),
        ("required_grain", contract.required_grain),
    ):
        if grain not in GRAINS:
            v.append(f"ratio {contract.metric_id!r} has {name} {grain!r} outside the vocabulary")
    if contract.zero_denominator not in ZERO_DENOMINATOR_BEHAVIORS:
        v.append(
            f"ratio {contract.metric_id!r} has zero_denominator "
            f"{contract.zero_denominator!r} outside the vocabulary"
        )
    if contract.valid_range is not None:
        low, high = contract.valid_range
        if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
            v.append(f"ratio {contract.metric_id!r} has a non-numeric valid_range")
        elif low > high:
            v.append(f"ratio {contract.metric_id!r} has an inverted valid_range")
    return v


# ── readers: from the metric vocabulary, and from the SQL ──────────────────


def _binding(payload: Any, key: str) -> Any:
    """Read ``key`` from a metric binding (a dict, or a list of dicts)."""
    items = payload if isinstance(payload, (list, tuple)) else [payload]
    for item in items:
        if isinstance(item, Mapping) and item.get(key) not in (None, "", [], {}):
            return item.get(key)
    return None


def contract_from_term(term: Any, *, units: Mapping[str, str] | None = None) -> RatioContract | None:
    """A ratio contract from a metric-contract term (semantic_store.GlossaryTerm).

    Reads, in order: an explicit numerator/denominator/bindings declaration, then
    the metric's own ``sql_expression``, then its prose ``definition``. Returns
    ``None`` when the metric is not a ratio at all.
    """
    if term is None:
        return None
    units = units or {}
    name = str(getattr(term, "term", "") or "")
    expression = str(getattr(term, "sql_expression", "") or "")
    definition = str(getattr(term, "definition", "") or "")
    bindings = getattr(term, "bindings", None)
    output_unit = str(getattr(term, "unit", "") or "")

    sides = split_ratio(expression) or sides_from_definition(definition)
    declared_num = _binding(bindings, "numerator")
    declared_den = _binding(bindings, "denominator")
    if declared_num and declared_den:
        sides = (str(declared_num), str(declared_den))
    if sides is None:
        return None
    numerator, denominator = sides

    num_unit = str(_binding(bindings, "numerator_unit") or "") or _unit_for(numerator, units)
    den_unit = str(_binding(bindings, "denominator_unit") or "") or _unit_for(denominator, units)
    num_grain = str(_binding(bindings, "numerator_grain") or "") or infer_grain(numerator, num_unit)
    den_grain = str(_binding(bindings, "denominator_grain") or "") or infer_grain(denominator, den_unit)
    required = str(_binding(bindings, "required_grain") or "") or (
        num_grain if num_grain == den_grain else GRAIN_UNKNOWN
    )
    valid_range = _binding(bindings, "valid_range")
    return RatioContract(
        metric_id=name or expression or "ratio",
        numerator=numerator,
        denominator=denominator,
        numerator_grain=num_grain,
        denominator_grain=den_grain,
        required_grain=required,
        numerator_unit=num_unit,
        denominator_unit=den_unit,
        output_unit=output_unit,
        date_scope=str(_binding(bindings, "date_scope") or "same_window"),
        allowed_dimensions=tuple(_binding(bindings, "allowed_dimensions") or ()),
        zero_denominator=str(_binding(bindings, "zero_denominator") or "unsupported"),
        valid_range=(tuple(valid_range) if valid_range else None),  # type: ignore[arg-type]
        evidence=(
            f"metric contract {name!r}",
            f"numerator={numerator!r} ({num_grain})",
            f"denominator={denominator!r} ({den_grain})",
        ),
        tier="declared",
    )


def contract_from_expressions(
    metric_id: str,
    numerator: str,
    denominator: str,
    *,
    units: Mapping[str, str] | None = None,
    source_ids: Iterable[str] = (),
) -> RatioContract:
    """A contract inferred from SQL the model actually wrote (the safety net)."""
    units = units or {}
    num_unit = _unit_for(numerator, units)
    den_unit = _unit_for(denominator, units)
    num_grain = infer_grain(numerator, num_unit)
    den_grain = infer_grain(denominator, den_unit)
    return RatioContract(
        metric_id=metric_id or "ratio",
        numerator=numerator,
        denominator=denominator,
        numerator_grain=num_grain,
        denominator_grain=den_grain,
        required_grain=num_grain if num_grain == den_grain else GRAIN_UNKNOWN,
        numerator_unit=num_unit,
        denominator_unit=den_unit,
        evidence=(
            "inferred from the SQL the query ran",
            f"numerator={numerator!r} ({num_grain})",
            f"denominator={denominator!r} ({den_grain})",
        ),
        source_ids=tuple(source_ids),
        tier="inferred",
    )


def _unit_for(expression: str, units: Mapping[str, str]) -> str:
    """The unit of the catalog column a side aggregates, when the catalog knows."""
    if not units:
        return ""
    bare = re.sub(r"^\s*(sum|avg|min|max|count)\s*\(\s*(distinct\s+)?", "", str(expression or ""), flags=re.I)
    bare = bare.split(")")[0].strip().strip("`\"[]")
    column = bare.split(".")[-1].lower()
    if not column:
        return ""
    if column in units:
        return str(units[column] or "")
    for key, unit in units.items():
        if key.lower() == column:
            return str(unit or "")
    return ""


#: Any top-level division in a SELECT list is a ratio candidate.
def _norm(text: Any) -> str:
    return re.sub(r"[\s`\"\[\]]+", "", str(text or "")).lower()


def _resolve_side(expr: str, defined: Mapping[str, str], depth: int = 0) -> tuple[str, str]:
    """``(expression, grain)`` for one side, chasing a CTE column to its source.

    Measured live: the model wrote ``WITH shipment AS (SELECT SUM(qty) …) SELECT
    s.qty / c.count AS rate``. The final SELECT holds no aggregation at all — the
    aggregation lives in the CTE — so reading only the outer expression found
    nothing to check on the one shape that most needed checking.
    """
    text = str(expr or "").strip()
    if _ANY_AGG.search(text) or depth >= 3:
        return text, infer_grain(text)
    bare = text.split(".")[-1].strip('`"[]').lower()
    target = defined.get(bare)
    if target and _norm(target) != _norm(text):
        return _resolve_side(target, defined, depth + 1)
    return text, infer_grain(text)


def ratios_in_sql(sql: str | None) -> list[dict[str, Any]]:
    """Every ratio-shaped result column in ``sql``, with its inferred contract.

    Prefers sqlglot (aliases, nesting, CTEs); falls back to reading the SELECT
    list, so a parse failure degrades the evidence instead of losing the check.
    """
    text = str(sql or "")
    if not text:
        return []
    parsed = _ratios_via_sqlglot(text)
    if parsed is not None:
        return parsed
    out: list[dict[str, Any]] = []
    for column, expression in _select_expressions(text).items():
        sides = split_ratio(expression)
        if sides is None:
            continue
        numerator, denominator = sides
        if not _ANY_AGG.search(numerator) and not _ANY_AGG.search(denominator):
            continue
        out.append(
            {
                "column": column,
                "expression": expression,
                "numerator": numerator,
                "denominator": denominator,
            }
        )
    return out


def _ratios_via_sqlglot(sql: str) -> list[dict[str, Any]] | None:
    """Ratio result columns via a real parse; ``None`` when it cannot be parsed."""
    try:
        import sqlglot
        from sqlglot import exp

        from app.services.db.schema_graph import _map_dialect

        tree = sqlglot.parse_one(sql, read=_map_dialect(None))
        if tree is None:
            return None
        defined: dict[str, str] = {}
        for select in tree.find_all(exp.Select):
            for projection in select.expressions:
                alias = projection.alias_or_name
                if alias:
                    defined[str(alias).lower()] = projection.sql()
        root = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
        if root is None:
            return None
        out: list[dict[str, Any]] = []
        for projection in root.expressions:
            division = next((node for node in projection.walk() if isinstance(node, exp.Div)), None)
            if division is None:
                continue
            numerator, num_grain = _resolve_side(division.left.sql(), defined)
            denominator, den_grain = _resolve_side(division.right.sql(), defined)
            if num_grain == GRAIN_UNKNOWN and den_grain == GRAIN_UNKNOWN:
                continue
            out.append(
                {
                    "column": str(projection.alias_or_name or ""),
                    "expression": projection.sql(),
                    "numerator": numerator,
                    "denominator": denominator,
                }
            )
        return out
    except Exception:  # pragma: no cover - a parse failure falls back to regex
        return None


def _select_expressions(sql: str) -> dict[str, str]:
    """``{result column: expression}`` for the SELECT list (aliases resolved)."""
    from app.services.db.grounding import sql_alias_expressions

    # The SELECT list is read here rather than borrowed from the grounding alias
    # map: that map is a display heuristic and drops a parenthesis on a
    # CASE-guarded division, which would make the numerator unreadable and turn a
    # computable ratio into an unknown-grain one. Grounding's map is the fallback.
    by_column: dict[str, str] = {}
    body = _final_select_body(sql)
    if not body:
        return dict(sql_alias_expressions(sql))
    for part in _split_select_list(body):
        item = part.strip()
        if not item:
            continue
        alias = None
        match = re.search(r"\bas\s+([`\"\[]?[\w\u4e00-\u9fff]+[`\"\]]?)\s*$", item, re.I)
        if match:
            alias = match.group(1).strip('`"[]')
        else:
            bare = re.fullmatch(r"[`\"\[]?([\w\u4e00-\u9fff]+)[`\"\]]?", item)
            if bare:
                alias = bare.group(1)
        if alias:
            expression = item[: match.start()].strip() if match else item
            by_column[alias] = expression
    for column, expression in sql_alias_expressions(sql).items():
        by_column.setdefault(column, expression)
    return by_column


def _final_select_body(sql: str) -> str:
    """The SELECT list of the statement's FINAL select — CTEs included.

    Measured live: the model wrote the ratio as ``WITH a AS (…), b AS (…) SELECT
    a.x / b.y``. Splitting on the first ``FROM`` then reads a CTE's column list,
    finds no division, and reports nothing — so the one query shape that most
    needs the check was the one shape it could not see. The last depth-0 SELECT is
    the statement's own result shape.
    """
    text = str(sql or "")
    depth = 0
    quote = ""
    selected_at = -1
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0:
            token = re.compile(r"\b(select|from)\b", re.IGNORECASE).match(text, index)
            if token:
                if token.group(1).lower() == "select":
                    selected_at = token.end()
                elif selected_at > 0:
                    body = text[selected_at : token.start()]
                    return re.sub(r"^\s*distinct\s+", "", body.strip(), flags=re.I)
        index += 1
    if selected_at > 0:
        return re.sub(r"^\s*distinct\s+", "", text[selected_at:].strip(), flags=re.I)
    return ""


def _split_select_list(body: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    quote = ""
    current = ""
    for ch in body:
        if quote:
            current += ch
            if ch == quote:
                quote = ""
            continue
        if ch in "'\"":
            quote = ch
            current += ch
        elif ch == "(":
            depth += 1
            current += ch
        elif ch == ")":
            depth -= 1
            current += ch
        elif ch == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    if current.strip():
        parts.append(current)
    return parts


# ── validation ─────────────────────────────────────────────────────────────


def sql_date_windows(sql: str | None) -> list[tuple[str, str, str, int]]:
    """``[(column, op, value, position)]`` for every date predicate in the SQL."""
    return [
        (m.group("column"), m.group("op"), m.group("value"), m.start())
        for m in _DATE_PREDICATE.finditer(str(sql or ""))
    ]


def _side_window(
    expression: str, sql: str, windows: Sequence[tuple[str, str, str, int]]
) -> tuple[str, str] | None:
    """The window that belongs to THIS side's own subquery.

    Position alone is not enough: in ``(SELECT … WHERE d >= 'a') / (SELECT …
    WHERE d >= 'b')`` the first side is written before any predicate, so nearest
    -preceding would hand it the other side's window. The enclosing parenthesised
    group is what actually scopes a side.
    """
    if not windows:
        return None
    text = str(sql or "")
    position = text.find(expression)
    if position < 0:
        position = len(text)
    depth = 0
    start = 0
    groups: list[tuple[int, int]] = []
    for index, char in enumerate(text):
        if char == "(":
            if depth == 0:
                start = index
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                groups.append((start, index))
    for low, high in groups:
        if low <= position <= high:
            inside = [w for w in windows if low <= w[3] <= high]
            if inside:
                return (inside[0][1], inside[0][2])
    before = [w for w in windows if w[3] <= position]
    chosen = before[-1] if before else windows[0]
    return (chosen[1], chosen[2])


def _num(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def validate_ratio(
    contract: RatioContract,
    *,
    value: Any = None,
    numerator_value: Any = None,
    denominator_value: Any = None,
    sql: str | None = None,
    groupings: Sequence[str] = (),
    source_ids: Iterable[str] = (),
    plan_sources: Iterable[str] = (),
    requested_window: bool = False,
) -> dict[str, Any]:
    """Run every ratio check and return the verdict, with its evidence.

    Returns ``{"ok", "reasons", "checks", "contract", "evidence", "value"}``.
    ``checks`` records pass/fail/unknown for each check by name, so a reader can
    see which ones actually ran — an unrun check is never reported as a pass.
    """
    checks: dict[str, str] = {}
    reasons: list[str] = []
    evidence: list[str] = list(contract.evidence)

    def fail(check: str, reason: str, note: str = "") -> None:
        checks[check] = "fail"
        if reason not in reasons:
            reasons.append(reason)
        if note:
            evidence.append(note)

    def soft(check: str, note: str) -> None:
        checks[check] = "unknown"
        evidence.append(note)

    # 1 & 2. both sides must be supported measurements, not bare column names
    for check, side, grain in (
        ("numerator_supported", contract.numerator, contract.numerator_grain),
        ("denominator_supported", contract.denominator, contract.denominator_grain),
    ):
        if not str(side).strip():
            fail(check, "numerator_unsupported" if check.startswith("numerator") else "denominator_unsupported")
        elif not _ANY_AGG.search(str(side)) and grain == GRAIN_UNKNOWN:
            fail(
                check,
                "numerator_unsupported" if check.startswith("numerator") else "denominator_unsupported",
                f"{side!r} carries no aggregation, so it is not a measurement",
            )
        else:
            checks[check] = "pass"

    # 3. both sides from the same source scope
    contract_sources = set(contract.source_ids) - {""}
    plan_set = set(plan_sources) - {""}
    if contract_sources and plan_set and not (contract_sources & plan_set):
        fail("source_scope", "source_scope_mismatch", f"contract sources {sorted(contract_sources)}")
    else:
        checks["source_scope"] = "pass" if (contract_sources or plan_set) else "unknown"
    if len({str(s) for s in source_ids if s}) > 1:
        fail("source_scope", "source_scope_mismatch", "the two sides read different sources")

    # 4. the same period must apply to both sides
    windows = sql_date_windows(sql)
    if len(windows) >= 2:
        left = _side_window(contract.numerator, str(sql or ""), windows)
        right = _side_window(contract.denominator, str(sql or ""), windows)
        if left and right and left != right:
            fail(
                "date_scope",
                "date_scope_mismatch",
                f"numerator window {left}, denominator window {right}",
            )
        else:
            checks["date_scope"] = "pass"
    elif requested_window and not windows:
        fail("date_scope", "date_scope_missing", "the question asked for a period; the SQL applies none")
    else:
        checks["date_scope"] = "pass" if windows else "unknown"

    # 5. the same business grain — the 17,957% check
    if contract.numerator_grain == GRAIN_UNKNOWN or contract.denominator_grain == GRAIN_UNKNOWN:
        fail(
            "grain",
            "unknown_grain",
            f"grains {contract.numerator_grain}/{contract.denominator_grain}",
        )
    elif contract.numerator_grain != contract.denominator_grain:
        reason = "quantity_over_count" if contract.mixes_count_with_measure else "grain_mismatch"
        fail(
            "grain",
            reason,
            f"numerator {contract.numerator!r} is {contract.numerator_grain}, "
            f"denominator {contract.denominator!r} is {contract.denominator_grain}",
        )
    elif contract.required_grain not in ("", GRAIN_UNKNOWN) and (
        contract.required_grain != contract.numerator_grain
    ):
        fail(
            "grain",
            "grain_mismatch",
            f"the SQL measures at {contract.numerator_grain}; the metric contract "
            f"defines it at {contract.required_grain}",
        )
    else:
        checks["grain"] = "pass"

    # 6. units must be compatible (an empty unit means unitless, not a mismatch)
    num_unit = (contract.numerator_unit or "").strip()
    den_unit = (contract.denominator_unit or "").strip()
    if num_unit and den_unit and num_unit != den_unit:
        fail("units", "unit_mismatch", f"{num_unit!r} vs {den_unit!r}")
    elif bool(num_unit) != bool(den_unit):
        soft("units", f"one side carries a unit ({num_unit or den_unit!r}) and the other does not")
    else:
        checks["units"] = "pass"

    # 7. denominator must not be zero
    den_value = _num(denominator_value)
    if den_value is not None:
        if den_value == 0:
            if contract.zero_denominator == "unsupported":
                fail("denominator_nonzero", "zero_denominator", "denominator = 0")
            else:
                checks["denominator_nonzero"] = "pass"
                evidence.append(
                    f"denominator is zero; this metric reports it as {contract.zero_denominator}"
                )
        else:
            checks["denominator_nonzero"] = "pass"
    elif value is None:
        fail(
            "denominator_nonzero",
            "zero_denominator",
            "the ratio came back null — nothing to divide by (denominator zero or unknown)",
        )
    else:
        checks["denominator_nonzero"] = "unknown"

    # 8. range: a declared range is the ONLY thing that licences an odd value
    numeric = _num(value)
    if numeric is None:
        checks["range"] = "unknown"
    elif contract.valid_range is not None:
        if contract.allows_value(numeric):
            checks["range"] = "pass"
            evidence.append(f"within the declared range {contract.valid_range}")
        else:
            fail(
                "range",
                "range_out_of_contract",
                f"{numeric:.4g} outside the declared range {contract.valid_range}",
            )
    elif reads_as_percentage(contract.output_unit, contract.metric_id) and not (0 <= numeric <= 100):
        # NOT clamped: a percentage outside 0-100 is a claim that needs proving —
        # a warning that requires contract evidence, never a trimmed number.
        fail(
            "range",
            "range_unproven",
            f"{numeric:.4g} is outside 0-100 and no contract range declares it",
        )
    else:
        checks["range"] = "pass"

    # 9. the grouping must be one the metric is defined over
    allowed = {str(d).lower() for d in contract.allowed_dimensions}
    if allowed and groupings:
        used = {str(g).split(".")[-1].lower() for g in groupings if g}
        if used - allowed:
            fail(
                "dimensions",
                "dimension_not_allowed",
                f"grouped by {sorted(used - allowed)}, contract allows {sorted(allowed)}",
            )
        else:
            checks["dimensions"] = "pass"
    elif allowed:
        checks["dimensions"] = "pass"
    else:
        checks["dimensions"] = "unknown"

    return {
        "metric": contract.metric_id,
        "column": "",
        "value": numeric,
        "ok": not reasons,
        "reasons": reasons,
        "checks": checks,
        "contract": contract.to_dict(),
        "evidence": evidence,
    }


def ratio_disclosure(verdict: Mapping[str, Any]) -> str:
    """One business-language sentence for an unverified ratio ("" when valid)."""
    if verdict.get("ok"):
        return ""
    label = str(verdict.get("metric") or "the ratio")
    reasons = [str(r) for r in (verdict.get("reasons") or [])]
    if "quantity_over_count" in reasons or "grain_mismatch" in reasons:
        return (
            f"{label} could not be verified: the two amounts behind it are not measured "
            "on the same basis — one counts records, the other sums a quantity — so no "
            "figure is reported for it. It is not zero — it is unknown."
        )
    if "unknown_grain" in reasons:
        return (
            f"{label} could not be verified: how the two amounts behind it are measured "
            "is not known, so no figure is reported for it. It is not zero — it is unknown."
        )
    if "unit_mismatch" in reasons:
        return (
            f"{label} could not be verified: its two sides carry different units, so no "
            "figure is reported for it. It is not zero — it is unknown."
        )
    if "zero_denominator" in reasons:
        return (
            f"{label} cannot be computed for this period: the amount it is measured "
            "against is zero or unknown, so no figure is reported for it."
        )
    if "date_scope_mismatch" in reasons or "date_scope_missing" in reasons:
        return (
            f"{label} could not be verified: its two sides do not cover the same period, "
            "so no figure is reported for it. It is not zero — it is unknown."
        )
    if "range_unproven" in reasons or "range_out_of_contract" in reasons:
        return (
            f"{label} came back far outside the range this metric can occupy, so it needs "
            "checking before it is used: the two sides are probably not measured on one basis."
        )
    if "source_scope_mismatch" in reasons:
        return (
            f"{label} could not be verified: its two sides come from different data sources, "
            "so no figure is reported for it. It is not zero — it is unknown."
        )
    if "dimension_not_allowed" in reasons:
        return (
            f"{label} is not defined at this level of detail, so no figure is reported for it."
        )
    return (
        f"{label} could not be verified against this data, so no figure is reported for it. "
        "It is not zero — it is unknown."
    )
