"""Synexia typed contracts — Pydantic v2 schemas for the report-card pipeline.

These contracts are the typed boundary between the FSM engine and the
rest of the system. They mirror the data shapes the architecture doc
(`Zhanlu_Layer_2_Synexia_Cognitive_Core_v1_1_Gao_Fixed.md`) calls for:

- TaskSpec      — what the user wants (goal)
- PlanDAG       — how to do it (plan)
- ObservationRecord — what happened (observe)
- ConfidenceScore — is the output trustworthy (verify)
- ReportCardPayload — the rich, in-chat report surface (finalize)
- FinalizeResult — what we hand back to the chat loop (finalize)

Keeping these in one file means the chat loop, the FSM, the data-agent,
and the frontend can all share the same shape.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# TaskSpec
# ---------------------------------------------------------------------------


class TaskSpec(BaseModel):
    """Typed representation of the user's request after GOAL parsing.

    Created by Goal Engine from the raw user message.  Used by the
    downstream engines (CONTEXT, PLAN, FINALIZE) to scope themselves.
    """

    task_kind: str = Field(
        default="general",
        description=(
            "One of: report | create_artifact | answer_question | analyze_data | "
            "configure_system | general"
        ),
    )
    artifact_intents: list[str] = Field(
        default_factory=list,
        description="Output artifact kinds the user wants (pptx, pdf, xlsx, html_report, chart, etc.)",
    )
    entities: dict[str, Any] = Field(
        default_factory=dict,
        description="Key entities: date_range, metric, product, department, etc.",
    )
    kpis: list[str] = Field(default_factory=list)
    complexity: str = Field(default="moderate")
    requires_data: bool = False
    user_signal: str = Field(
        default="default",
        description="default | export | download | save | share",
    )


# ---------------------------------------------------------------------------
# PlanDAG
# ---------------------------------------------------------------------------


class PlanNodeSpec(BaseModel):
    """One node in the PlanDAG. Used for in-process FSM run (not persisted)."""

    node_id: str
    node_type: str  # tool | skill | nl2sql | sandbox | synthesize
    name: str
    description: str = ""
    agent_role: Optional[str] = None  # data_analyst | synthesizer | presenter
    dependencies: list[str] = Field(default_factory=list)
    inputs: dict[str, Any] = Field(default_factory=dict)
    expected_output: str = ""
    output_artifact_type: Optional[str] = None


class PlanDAG(BaseModel):
    """A directed acyclic graph of PlanNodeSpec entries."""

    nodes: list[PlanNodeSpec] = Field(default_factory=list)

    def topo_sort(self) -> list[PlanNodeSpec]:
        """Return nodes in topological order using Kahn's algorithm.

        Falls back to input order on cycles (we already validate
        acyclicity when building).
        """
        by_id = {n.node_id: n for n in self.nodes}
        in_deg: dict[str, int] = {n.node_id: 0 for n in self.nodes}
        children: dict[str, list[str]] = {n.node_id: [] for n in self.nodes}
        for n in self.nodes:
            for dep in n.dependencies:
                if dep in by_id:
                    in_deg[n.node_id] += 1
                    children[dep].append(n.node_id)

        ready = [nid for nid, d in in_deg.items() if d == 0]
        out: list[PlanNodeSpec] = []
        visited = 0
        while ready:
            nid = ready.pop(0)
            out.append(by_id[nid])
            visited += 1
            for child in children[nid]:
                in_deg[child] -= 1
                if in_deg[child] == 0:
                    ready.append(child)

        if visited != len(self.nodes):
            # Cycle: append the leftovers in input order so the FSM
            # still terminates.
            seen = {n.node_id for n in out}
            for n in self.nodes:
                if n.node_id not in seen:
                    out.append(n)
        return out


# ---------------------------------------------------------------------------
# ObservationRecord
# ---------------------------------------------------------------------------


class ObservationRecord(BaseModel):
    """Structured output of one plan node. The FSM's audit trail."""

    node_id: str
    observation_type: str  # tool_call | nl2sql | sandbox | synthesize | error
    tool_name: Optional[str] = None
    request_args: dict[str, Any] = Field(default_factory=dict)
    result_data: Any = None
    result_text: str = ""
    success: bool = True
    error_message: Optional[str] = None
    artifact_ids: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# ConfidenceScore
