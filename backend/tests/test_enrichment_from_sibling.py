"""Tests for the ``_enrich_args_from_sibling_html_report`` /
``_enrich_payload_from_sibling_html_report`` helpers that fix the
"docx is empty" complaint.

When the agent calls ``run_sandbox_skill(format='docx', data=rows,
title=..., instructions=...)`` after a previous
``finalize_into_artifact`` (or another ``run_sandbox_skill`` for
``format='html'``) the rich rcp lives on the sibling artifact —
NOT on the agent's current call.  The helper must:

1. Find a title-matched sibling with a non-empty rcp.
2. Fall back to the most-recent rich sibling if no title match.
3. Skip sparse sidecars (the ones the sandbox just created for
   the same docx task).
4. Walk the three possible rcp storage locations:
   - ``artifact.metadata_json['report_card_payload']``
   - ``artifact.metadata_json['rcp']``
   - ``version.source_json['rcp']``
5. Never fill in fields the agent already provided.
"""
import json
import uuid

import pytest

from app.services.tool_handlers.artifact_tool import (
    _enrich_payload_from_sibling_html_report,
)
from app.services.tool_handlers.sandbox_tool import (
    _enrich_args_from_sibling_html_report,
)


# ---------------------------------------------------------------------------
# Stubs / in-memory fakes
# ---------------------------------------------------------------------------


class _FakeRCP(dict):
    """Mimic a ReportCardPayload dict with summary / kpis / etc."""


class _FakeVersion:
    def __init__(self, source_json=None):
        self.source_json = source_json or {}


class _FakeArtifact:
    def __init__(self, *, id=None, artifact_type="html_report", title="",
                 metadata_json=None, source_json=None, created_date=None):
        self.id = id or str(uuid.uuid4())
        self.artifact_type = artifact_type
        self.title = title
        self.metadata_json = metadata_json or {}
        self._source_json = source_json
        self.current_version_id = f"v-{self.id}"
        self.created_date = created_date or "2026-07-22T13:00:00"
        self.conversation_id = "conv-1"
        self.is_deleted = False

    @property
    def source_json(self):
        return self._source_json

    @source_json.setter
    def source_json(self, value):
        self._source_json = value


class _FakeQuery:
    def __init__(self, items):
        self._items = items

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def all(self):
        return self._items

    def first(self):
        return self._items[0] if self._items else None


