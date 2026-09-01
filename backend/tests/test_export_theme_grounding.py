"""Tests for ExportService theme + data-grounding wiring (plan items E + B).

Locks in four behaviors:

* ``ExportContext.theme_name`` round-trips and stays JSON-serializable (E).
* ``ExportService._resolved_theme_name`` returns a validate-able theme name:
  brand ``theme_tokens`` present → the vendored base name (a hex kit name
  would NOT pass ``validate_theme_name``); otherwise ``theme_name`` /
  ``theme`` / the ``zhanlu-blue`` default (B).
* ``_render_deck_pipeline`` threads ``theme_tokens`` + ``user_context`` into
  ``build_deck_plan`` so the planner sees the brand palette and the
  user/brand context — the deck stops being a one-template-fits-all (B).
* ``_render_and_store`` grounds the deck rows in the REAL query rows
  (``collect_grounded_rows``) when ``PPT_DECK_DATA_GROUNDING_ENABLED``,
  falling back to ``payload.chart.data`` otherwise; the empty-grounding
  fallback still yields the payload rows (B).
* ``render_pptx_deck`` is a thin public wrapper so the tool path and the
  download path share the SAME pipeline renderer (B).
"""

import json

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.config import settings


def _make_db():
    engine = sa.create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _sample_payload():
    from app.services.synexia.contracts import (
        ChartSpec,
        KPISpec,
        ReportCardPayload,
    )

    return ReportCardPayload(
        title="Sales by region",
        source="erp",
        generated_at="2026-08-01T00:00:00Z",
        summary="Region sales",
        kpis=[KPISpec(label="Total", value="100", delta="+5%", caption="All regions")],
        chart=ChartSpec(
            type="bar",
            title="Sales by region",
            x_key="region",
            y_keys=["sales"],
            unit="USD",
            data=[
                {"region": "EA", "sales": 10},
                {"region": "WA", "sales": 20},
            ],
        ),
        insights=[],
        actions=[],
        next_step="",
        user_signal="export",
        warnings=[],
    )


def _nonrich_payload():
    """A payload with NO narrative fields — forces the row-based planner.

    The rich-payload short-circuit (2026-08-28) renders any payload with
    summary/chart/key_findings DIRECTLY via DocumentPlan → DeckPlan, so a
    test that wants to exercise the row-based deck planner (theme / user
    context threading) must pass a payload with no rich narrative.
    """
    from app.services.synexia.contracts import (
        KPISpec,
        ReportCardPayload,
    )

    return ReportCardPayload(
        title="Sales by region",
        source="erp",
        generated_at="2026-08-01T00:00:00Z",
        summary="",
        kpis=[KPISpec(label="Total", value="100", delta="+5%", caption="All regions")],
        chart=None,
        insights=[],
        actions=[],
        next_step="",
        user_signal="export",
        warnings=[],
    )


def _make_artifact(db, *, conversation_id="conv-1", execution_id=None):
    from app.models.artifact import Artifact

    payload = _sample_payload()
    artifact = Artifact(
        id="a-grounding",
        conversation_id=conversation_id,
        execution_id=execution_id,
        created_by_agent_id="test-agent",
        artifact_type="html_report",
        title=payload.title,
        description=payload.summary,
        status="preview_ready",
        visibility="conversation_private",
        metadata_json={"report_card_payload": payload.model_dump()},
    )
    db.add(artifact)
    db.commit()
    return artifact


# ── Work item E: ExportContext.theme_name round-trip ─────────────────────

def test_theme_name_roundtrip():
    from app.services.artifacts.exporters._common import ExportContext

    ctx = ExportContext(theme_name="ocean-blue", theme_tokens={"primary": "#000"})
    assert ctx.theme_name == "ocean-blue"
    # Must stay JSON-serializable (it rides in metadata / cache keys).
    dumped = json.dumps(
        {"theme_name": ctx.theme_name, "theme_tokens": ctx.theme_tokens}
    )
    assert '"ocean-blue"' in dumped


# ── Work item B: _resolved_theme_name ────────────────────────────────────

def test_resolved_theme_name_with_brand_tokens_uses_vendored_base():
    from app.services.artifacts.exporters._common import ExportContext
    from app.services.artifacts.exporters.service import ExportService

    ctx = ExportContext(
        theme="zhanlu-blue",
        theme_name="brand-hex-name",  # would NOT pass validate_theme_name
        theme_tokens={"primary": "#7c3aed"},
    )
    assert ExportService._resolved_theme_name(ctx) == "zhanlu-blue"


def test_resolved_theme_name_prefers_theme_name():
    from app.services.artifacts.exporters._common import ExportContext
    from app.services.artifacts.exporters.service import ExportService

    ctx = ExportContext(theme="ocean-blue", theme_name="midnight")
    assert ExportService._resolved_theme_name(ctx) == "midnight"