# ---------------------------------------------------------------------------


class ConfidenceScore(BaseModel):
    """Deterministic confidence score for the finished execution."""

    overall: float = 0.0
    factors: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# ReportCardPayload — the in-chat report surface
# ---------------------------------------------------------------------------


class KPISpec(BaseModel):
    """One KPI tile on the report card."""

    label: str
    value: str
    delta: Optional[str] = None
    caption: Optional[str] = None


class ChartSpec(BaseModel):
    """Chart instructions for the frontend to render with recharts.

    The frontend (ReportCard.jsx) maps this directly to a BarChart or
    LineChart without re-parsing the data.
    """

    type: str = "bar"  # bar | line | pie
    title: str = ""
    # x_key / y_keys are optional. Exporters (_common.chart_x_key,
    # _common.chart_y_keys) already fall back to "label" / ["value"] when
    # these are empty, so the docx/pdf/pptx render path works either way.
    # Required here previously caused _payload_to_reportcard to crash on
    # agent payloads that only supplied `chart.data` (a list of dicts).
    x_key: str = ""
    y_keys: list[str] = Field(default_factory=list)
    data: list[dict[str, Any]] = Field(default_factory=list)
    unit: str = ""

    @field_validator("data", mode="before")
    @classmethod
    def _coerce_data(cls, v):
        """Accept the ``{labels, values}`` "wide" chart shape that several
        producers (FINALIZE, the data-agent SQL result) emit, and normalize
        it into the row-list shape ``data: list[dict]`` the exporters
        expect.

        Single series: ``{"labels": [...], "values": [...]}`` →
        ``[{"<x_key>": l, "<y_key>": v}, ...]``.
        Multi series: ``"values": [[...], [...]]`` (with ``"series"`` /
        ``"y_keys"`` names) → one column per series.
        Columnar wide (2026-08-29): ``{"months": [...], "amount": [...],
        "qty": [...]}`` — a dict of equal-length lists — is transposed
        into rows ``[{"months": m, "amount": a, "qty": q}, ...]`` so
        multi-series charts (combo / grouped) keep their data instead of
        being coerced to an empty list.
        """
        if isinstance(v, dict):
            labels = v.get("labels") or []
            values = v.get("values")
            if isinstance(labels, list) and isinstance(values, list):
                x_key = v.get("x_key") or "label"
                if values and isinstance(values[0], list):
                    series = (
                        v.get("y_keys")
                        or v.get("series")
                        or [f"series_{i + 1}" for i in range(len(values))]
                    )
                    return [
                        {
                            x_key: labels[i],
                            **{
                                sname: (values[j][i] if i < len(values[j]) else None)
                                for j, sname in enumerate(series)
                            },
                        }
                        for i in range(len(labels))
                    ]
                y_key = (v.get("y_keys") or ["value"])[0] if v.get("y_keys") else "value"
                return [{x_key: lab, y_key: val} for lab, val in zip(labels, values)]
            # Columnar wide shape: all values are lists → transpose to rows.
            list_cols = {k: val for k, val in v.items() if isinstance(val, list)}
            if len(list_cols) >= 2:
                n = max((len(c) for c in list_cols.values()), default=0)
                # Prefer a label-like first column; else keep dict order.
                label_key = next(
                    (k for k in ("months", "label", "x", "date", "period", "category") if k in list_cols),
                    next(iter(list_cols)),
                )
                series_keys = [k for k in list_cols if k != label_key]
                rows = []
                for i in range(n):
                    row: dict = {label_key: list_cols[label_key][i] if i < len(list_cols[label_key]) else None}
                    for sk in series_keys:
                        row[sk] = list_cols[sk][i] if i < len(list_cols[sk]) else None
                    rows.append(row)
                return rows
            return []
        return v

    @model_validator(mode="after")
    def _backfill_axis_keys(self):
        """When ``x_key`` / ``y_keys`` were not supplied, infer them from the
        (normalized) first data row so exporters never dereference a missing
        key.
        """
        if (not self.x_key) and self.data and isinstance(self.data[0], dict):
            keys = list(self.data[0].keys())
            if keys:
                self.x_key = keys[0]
                if not self.y_keys:
                    self.y_keys = keys[1:]
        return self