class _FakeDB:
    """Minimal session stub: query(Artifact) returns our preloaded list,
    query(ArtifactVersion) returns versions registered via
    ``add_version``."""

    def __init__(self, artifacts_by_conversation):
        self._artifacts = artifacts_by_conversation
        self._versions = []  # list of _FakeVersion

    def add_version(self, version: _FakeVersion):
        self._versions.append(version)

    def query(self, model):
        name = model.__name__ if hasattr(model, "__name__") else str(model)
        if name == "Artifact":
            flat = []
            for arts in self._artifacts.values():
                flat.extend(arts)
            return _FakeQuery(flat)
        if name == "ArtifactVersion":
            class _VQuery:
                def __init__(self, versions):
                    self.versions = versions

                def filter(self, *args, **kwargs):
                    return self

                def first(self):
                    return self.versions[0] if self.versions else None

            return _VQuery(list(self._versions))
        raise ValueError(f"Unexpected model: {name}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_enrichment_uses_title_matched_html_report():
    """If there's a title-matched html_report in the conversation, the
    enrichment should use its rcp."""
    rich = _FakeArtifact(
        id="rich-1",
        artifact_type="html_report",
        title="Address count by region",
        metadata_json={
            "report_card_payload": _FakeRCP(
                title="Address count by region",
                summary="A total of 54 addresses are distributed across 3 regions.",
                kpis=[
                    {"label": "Total addresses", "value": "54"},
                    {"label": "Regions", "value": "3"},
                ],
                insights=[
                    {"icon": "trending-up", "text": "EMEA leads with 24 addresses."},
                ],
                key_findings=[
                    {"icon": "star", "text": "EMEA dominates with nearly half."},
                ],
                recommendations=[
                    {"icon": "target", "text": "Maintain EMEA strength."},
                ],
                next_step="Connect a sales data source.",
                sql="SELECT region, COUNT(*) FROM addresses GROUP BY region;",
            ),
        },
    )
    db = _FakeDB({"conv-1": [rich]})

    args = {
        "format": "docx",
        "data": [{"region": "EMEA", "count": 24}],
        "title": "Address count by region",
        "instructions": "Generate a DOCX report",
    }
    enriched = _enrich_args_from_sibling_html_report(
        args=args,
        db=db,
        conversation_id="conv-1",
        title="Address count by region",
    )

    assert enriched["summary"] == "A total of 54 addresses are distributed across 3 regions."
    assert len(enriched["kpis"]) == 2
    assert enriched["insights"][0]["text"] == "EMEA leads with 24 addresses."
    assert enriched["key_findings"][0]["text"] == "EMEA dominates with nearly half."
    assert enriched["recommendations"][0]["text"] == "Maintain EMEA strength."
    assert enriched["next_step"] == "Connect a sales data source."
    assert enriched["sql"] == "SELECT region, COUNT(*) FROM addresses GROUP BY region;"


def test_enrichment_skips_sparse_sibling_even_when_title_matches():
    """The user's exact case: the latest artifact is a SPARSE sidecar
    (the sandbox just created it for the same docx task) with the
    same title, but its rcp is empty.  The enrichment should skip
    the sparse sidecar and use the richer html_report."""
    sparse_sidecar = _FakeArtifact(
        id="sparse-1",
        artifact_type="html",
        title="Regional_Address_Report.docx (preview)",
        metadata_json={},
        source_json={
            "_sidecar_of": "docx-id",
            "rcp": {
                "title": "Regional_Address_Report.docx",
                "summary": "",
                "kpis": [],
                "insights": [],
                "methodology": "",
                "key_findings": [],
                "recommendations": [],
                "next_step": None,
                "sql": "",
            },
        },
    )
    rich_html_report = _FakeArtifact(
        id="rich-2",
        artifact_type="html_report",
        title="Address count by region",
        metadata_json={
            "report_card_payload": _FakeRCP(
                title="Address count by region",
                summary="A total of 54 addresses across 3 regions.",
                kpis=[{"label": "Total", "value": "54"}],
                insights=[{"icon": "info", "text": "EMEA leads."}],
            ),
        },
    )
    db = _FakeDB({"conv-1": [rich_html_report, sparse_sidecar]})

    args = {
        "format": "docx",
        "data": [{"region": "EMEA", "count": 24}],
        "title": "Regional_Address_Report.docx",
        "instructions": "Generate a DOCX report",
    }
    enriched = _enrich_args_from_sibling_html_report(
        args=args,
        db=db,
        conversation_id="conv-1",
        title="Regional_Address_Report.docx",
    )

    # The summary is from the rich html_report, not the sparse sidecar.
    assert enriched["summary"] == "A total of 54 addresses across 3 regions."
    assert len(enriched["kpis"]) == 1


def test_enrichment_falls_back_to_most_recent_rich_sibling_when_no_title_match():
    """If no title matches, use the most-recent rich sibling."""
    rich = _FakeArtifact(
        id="rich-3",
        artifact_type="html_report",
        title="Completely Unrelated Title",
        metadata_json={
            "report_card_payload": _FakeRCP(
                title="Completely Unrelated Title",
                summary="Rich content from unrelated report.",
                kpis=[{"label": "Foo", "value": "1"}],
            ),
        },
    )
    db = _FakeDB({"conv-1": [rich]})

    args = {
        "format": "docx",
        "data": [{"a": 1}],
        "title": "My Brand New Report",
        "instructions": "...",
    }
    enriched = _enrich_args_from_sibling_html_report(
        args=args,
        db=db,
        conversation_id="conv-1",
        title="My Brand New Report",
    )

    assert enriched["summary"] == "Rich content from unrelated report."


def test_enrichment_does_not_clobber_agent_provided_fields():
    """If the agent already passed a summary, do not overwrite it."""
    rich = _FakeArtifact(
        id="rich-4",
        artifact_type="html_report",
        title="Same Title",
        metadata_json={
            "report_card_payload": _FakeRCP(
                title="Same Title",
                summary="From sibling",
                kpis=[{"label": "A", "value": "1"}],
            ),
        },
    )
    db = _FakeDB({"conv-1": [rich]})

    args = {
        "format": "docx",
        "data": [{"a": 1}],
        "title": "Same Title",
        "instructions": "...",
        "summary": "Agent-provided summary",
    }
    enriched = _enrich_args_from_sibling_html_report(
        args=args,
        db=db,
        conversation_id="conv-1",
        title="Same Title",
    )
    # Summary from agent preserved.
    assert enriched["summary"] == "Agent-provided summary"
    # KPIs from sibling still filled in.
    assert len(enriched["kpis"]) == 1


def test_enrichment_returns_args_unchanged_when_no_rich_sibling_exists():
    """If no rich sibling exists, return args unchanged (no
    enrichment possible)."""
    sparse_only = _FakeArtifact(
        id="sparse-2",
        artifact_type="html",
        title="Sparse Report",
        metadata_json={},
        source_json={
            "rcp": {
                "title": "Sparse Report",
                "summary": "",
                "kpis": [],
                "insights": [],
            },
        },
    )
    db = _FakeDB({"conv-1": [sparse_only]})

    args = {
        "format": "docx",
        "data": [{"a": 1}],
        "title": "Sparse Report",
        "instructions": "...",
    }
    enriched = _enrich_args_from_sibling_html_report(
        args=args,
        db=db,
        conversation_id="conv-1",
        title="Sparse Report",
    )

    # No summary filled in.
    assert enriched.get("summary") in (None, "")
    # Args are still usable.
    assert enriched["data"] == [{"a": 1}]


def test_enrichment_picks_rcp_from_version_source_json_when_metadata_empty():
    """Some siblings might have the rcp only in version.source_json
    (older artifacts created before the metadata_json field was
    standardized)."""
    rich = _FakeArtifact(
        id="rich-5",
        artifact_type="html",
        title="Old Style Report",
        metadata_json={},
        source_json={
            "rcp": _FakeRCP(
                title="Old Style Report",
                summary="From version.source_json.rcp",
                kpis=[{"label": "K", "value": "1"}],
            ),
        },
    )
    db = _FakeDB({"conv-1": [rich]})
    db.add_version(_FakeVersion(source_json=rich._source_json))

    args = {
        "format": "docx",
        "data": [{"a": 1}],
        "title": "Old Style Report",
        "instructions": "...",
    }
    enriched = _enrich_args_from_sibling_html_report(
        args=args,
        db=db,
        conversation_id="conv-1",
        title="Old Style Report",
    )

    assert enriched["summary"] == "From version.source_json.rcp"


def test_enrichment_uses_lenient_title_match():
    """Docx title with underscores should match a human-titled
    sibling via the 2+ token prefix match (e.g.
    'Regional_Address_Report.docx' vs 'Regional Address Report')."""
    rich = _FakeArtifact(
        id="rich-6",
        artifact_type="html_report",
        title="Regional Address Report",
        metadata_json={
            "report_card_payload": _FakeRCP(
                title="Regional Address Report",
                summary="54 addresses across regions.",
                kpis=[{"label": "Total", "value": "54"}],
            ),
        },
    )
    db = _FakeDB({"conv-1": [rich]})

    args = {
        "format": "docx",
        "data": [{"a": 1}],
        "title": "Regional_Address_Report.docx",
        "instructions": "...",
    }
    enriched = _enrich_args_from_sibling_html_report(
        args=args,
        db=db,
        conversation_id="conv-1",
        title="Regional_Address_Report.docx",
    )
    # The docx title normalizes to "regional address report"; the
    # sibling normalizes to "regional address report" — exact match.
    assert enriched["summary"] == "54 addresses across regions."


def test_create_artifact_payload_enrichment_uses_same_logic():
    """The create_artifact-side helper must behave identically to
    the sandbox-side helper."""
    rich = _FakeArtifact(
        id="rich-7",
        artifact_type="html_report",
        title="Q3 Sales Report",
        metadata_json={
            "report_card_payload": _FakeRCP(
                title="Q3 Sales Report",
                summary="Revenue grew 23% YoY.",
                kpis=[{"label": "Revenue", "value": "$1.2M"}],
                insights=[{"icon": "trending-up", "text": "APAC grew 34% QoQ."}],
            ),
        },
    )
    db = _FakeDB({"conv-1": [rich]})

    payload = {
        "html_path": "outputs/report.html",
        "filename": "Q3_Sales_Report.docx",
    }
    enriched = _enrich_payload_from_sibling_html_report(
        payload=payload,
        db=db,
        conversation_id="conv-1",
        title="Q3_Sales_Report.docx",
    )

    assert enriched["summary"] == "Revenue grew 23% YoY."
    assert len(enriched["kpis"]) == 1
    assert enriched["insights"][0]["text"] == "APAC grew 34% QoQ."