def test_resolved_theme_name_falls_back_to_theme_then_default():
    from app.services.artifacts.exporters._common import ExportContext
    from app.services.artifacts.exporters.service import ExportService

    assert ExportService._resolved_theme_name(ExportContext()) == "zhanlu-blue"


# ── Work item B: _render_deck_pipeline threads theme + user context ─────

def test_render_deck_pipeline_threads_theme_and_user_context(monkeypatch):
    from app.services.artifacts.exporters._common import ExportContext
    from app.services.artifacts.exporters.service import ExportService
    from app.services.synexia.contracts import DeckPlan, SlidePlan

    captured = {}

    async def fake_classify(intent, explicit=None, allow_llm_fallback=False):
        return "data_report"

    async def fake_plan(
        user_intent, rows,
        theme_recommendation=None, profile_name=None, *,
        theme_tokens=None, user_context=None,
    ):
        captured["intent"] = user_intent
        captured["rows"] = rows
        captured["theme_recommendation"] = theme_recommendation
        captured["theme_tokens"] = theme_tokens
        captured["user_context"] = user_context
        return (
            DeckPlan(
                title="Sales by region",
                deck_type="data_report",
                slides=[SlidePlan(layout="cover", title="Sales by region")],
            ),
            "data_report",
        )

    def fake_render(plan, rows, ctx):
        return b"deck-bytes", {"status": "PASS", "summary": {"total": 1, "fail": 0}}

    def fake_context(
        db, *, artifact=None, user_id=None, conversation_id=None,
        user_message="", agent_app_id=None,
        org_id="default-org", app_id="default-app",
    ):
        captured["context_user_id"] = user_id
        captured["context_conversation_id"] = conversation_id
        return {"role_text": "CFO", "brand_tokens": {"primary": "#7c3aed"}}

    monkeypatch.setattr(
        "app.services.artifacts.deck_router.classify_profile", fake_classify
    )
    monkeypatch.setattr(
        "app.services.artifacts.deck_planner.build_deck_plan", fake_plan
    )
    monkeypatch.setattr(
        "app.services.artifacts.render_dispatcher.render_pptx_from_plan_sync",
        fake_render,
    )
    monkeypatch.setattr(
        "app.services.artifacts.exporters.service.build_deck_user_context",
        fake_context,
    )

    ctx = ExportContext(
        conversation_id="conv-1",
        theme_tokens={"primary": "#7c3aed"},
        theme_name="brand-hex-name",
    )
    rows = [{"region": "EA", "sales": 10}]
    svc = ExportService(_make_db())
    data, mime, ext, _audit = svc._render_deck_pipeline(
        _nonrich_payload(), ctx, rows,
        user_message="Build me a regional sales deck",
        artifact=None, user_id="u1",
    )

    # The planner receives the vendored base name (NOT the brand hex name),
    # the brand palette, and the user context — so decks are theme-aware.
    assert captured["theme_recommendation"] == "zhanlu-blue"
    assert captured["theme_tokens"] == {"primary": "#7c3aed"}
    assert captured["user_context"] == {
        "role_text": "CFO",
        "brand_tokens": {"primary": "#7c3aed"},
    }
    assert captured["context_user_id"] == "u1"
    assert captured["context_conversation_id"] == "conv-1"
    assert captured["rows"] == rows
    assert captured["intent"] == "Build me a regional sales deck"
    assert data == b"deck-bytes"
    assert ext == "pptx"


# ── Work item B: _render_and_store data grounding ────────────────────────

def test_render_and_store_uses_grounded_rows_when_flag_on(monkeypatch):
    from app.services.artifacts.exporters.service import ExportService

    monkeypatch.setattr(settings, "PPT_DECK_PLANNER_ENABLED", True)
    monkeypatch.setattr(settings, "PPT_DECK_DATA_GROUNDING_ENABLED", True)

    grounded = [{"region": "REAL-EA", "sales": 999}]
    captured = {}

    def fake_collect(db, *, artifact=None, conversation_id=None,
                     execution_id=None, user_message="", limit=2000):
        captured["collect_conversation"] = conversation_id
        captured["collect_execution"] = execution_id
        return grounded

    def fake_pipeline(self_, payload_, ctx_, rows_, **kwargs):
        captured["rows"] = rows_
        return (
            b"deck-bytes",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "deck.pptx",
            {"status": "PASS", "summary": {"total": 1, "fail": 0}},
        )

    monkeypatch.setattr(
        "app.services.artifacts.exporters.service.collect_grounded_rows",
        fake_collect,
    )
    monkeypatch.setattr(ExportService, "_render_deck_pipeline", fake_pipeline)

    db = _make_db()
    artifact = _make_artifact(db, conversation_id="conv-1", execution_id="exec-9")
    svc = ExportService(db)
    data, _mime, _ext = svc._render_and_store(
        artifact, "pptx", user_message="deck", sql=None, source=None, persist=False
    )

    # The REAL query rows win over the LLM-authored payload.chart.data.
    assert captured["rows"] == grounded
    assert captured["collect_conversation"] == "conv-1"
    assert captured["collect_execution"] == "exec-9"
    assert data == b"deck-bytes"