class InsightSpec(BaseModel):
    """One insight bullet rendered on the report card."""

    icon: str = "lightbulb"  # lucide icon name
    text: str


class ActionSpec(BaseModel):
    """A follow-up action chip rendered on the report card."""

    label: str
    prompt: str


class SectionSpec(BaseModel):
    """A custom narrative section rendered in the document body.

    Used by Claude-style docx / pdf / pptx renderers to add headings
    + paragraphs (and optional bullet lists) for things like
    "Methodology", "Data Source", "Recommendations", or any other
    custom section the data agent wants to surface.

    The optional `bullets` list is rendered as a bulleted list under
    the heading; `content` is rendered as a paragraph.  At least one
    of `content` / `bullets` should be set.
    """

    title: str
    content: str = ""
    bullets: list[str] = Field(default_factory=list)
    type: str = "narrative"  # narrative | methodology | findings | recommendations

    @model_validator(mode="before")
    @classmethod
    def _normalize_section_keys(cls, data: Any) -> Any:
        """Accept the varying key names the data-agent LLM emits.

        The agent has been observed emitting sections as
        ``{"title", "body"}``, ``{"heading", "body"}``,
        ``{"heading", "paragraphs"}``, ``{"heading", "bullets"}`` or
        ``{"name", "text"}``.  Map those onto the canonical
        ``title`` / ``content`` / ``bullets`` fields so the renderers
        (docx / pdf / pptx) never silently drop a section because it
        used a slightly different key name.
        """
        if not isinstance(data, dict):
            return data
        norm: dict = dict(data)
        if "title" not in norm or not norm.get("title"):
            for alt in ("heading", "name"):
                if norm.get(alt):
                    norm["title"] = norm[alt]
                    break
        if "content" not in norm or not norm.get("content"):
            for alt in ("body", "text"):
                if norm.get(alt):
                    norm["content"] = norm[alt]
                    break
        if not norm.get("bullets"):
            for alt in ("paragraphs", "lines"):
                if norm.get(alt):
                    norm["bullets"] = norm[alt]
                    break
        return norm


