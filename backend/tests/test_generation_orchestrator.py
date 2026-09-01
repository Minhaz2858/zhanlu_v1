"""Unit tests for the server-driven generation orchestrator.

Covers the two guarantees from the end-to-end fix (Q1):
  1. fulfill_markers() properly *awaits* the async _create_artifact_tool and
     strips markers from the visible text.
  2. ensure_artifact_for_doc_request() synthesizes a fallback artifact when
     the user asked for a file but the LLM produced neither a marker nor a
     successful create_artifact tool call.

These tests stub _create_artifact_tool so no DB / exporter / filesystem is
touched — we assert on the routing logic, not the rendering pipeline.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services import generation_orchestrator as orch


# ---------------------------------------------------------------------------
# fulfill_markers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fulfill_markers_awaits_handler_and_strips_marker():
    content = (
        "Here is your report.\n"
        '◤MD_DOCX◤{"md_path": "outputs/r.md", "filename": "Report.docx"}◤END_MD_DOCX◤'
    )
    fake_result = {"success": True, "artifact_id": "a1", "file_url": "/x"}

    with patch.object(orch, "_create_artifact_tool", new=AsyncMock(return_value=fake_result)) as m:
        cleaned, created = await orch.fulfill_markers(
            content, db=object(), context={"conversation_id": "c1", "agent_app_id": "app1"}
        )

    assert m.await_count == 1, "handler must be awaited exactly once"
    assert created == [fake_result]
    assert "◤" not in cleaned and "END_MD_DOCX" not in cleaned
    assert "Here is your report." in cleaned


@pytest.mark.asyncio
async def test_fulfill_markers_passes_path_payload_through():
    content = '◤PPTX◤{"slides_path": "outputs/deck.json", "filename": "Deck.pptx"}◤END_PPTX◤'
    captured = {}

    async def fake(args, db=None, context=None):
        captured.update(args)
        return {"success": True, "artifact_id": "p1"}

    with patch.object(orch, "_create_artifact_tool", new=fake):
        await orch.fulfill_markers(content, db=object(), context={})

    assert captured["type"] == "pptx"
    assert captured["title"] == "Deck.pptx"
    assert captured["skill"] == "pptx"
    # the source path is forwarded so a path-aware renderer can pick it up
    assert captured["payload"]["slides_path"] == "outputs/deck.json"


@pytest.mark.asyncio
async def test_fulfill_markers_no_markers_returns_content_unchanged():
    content = "Just prose, no markers here."
    with patch.object(orch, "_create_artifact_tool", new=AsyncMock()) as m:
        cleaned, created = await orch.fulfill_markers(content, db=object(), context={})
    assert m.await_count == 0
    assert cleaned == content
    assert created == []


@pytest.mark.asyncio
async def test_fulfill_markers_handler_failure_is_nonfatal():
    content = '◤MD_DOCX◤{"md_path": "o.md", "filename": "R.docx"}◤END_MD_DOCX◤'
    with patch.object(orch, "_create_artifact_tool", new=AsyncMock(side_effect=RuntimeError("boom"))):
        cleaned, created = await orch.fulfill_markers(content, db=object(), context={})
    # marker still stripped, no exception propagated, nothing created
    assert "◤" not in cleaned
    assert created == []


# ---------------------------------------------------------------------------
# ensure_artifact_for_doc_request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_creates_artifact_when_none_produced():
    fake_result = {"success": True, "artifact_id": "fb1", "file_url": "/f"}
    with patch.object(orch, "_create_artifact_tool", new=AsyncMock(return_value=fake_result)) as m:
        result = await orch.ensure_artifact_for_doc_request(
            doc_format="docx",
            assistant_content="# Sales Report\nRevenue grew 20% this quarter.",
            already_created=[],
            tool_calls_for_frontend=[],
            db=object(),
            context={},
        )
    assert result == fake_result
    assert m.await_count == 1
    args = m.call_args.kwargs["args"]
    assert args["type"] == "docx"
    assert args["payload"]["title"]
    assert "Sales Report" in args["payload"]["summary"] or "Sales Report" in args["payload"]["title"]


@pytest.mark.asyncio
async def test_fallback_skipped_when_marker_already_created():
    from app.config import settings

    # Legacy marker path: any marker-produced artifact suppresses the fallback
    # (the goal-contract content gate is a separate, flag-gated behavior).
    with patch.object(settings, "GOAL_CONTRACT_ENABLED", False), patch.object(
        orch, "_create_artifact_tool", new=AsyncMock()
    ) as m:
        result = await orch.ensure_artifact_for_doc_request(
            doc_format="pptx",
            assistant_content="deck",
            already_created=[{"success": True, "artifact_id": "a1"}],
            tool_calls_for_frontend=[],
            db=object(),
            context={},
        )
    assert result is None
    assert m.await_count == 0


@pytest.mark.asyncio
async def test_fallback_skipped_when_tool_call_succeeded():
    tcs = [{
        "name": "create_artifact",
        "results": {"success": True, "artifact_id": "a9"},
    }]
    with patch.object(orch, "_create_artifact_tool", new=AsyncMock()) as m:
        result = await orch.ensure_artifact_for_doc_request(
            doc_format="pdf",
            assistant_content="report",
            already_created=[],
            tool_calls_for_frontend=tcs,
            db=object(),
            context={},
        )
    assert result is None
    assert m.await_count == 0


@pytest.mark.asyncio
async def test_fallback_skipped_for_non_renderable_format():
    # xlsx / md have no artifact renderer — must not fabricate. ``dashboard``
    # is renderable as an html artifact (see test_fallback_dashboard_*) so it
    # is NOT in this skip list.
    for fmt in ("xlsx", "md"):
        with patch.object(orch, "_create_artifact_tool", new=AsyncMock()) as m:
            result = await orch.ensure_artifact_for_doc_request(
                doc_format=fmt,
                assistant_content="data",
                already_created=[],
                tool_calls_for_frontend=[],
                db=object(),
                context={},
            )
        assert result is None
        assert m.await_count == 0


@pytest.mark.asyncio
async def test_fallback_skipped_when_no_doc_format():
    with patch.object(orch, "_create_artifact_tool", new=AsyncMock()) as m:
        result = await orch.ensure_artifact_for_doc_request(
            doc_format=None,
            assistant_content="hello",
            already_created=[],
            tool_calls_for_frontend=[],
            db=object(),
            context={},
        )
    assert result is None
    assert m.await_count == 0


@pytest.mark.asyncio
async def test_fallback_runs_when_tool_call_failed():
    # A failed create_artifact tool call must NOT suppress the fallback.
    tcs = [{"name": "create_artifact", "results": {"success": False, "error": "x"}}]
    fake_result = {"success": True, "artifact_id": "fb2"}
    with patch.object(orch, "_create_artifact_tool", new=AsyncMock(return_value=fake_result)) as m:
        result = await orch.ensure_artifact_for_doc_request(
            doc_format="docx",
            assistant_content="quarterly numbers",
            already_created=[],
            tool_calls_for_frontend=tcs,
            db=object(),
            context={},
        )
    assert result == fake_result
    assert m.await_count == 1


@pytest.mark.asyncio
async def test_fallback_runs_when_tool_call_success_but_no_artifact_id():
    # A partial success (success=True but no artifact_id) must NOT suppress
    # the fallback — nothing downloadable was actually produced.
    tcs = [{"name": "create_artifact", "results": {"success": True}}]  # no artifact_id
    fake_result = {"success": True, "artifact_id": "fb3"}
    with patch.object(orch, "_create_artifact_tool", new=AsyncMock(return_value=fake_result)) as m:
        result = await orch.ensure_artifact_for_doc_request(
            doc_format="pptx",
            assistant_content="quarterly deck",
            already_created=[],
            tool_calls_for_frontend=tcs,
            db=object(),
            context={},
        )
    assert result == fake_result
    assert m.await_count == 1


@pytest.mark.asyncio
async def test_fallback_skipped_when_artifact_ids_present():
    # Legacy behavior (DOC_REQUEST_STRICT_ARTIFACT_MATCH_ENABLED off): the
    # ask_data_agent finalize path (rich or no-data) already attached an
    # artifact this turn — the fallback must not create a user-visible dup.
    from app.config import settings

    with patch.object(settings, "DOC_REQUEST_STRICT_ARTIFACT_MATCH_ENABLED", False), patch.object(
        orch, "_create_artifact_tool", new=AsyncMock()
    ) as m:
        result = await orch.ensure_artifact_for_doc_request(
            doc_format="pptx",
            assistant_content="here is the deck",
            already_created=[],
            tool_calls_for_frontend=[],
            db=object(),
            context={},
            artifact_ids=["art-from-finalize"],
        )
    assert result is None
    assert m.await_count == 0


# ---------------------------------------------------------------------------
# DOC_REQUEST_STRICT_ARTIFACT_MATCH_ENABLED: report-card artifacts must not
# preempt an explicit file request (Fix 1a).
# ---------------------------------------------------------------------------


class _FakeArtifact:
    def __init__(self, artifact_type):
        self.artifact_type = artifact_type


class _FakeDB:
    def __init__(self, artifacts):
        self._artifacts = artifacts

    def get(self, model, artifact_id):
        return self._artifacts.get(artifact_id)


@pytest.mark.asyncio
async def test_fallback_runs_when_artifact_ids_are_report_cards_only():
    # A report card (artifact_type=html_report) does NOT satisfy an explicit
    # pptx request, even though the finalize path attached an artifact id.
    from app.config import settings

    fake_db = _FakeDB({"rc1": _FakeArtifact("html_report")})
    with patch.object(settings, "DOC_REQUEST_STRICT_ARTIFACT_MATCH_ENABLED", True), patch.object(
        orch, "_create_artifact_tool", new=AsyncMock(return_value={"success": True, "artifact_id": "a2"})
    ) as m:
        result = await orch.ensure_artifact_for_doc_request(
            doc_format="pptx",
            assistant_content="here is the deck",
            already_created=[],
            tool_calls_for_frontend=[],
            db=fake_db,
            context={},
            artifact_ids=["rc1"],
        )
    assert result is not None
    assert m.await_count == 1


@pytest.mark.asyncio
async def test_fallback_skipped_when_artifact_matches_doc_format():
    # A real pptx artifact DOES satisfy an explicit pptx request.
    from app.config import settings

    fake_db = _FakeDB({"p1": _FakeArtifact("pptx")})
    with patch.object(settings, "GOAL_CONTRACT_ENABLED", False), patch.object(
        settings, "DOC_REQUEST_STRICT_ARTIFACT_MATCH_ENABLED", True
    ), patch.object(orch, "_create_artifact_tool", new=AsyncMock()) as m:
        result = await orch.ensure_artifact_for_doc_request(
            doc_format="pptx",
            assistant_content="deck",
            already_created=[],
            tool_calls_for_frontend=[],
            db=fake_db,
            context={},
            artifact_ids=["p1"],
        )
    assert result is None
    assert m.await_count == 0


@pytest.mark.asyncio
async def test_dashboard_fallback_runs_when_only_report_card_present():
    # The dashboard fallback branch must be reachable again: a report card
    # must not preempt an explicit dashboard request.
    from app.config import settings

    fake_db = _FakeDB({"rc1": _FakeArtifact("html_report")})
    with patch.object(settings, "DOC_REQUEST_STRICT_ARTIFACT_MATCH_ENABLED", True), patch.object(
        orch, "_create_artifact_tool", new=AsyncMock(return_value={"success": True, "artifact_id": "d1"})
    ) as m:
        result = await orch.ensure_artifact_for_doc_request(
            doc_format="dashboard",
            assistant_content="make a dashboard",
            already_created=[],
            tool_calls_for_frontend=[],
            db=fake_db,
            context={},
            artifact_ids=["rc1"],
        )
    assert result is not None
    assert m.await_count == 1


@pytest.mark.asyncio
async def test_fallback_skipped_when_sandbox_tool_succeeded():
    # run_sandbox_skill is an artifact engine too — its success suppresses
    # the fallback exactly like a create_artifact success.
    tcs = [{
        "name": "run_sandbox_skill",
        "results": {"success": True, "artifact_id": "sb1"},
    }]
    with patch.object(orch, "_create_artifact_tool", new=AsyncMock()) as m:
        result = await orch.ensure_artifact_for_doc_request(
            doc_format="pptx",
            assistant_content="deck",
            already_created=[],
            tool_calls_for_frontend=tcs,
            db=object(),
            context={},
        )
    assert result is None
    assert m.await_count == 0


@pytest.mark.asyncio
async def test_fallback_runs_when_sandbox_failed_and_no_artifact_ids():
    # A failed sandbox run must NOT suppress the fallback.
    tcs = [{"name": "run_sandbox_skill", "results": {"success": False, "error": "timeout"}}]
    fake_result = {"success": True, "artifact_id": "fb4"}
    with patch.object(orch, "_create_artifact_tool", new=AsyncMock(return_value=fake_result)) as m:
        result = await orch.ensure_artifact_for_doc_request(
            doc_format="docx",
            assistant_content="quarterly numbers",
            already_created=[],
            tool_calls_for_frontend=tcs,
            db=object(),
            context={},
            artifact_ids=[],
        )
    assert result == fake_result
    assert m.await_count == 1


@pytest.mark.asyncio
async def test_fulfill_markers_multiple_markers_all_created():
    content = (
        "Intro text.\n"
        '◤MD_DOCX◤{"md_path": "outputs/r.md", "filename": "R.docx"}◤END_MD_DOCX◤\n'
        "middle\n"
        '◤PPTX◤{"slides_path": "outputs/d.json", "filename": "D.pptx"}◤END_PPTX◤'
    )

    async def fake(args, db=None, context=None):
        return {"success": True, "artifact_id": f"id-{args['type']}"}

    with patch.object(orch, "_create_artifact_tool", new=fake) :
        cleaned, created = await orch.fulfill_markers(content, db=object(), context={})
    assert len(created) == 2
    assert {c["artifact_id"] for c in created} == {"id-docx", "id-pptx"}
    assert "◤" not in cleaned and "Intro text." in cleaned and "middle" in cleaned


@pytest.mark.asyncio
async def test_fulfill_markers_unknown_kind_skipped_but_stripped():
    # ◤XLSX◤ is not in SUPPORTED_KINDS — find_markers skips it entirely, so
    # it is neither created nor stripped (left as visible text). A *supported*
    # kind in the same message must still be processed.
    content = (
        '◤MD_DOCX◤{"md_path": "o.md", "filename": "R.docx"}◤END_MD_DOCX◤'
    )

    async def fake(args, db=None, context=None):
        return {"success": True, "artifact_id": "only-one"}

    with patch.object(orch, "_create_artifact_tool", new=fake):
        cleaned, created = await orch.fulfill_markers(content, db=object(), context={})
    assert len(created) == 1
    assert "◤" not in cleaned


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_title_from_prose_prefers_h1():
    assert orch._title_from_prose("# Q3 Report\nbody", "fb") == "Q3 Report"


def test_title_from_prose_falls_back_to_first_sentence():
    assert orch._title_from_prose("Revenue is up. More detail here.", "fb") == "Revenue is up."


def test_title_from_prose_uses_fallback_when_empty():
    assert orch._title_from_prose("", "docx-export") == "docx-export"


def test_prose_to_summary_truncates_long_text():
    long_text = "word " * 5000
    out = orch._prose_to_summary(long_text, max_chars=100)
    assert len(out) <= 100  # hard cap including ellipsis


def test_prose_to_summary_hard_cap_without_spaces():
    # CJK / URL text with no spaces must still be hard-capped.
    out = orch._prose_to_summary("漢字" * 500, max_chars=50)
    assert len(out) <= 50


# ---------------------------------------------------------------------------
# Dashboard wiring (DASHBOARD marker + dashboard fallback + helpers)
# ---------------------------------------------------------------------------


def test_dashboard_marker_kind_is_recognized():
    """The DASHBOARD marker kind must be in SUPPORTED_KINDS so the parser
    surfaces it (and the orchestrator routes it to the html artifact)."""
    from app.services.artifact_markers import SUPPORTED_KINDS

    assert "DASHBOARD" in SUPPORTED_KINDS


def test_dashboard_marker_parses_to_html_path_payload():
    text = '◤DASHBOARD◤{"html_path": "outputs/d.html", "filename": "D.html", "title": "D"}◤END_DASHBOARD◤'
    markers = list(orch.find_markers(text))  # exported via import
    # The find_markers used here is the artifact_markers one; import directly.
    from app.services.artifact_markers import find_markers as real_find

    markers = list(real_find(text))
    assert len(markers) == 1
    m = markers[0]
    assert m.kind == "DASHBOARD"
    assert m.payload["html_path"] == "outputs/d.html"
    assert m.payload["title"] == "D"


def test_marker_to_artifact_args_dashboard_routes_to_html_type():
    args = orch._marker_to_artifact_args(
        "DASHBOARD",
        {"html_path": "outputs/d.html", "filename": "D.html", "title": "D"},
        "D.html",
    )
    assert args is not None
    assert args["type"] == "html"
    assert args["title"] == "D"  # marker-supplied title wins over filename
    assert args["skill"] == "dashboard"
    # html_content is missing because the file doesn't exist on disk —
    # the handler will surface a "no-content" html artifact (tested below).
    assert args["payload"].get("html_content") is None


def test_marker_to_artifact_args_dashboard_reads_html_content(tmp_path):
    html_path = tmp_path / "outputs" / "d.html"
    html_path.parent.mkdir(parents=True)
    html_path.write_text("<!DOCTYPE html><body>hello</body>", encoding="utf-8")
    args = orch._marker_to_artifact_args(
        "DASHBOARD",
        {"html_path": str(html_path), "filename": "D.html"},
        "D.html",
    )
    assert args["type"] == "html"
    assert "<body>hello</body>" in args["payload"]["html_content"]
    assert args["title"] == "D.html"  # falls back to filename when no title


def test_read_dashboard_html_caps_oversized_file(tmp_path):
    big = tmp_path / "big.html"
    big.write_text("x" * (2 * 1024 * 1024 + 1), encoding="utf-8")
    assert orch._read_dashboard_html(str(big)) is None


def test_read_dashboard_html_returns_none_for_missing_file():
    assert orch._read_dashboard_html("/nonexistent/path/x.html") is None
    assert orch._read_dashboard_html("") is None
    assert orch._read_dashboard_html(None) is None


def test_rows_to_kpis_sums_and_maxes_numeric_columns():
    rows = [
        {"region": "EMEA", "revenue": 100, "units": 5},
        {"region": "APAC", "revenue": 200, "units": 7},
        {"region": "NA",   "revenue": 50,  "units": 3},
    ]
    kpis = orch._rows_to_kpis(rows)
    labels = [k["label"] for k in kpis]
    # both sum and max for revenue, then for units (or whichever runs first)
    assert any("Total revenue" in l for l in labels)
    assert any("Max revenue"   in l for l in labels)
    # the sums match
    by_label = {k["label"]: k for k in kpis}
    assert by_label["Total revenue"]["value"] == 350
    assert by_label["Max revenue"]["value"]   == 200


def test_pick_chart_columns_picks_label_then_value():
    rows = [
        {"region": "EMEA", "revenue": 100},
        {"region": "APAC", "revenue": 200},
    ]
    label, value = orch._pick_chart_columns(rows)
    assert label == "region"
    assert value == "revenue"


def test_mine_ask_data_rows_extracts_and_caps():
    big_rows = [{"x": i} for i in range(orch._DASHBOARD_MAX_ROWS + 50)]
    tcs = [
        {"name": "web_search", "results": {"ok": True}},
        {"name": "ask_data_agent", "results": {"rows": big_rows, "source_name": "t"}},
    ]
    rows = orch._mine_ask_data_rows(tcs)
    assert len(rows) == orch._DASHBOARD_MAX_ROWS  # capped, not 1050


def test_synthesize_dashboard_html_is_self_contained():
    rows = [{"region": "EMEA", "revenue": 100}, {"region": "APAC", "revenue": 200}]
    html = orch._synthesize_dashboard_html("Test", rows, "summary text", "source")
    assert html.lstrip().startswith("<!DOCTYPE html>")
    assert "Chart(" in html
    assert "EMEA" in html and "APAC" in html
    assert "Test" in html
    # No external script tag should depend on origin — Chart.js is jsDelivr CDN
    # with an SRI hash, so it works equally for inline preview and download.
    assert 'integrity="sha384-' in html


def test_synthesize_dashboard_html_handles_empty_rows():
    html = orch._synthesize_dashboard_html("Empty", [], "no data summary", "x")
    assert html.lstrip().startswith("<!DOCTYPE html>")
    assert "no data summary" in html


@pytest.mark.asyncio
async def test_fallback_dashboard_mines_ask_data_rows_and_renders_html():
    """doc_format='dashboard' must guarantee a real interactive HTML artifact
    even when no marker / tool call was emitted — the chain that used to die
    silently now produces a self-contained dashboard."""
    tcs = [
        {"name": "ask_data_agent", "results": {
            "rows": [{"region": "EMEA", "revenue": 100},
                     {"region": "APAC", "revenue": 200}],
            "source_name": "orders",
        }},
    ]
    fake_result = {"success": True, "artifact_id": "dash1", "file_url": "/d"}

    with patch.object(orch, "_create_artifact_tool", new=AsyncMock(return_value=fake_result)) as m:
        result = await orch.ensure_artifact_for_doc_request(
            doc_format="dashboard",
            assistant_content="",  # empty prose
            already_created=[],
            tool_calls_for_frontend=tcs,
            db=object(),
            context={},
        )

    assert result == fake_result
    assert m.await_count == 1
    args = m.call_args.kwargs["args"]
    assert args["type"] == "html"
    assert args["skill"] == "dashboard"
    # real interactive HTML embedded, not a ReportCard skeleton
    assert "<!DOCTYPE html>" in args["payload"]["html_content"]
    assert "Chart(" in args["payload"]["html_content"]
    assert "EMEA" in args["payload"]["html_content"]
    # row count surfaced for the user
    assert args["payload"]["row_count"] == 2


@pytest.mark.asyncio
async def test_fallback_dashboard_with_no_rows_renders_empty_state():
    """When ask_data_agent returned no rows, the dashboard still renders —
    a prose-only empty-state shell, not silence."""
    fake_result = {"success": True, "artifact_id": "dash-empty"}

    with patch.object(orch, "_create_artifact_tool", new=AsyncMock(return_value=fake_result)) as m:
        result = await orch.ensure_artifact_for_doc_request(
            doc_format="dashboard",
            assistant_content="No data found for that query.",
            already_created=[],
            tool_calls_for_frontend=[],
            db=object(),
            context={},
        )

    assert result == fake_result
    args = m.call_args.kwargs["args"]
    assert args["type"] == "html"
    assert "No data found" in args["payload"]["html_content"]


@pytest.mark.asyncio
async def test_fallback_dashboard_skipped_when_already_created():
    """Duplicate-safety: a finalizer that already attached an artifact this
    turn must suppress the dashboard fallback (no double artifact)."""
    from app.config import settings

    with patch.object(settings, "GOAL_CONTRACT_ENABLED", False), patch.object(
        orch, "_create_artifact_tool", new=AsyncMock()
    ) as m:
        result = await orch.ensure_artifact_for_doc_request(
            doc_format="dashboard",
            assistant_content="done",
            already_created=[{"success": True, "artifact_id": "x"}],
            tool_calls_for_frontend=[],
            db=object(),
            context={},
        )
    assert result is None
    assert m.await_count == 0


@pytest.mark.asyncio
async def test_fallback_dashboard_handler_failure_is_nonfatal():
    with patch.object(orch, "_create_artifact_tool", new=AsyncMock(side_effect=RuntimeError("boom"))):
        result = await orch.ensure_artifact_for_doc_request(
            doc_format="dashboard",
            assistant_content="data",
            already_created=[],
            tool_calls_for_frontend=[{"name": "ask_data_agent", "results": {"rows": [{"a": 1}]}}],
            db=object(),
            context={},
        )
    # must not raise
    assert result is None