def test_render_and_store_falls_back_to_chart_rows_when_flag_off(monkeypatch):
    from app.services.artifacts.exporters.service import ExportService

    monkeypatch.setattr(settings, "PPT_DECK_PLANNER_ENABLED", True)
    monkeypatch.setattr(settings, "PPT_DECK_DATA_GROUNDING_ENABLED", False)

    captured = {}

    def boom_collect(*args, **kwargs):  # must NOT be called when flag is off
        raise AssertionError("collect_grounded_rows called with flag off")

    def fake_pipeline(self_, payload_, ctx_, rows_, **kwargs):
        captured["rows"] = rows_
        return (
            b"deck-bytes",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "deck.pptx",
            {"status": "PASS", "summary": {"total": 1, "fail": 0}},
        )

    monkeypatch.setattr(
        "app.services.artifacts.exporters.service.collect_grounded_rows",
        boom_collect,
    )
    monkeypatch.setattr(ExportService, "_render_deck_pipeline", fake_pipeline)

    db = _make_db()
    artifact = _make_artifact(db)
    svc = ExportService(db)
    svc._render_and_store(
        artifact, "pptx", user_message="deck", sql=None, source=None, persist=False
    )

    # Historical behavior: rows come straight from payload.chart.data.
    payload_rows = _sample_payload().chart.data
    assert captured["rows"] == payload_rows


def test_render_and_store_empty_grounding_falls_back_to_payload(monkeypatch):
    from app.services.artifacts.exporters.service import ExportService

    monkeypatch.setattr(settings, "PPT_DECK_PLANNER_ENABLED", True)
    monkeypatch.setattr(settings, "PPT_DECK_DATA_GROUNDING_ENABLED", True)

    captured = {}
    monkeypatch.setattr(
        "app.services.artifacts.exporters.service.collect_grounded_rows",
        lambda *a, **k: [],
    )

    def fake_pipeline(self_, payload_, ctx_, rows_, **kwargs):
        captured["rows"] = rows_
        return (
            b"deck-bytes",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "deck.pptx",
            {"status": "PASS", "summary": {"total": 1, "fail": 0}},
        )

    monkeypatch.setattr(ExportService, "_render_deck_pipeline", fake_pipeline)

    db = _make_db()
    artifact = _make_artifact(db)
    svc = ExportService(db)
    svc._render_and_store(
        artifact, "pptx", user_message="deck", sql=None, source=None, persist=False
    )

    assert captured["rows"] == _sample_payload().chart.data


# ── Work item B: render_pptx_deck public wrapper ─────────────────────────

def test_render_pptx_deck_wrapper_uses_pipeline_when_enabled(monkeypatch):
    from app.services.artifacts.exporters._common import ExportContext
    from app.services.artifacts.exporters.service import ExportService

    monkeypatch.setattr(settings, "PPT_DECK_PLANNER_ENABLED", True)
    captured = {}

    def fake_pipeline(self_, payload_, ctx_, rows_, **kwargs):
        captured["rows"] = rows_
        captured["user_message"] = kwargs.get("user_message")
        return (
            b"deck-bytes",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "pptx",
            {"status": "PASS", "summary": {"total": 1, "fail": 0}},
        )

    monkeypatch.setattr(ExportService, "_render_deck_pipeline", fake_pipeline)

    svc = ExportService(_make_db())
    rows = [{"a": 1}]
    data, mime, ext = svc.render_pptx_deck(
        _sample_payload(), ExportContext(conversation_id="conv-1"), rows,
        user_message="hi",
    )
    assert data == b"deck-bytes"
    assert ext == "pptx"
    assert captured["rows"] == rows
    assert captured["user_message"] == "hi"


def test_render_pptx_deck_wrapper_legacy_when_disabled(monkeypatch):
    from app.services.artifacts.exporters._common import ExportContext
    from app.services.artifacts.exporters.service import ExportService

    monkeypatch.setattr(settings, "PPT_DECK_PLANNER_ENABLED", False)
    captured = {}

    def fake_legacy(payload_, ctx_):
        captured["legacy"] = True
        return b"legacy-bytes", "application/vnd.openxmlformats-officedocument.presentationml.presentation", "pptx"

    monkeypatch.setattr(
        "app.services.artifacts.exporters.pptx_export.render", fake_legacy
    )

    svc = ExportService(_make_db())
    data, _mime, ext = svc.render_pptx_deck(_sample_payload(), ExportContext(), [])
    assert captured["legacy"] is True
    assert data == b"legacy-bytes"
    assert ext == "pptx"