class ReportCardPayload(BaseModel):
    """The in-chat report surface — what ReportCard.jsx renders.

    Always emitted by FINALIZE for report-style requests.  The frontend
    picks the primary surface (card vs artifact) from `user_signal`.
    """

    title: str
    source: str = ""
    generated_at: str = ""
    summary: str = ""
    kpis: list[KPISpec] = Field(default_factory=list)
    chart: Optional[ChartSpec] = None
    insights: list[InsightSpec] = Field(default_factory=list)
    next_step: Optional[str] = None
    actions: list[ActionSpec] = Field(default_factory=list)
    user_signal: str = "default"
    warnings: list[str] = Field(default_factory=list)
    # --- Claude-style fields (v2) ---------------------------------------
    # Methodology / how the data was gathered.  Renders as a section
    # in docx / pdf / pptx; appended to the executive summary in the
    # HTML sidecar.
    methodology: str = ""
    # Key findings — typically a richer, narrative version of `insights`.
    # Renders as a section in docx / pdf / pptx with one paragraph per
    # finding (no bullet markers).
    key_findings: list[InsightSpec] = Field(default_factory=list)
    # Recommendations — action items the user can take next.
    recommendations: list[InsightSpec] = Field(default_factory=list)
    # Free-form narrative sections in the order they should appear.
    # Each section renders as a heading + paragraph/bullets.
    sections: list[SectionSpec] = Field(default_factory=list)
    # SQL or pseudo-code that produced the data.  Renders in a fenced
    # code block in docx / pdf and a <pre> in HTML.
    sql: str = ""
    # --- Phase: fully-dynamic document generation -------------------------
    # An ordered list of typed blocks (cover / section_divider / kpi_grid /
    # data_table / chart / callout / comparison / timeline / findings /
    # recommendations / methodology / appendix / ...).  When present, the
    # docx / pptx renderers execute these blocks directly instead of the
    # fixed legacy layout — making the document structure fully data-driven
    # and authored by the agent (or the server-side architect).
    blocks: list[Any] = Field(default_factory=list)
    # --- Deck-specific authoring (2026-08-29) ------------------------------
    # The agent may hand the deck pipeline a complete slide-by-slide
    # structure (title / subtitle / bullets / layout per slide).  When
    # present, the deck exporter renders EXACTLY these slides instead of
    # re-planning a generic structure from summary/kpis/sections — so a
    # consulting-style narrative the agent wrote in chat survives into the
    # PPTX instead of becoming "Notes / Key Metrics / Methodology" filler.
    # Each entry: {"title", "subtitle"?, "bullets"?, "layout"?} where
    # ``layout`` is a valid DeckPlan layout (cover, agenda, kpi_grid,
    # chart_full, chart_with_bullets, findings_cards, insights_bullets,
    # recommendations, data_table, methodology, section_divider, closing,
    # executive_brief → insights_bullets alias).
    slides: list[Any] = Field(default_factory=list)
    # --- Spreadsheet-specific authoring (2026-08-29) ------------------------
    # One sheet per logical view for XLSX exports. Each entry:
    # {"title": "...", "columns"?: [...], "rows": [{col: value, ...}, ...],
    #  "summary"?: "..."} — the xlsx renderer emits these sheets verbatim
    # when present, falling back to the summary/kpis/chart layout otherwise.
    sheets: list[Any] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _sanitize_payload(cls, data: Any) -> Any:
        """Make `model_validate` tolerant of agent-supplied `source_json`.

        The raw payload persisted by ``create_artifact`` / ``run_sandbox_skill``
        is not guaranteed to match these typed shapes exactly.  Common
        deviations that would otherwise raise a ``ValidationError`` and force
        the export path to fall back to a *minimal* (cover-only) payload:

        * ``chart`` supplied as boolean ``True`` (a "yes, include a chart"
          flag) instead of a ``ChartSpec`` dict.
        * ``kpis`` / ``insights`` / ``key_findings`` / ``recommendations`` /
          ``sections`` supplied as ``None`` or a single dict rather than a list.

        Normalize all of these so re-rendering (``GET /download?format=…``)
        reproduces the full rich document the agent authored.
        """
        if not isinstance(data, dict):
            return data
        norm: dict = dict(data)
        # `chart: true` is a flag, not a spec — drop it to None so the
        # renderer's "has_chart" check simply sees no chart instead of
        # raising.  A real ChartSpec (dict or already-validated instance)
        # must be preserved — dropping it silently deletes every chart.
        chart_value = norm.get("chart")
        if isinstance(chart_value, bool) or (
            chart_value is not None
            and not isinstance(chart_value, (dict, ChartSpec))
        ):
            norm["chart"] = None
        for list_key in (
            "kpis",
            "insights",
            "key_findings",
            "recommendations",
            "sections",
            "actions",
            "warnings",
            "blocks",
            "slides",
        ):
            v = norm.get(list_key)
            if v is None:
                continue
            if not isinstance(v, list):
                norm[list_key] = [v]
        # Internal fetch-failure noise must never leak into methodology.
        # The data agent's failure message ("Data sourced from
        # fetch_data_batch (0 rows, 0 columns). Cached at unknown.") is a
        # debugging string, not a methodology — blanking it lets the
        # renderers skip the methodology slide entirely instead of showing
        # the embarrassing internal error.
        _methodology = norm.get("methodology")
        if isinstance(_methodology, str) and (
            "0 rows, 0 columns" in _methodology
            or "fetch_data_batch" in _methodology
            or "Cached at unknown" in _methodology
        ):
            norm["methodology"] = ""
        return norm


# ---------------------------------------------------------------------------
# DeckPlan / SlidePlan — the deck-planner output contract
# ---------------------------------------------------------------------------


class ChartSpecInSlide(BaseModel):
    """Chart specification within a single slide plan.

    Mirrors ``ChartSpec`` but scoped to a deck slide: it carries the
    column keys the structured renderer should chart (bar / line / pie)
    rather than a fully materialized ``data`` list (the renderer pulls
    rows from the profile / payload at build time).
    """

    chart_type: str = "bar"  # bar | line | pie
    x_key: str = ""
    y_keys: list[str] = Field(default_factory=list)
    title: str = ""


class KPISpecInSlide(BaseModel):
    """A single KPI tile the planner wants on a KPI-grid slide."""

    label: str
    value: str
    delta: Optional[str] = None
    caption: Optional[str] = None


class SlidePlan(BaseModel):
    """One slide in a DeckPlan — the planner's output contract.

    ``layout`` selects the layout engine entry (cover | agenda |
    kpi_grid | chart_full | chart_with_bullets | findings_cards |
    insights_bullets | recommendations | data_table | methodology |
    section_divider | closing).  ``narrative_role`` tags the slide's
    position in the narrative arc so the router / polish pass can reason
    about flow (hook | context | evidence | insight | action | closing).
    """

    layout: str  # see layout enum above
    title: str
    subtitle: str = ""
    bullets: list[str] = Field(default_factory=list)
    chart_spec: Optional[ChartSpecInSlide] = None
    kpi_specs: list[KPISpecInSlide] = Field(default_factory=list)
    notes: str = ""
    narrative_role: str = "context"  # hook | context | evidence | insight | action | closing
    # --- CEO-grade cover / summary extras (additive) ----------------------
    # ``period`` renders on the cover as the report period line (e.g.
    # "July 2026 · vs June 2026").  ``callouts`` renders risk (red) /
    # opportunity (green) boxes on the kpi_grid / summary slide.
    period: str = ""
    callouts: list[dict[str, str]] = Field(default_factory=list)  # [{type: risk|opportunity, text}]
    # --- Phase 1B: assertion-style headline + body budgets (additive) ------
    headline_style: str = "topic"  # topic | assertion
    max_bullets: int = 5
    max_words_per_bullet: int = 12
    # --- Materialized data (optional) -------------------------------------
    # The planner normally carries only column keys (``chart_spec``), and the
    # renderer pulls rows from the payload at build time.  Explicit rows set
    # here (e.g. by the sandbox runner) win over the projection.
    chart_rows: list[dict[str, Any]] = Field(default_factory=list)
    table_rows: list[dict[str, Any]] = Field(default_factory=list)
    table_cols: list[str] = Field(default_factory=list)
    # --- Hero image (optional, 2026-08-29) ---------------------------------
    # When set (AI-generated or otherwise), cover / section_divider / closing
    # renderers use it as the full-bleed background. When empty they fall
    # back to deterministic theme-aware SVG hero art.
    hero_image: str = ""


class DeckPlan(BaseModel):
    """Structured content plan for a deck — output of deck_planner.

    The planner turns raw data rows + user intent into this narrative
    slide sequence.  ``deck_type`` drives the smart router (data_report
    / investor_deck / marketing / executive_brief) and
    ``theme_recommendation`` feeds the theme resolver.
    """

    title: str
    deck_type: str = "data_report"  # data_report | investor_deck | marketing | executive_brief
    theme_recommendation: str = "zhanlu-blue"
    palette_recommendation: str = ""
    slides: list[SlidePlan] = Field(default_factory=list)
    summary: str = ""
    methodology: str = ""
    headline_style: str = "topic"  # topic | assertion (deck-wide default)


# ---------------------------------------------------------------------------
# FinalizeResult — the chat-loop return value
# ---------------------------------------------------------------------------


class FinalizeResult(BaseModel):
    """What FINALIZE hands back to the calling-agent loop in agents.py.

    The chat loop merges this into the assistant message and the
    tool_calls_for_frontend list so the existing renderer can display it.
    """

    task_kind: str = "general"
    assistant_content: str = ""
    report_card_payload: Optional[ReportCardPayload] = None
    artifact_ids: list[str] = Field(default_factory=list)
    confidence: ConfidenceScore = Field(default_factory=ConfidenceScore)
    user_signal: str = "default"
    plan_summary: Optional[dict] = None
    observations: list[ObservationRecord] = Field(default_factory=list)
    warnings: list[str] = Field(
        default_factory=list,
        description="Non-fatal warnings emitted during synthesis / finalize (e.g. LLM failure, row cap).",
    )
