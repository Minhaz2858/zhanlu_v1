"""Tests for the LLM narrative enrichment layer (hybrid planner).

These run WITHOUT a live LLM. The LLM client is monkeypatched in-process:
  * a fake that returns a structured narrative dict (asserts the plan is
    enriched: exec summary overwritten, findings + recommendations inserted),
  * and a fake that raises (asserts the deterministic plan is returned
    unchanged — the export must never break on LLM failure).
"""

import asyncio
import json

import pytest

from app.services.artifacts.architect import synthesize_plan
from app.services.artifacts.document_plan import DocumentPlan
from app.services.artifacts import llm_narrative


FAKE_NARRATIVE = {
    "executive_summary": (
        "Revenue declined 6% across H1, driven by softer DCPD demand; "
        "Isoprene held steady and should anchor the recovery plan."
    ),
    "findings": [
        "Isoprene is the most stable line, easing only 1% while peers fell further.",
        "DCPD concentration risk: a single customer drove 40% of the drop.",
        "March was the inflection point — all lines turned down together.",
    ],
    "recommendations": [
        "Reallocate sales effort to Isoprene where demand is resilient.",
        "Diversify the DCPD customer base to remove single-buyer risk.",
    ],
}


def _make_plan():
    rows = [
        {"month": "2026-01", "product": "Isoprene", "revenue_m": 110},
        {"month": "2026-02", "product": "Isoprene", "revenue_m": 118},
        {"month": "2026-03", "product": "Isoprene", "revenue_m": 121},
        {"month": "2026-01", "product": "DCPD", "revenue_m": 80},
        {"month": "2026-02", "product": "DCPD", "revenue_m": 84},
        {"month": "2026-03", "product": "DCPD", "revenue_m": 90},
    ]
    return synthesize_plan(
        "Monthly Revenue by Product",
        rows=rows,
        columns=["month", "product", "revenue_m"],
        theme="zhanlu-blue",
    )


class _FakeLLM:
    def __init__(self, payload, as_raw_string=False, raise_exc=False):
        self._payload = payload
        self._as_raw_string = as_raw_string
        self._raise_exc = raise_exc
        self.calls = 0

    async def __call__(self, *args, **kwargs):
        self.calls += 1
        if self._raise_exc:
            raise RuntimeError("simulated LLM outage")
        if self._as_raw_string:
            return {"response": json.dumps(self._payload), "model": "fake", "usage": {}}
        # Schema-parsed shape: {data: {response: <parsed dict>}}
        return {"data": {"response": self._payload}, "model": "fake", "usage": {}}


@pytest.fixture
def patch_llm(monkeypatch):
    def _install(fake):
        import app.services.llm_service as svc

        monkeypatch.setattr(svc, "call_llm", fake)
        return fake

    return _install


def test_enrich_overwrites_exec_summary_and_inserts_blocks():
    plan = _make_plan()

    import app.services.llm_service as svc
    import unittest.mock as mock

    with mock.patch.object(svc, "call_llm", _FakeLLM(FAKE_NARRATIVE)):
        out = asyncio.run(
            llm_narrative.enrich_plan_narrative(
                plan,
                rows=[{"month": "2026-01", "product": "Isoprene", "revenue_m": 110}],
                columns=["month", "product", "revenue_m"],
                request_text="exec summary of revenue",
                user_context={"role": "executive"},
            )
        )

    # 1) exec summary paragraph was rewritten
    exec_blocks = [
        b for b in out.blocks
        if b.type == "paragraph" and (b.title or "").lower().startswith("executive")
    ]
    assert exec_blocks, "expected an Executive Summary paragraph"
    assert FAKE_NARRATIVE["executive_summary"] in exec_blocks[0].text

    # 2) findings + recommendations blocks now exist (architect did not emit them)
    assert "findings" in [b.type for b in out.blocks]
    assert "recommendations" in [b.type for b in out.blocks]
    findings_block = next(b for b in out.blocks if b.type == "findings")
    recs_block = next(b for b in out.blocks if b.type == "recommendations")
    assert findings_block.items[0]["text"].startswith("Isoprene is the most stable")
    assert recs_block.items[0]["text"].startswith("Reallocate sales effort")

    # 3) the plan object identity is preserved (mutated in place)
    assert out is plan


def test_enrich_falls_back_silently_on_llm_failure():
    plan = _make_plan()
    original_summary = next(
        b.text for b in plan.blocks
        if b.type == "paragraph" and (b.title or "").lower().startswith("executive")
    )

    import app.services.llm_service as svc
    import unittest.mock as mock

    with mock.patch.object(svc, "call_llm", _FakeLLM(None, raise_exc=True)):
        out = asyncio.run(
            llm_narrative.enrich_plan_narrative(
                plan, rows=[], columns=[], request_text="x", user_context={"role": "analyst"}
            )
        )

    # Plan returned unchanged — deterministic narrative still present.
    assert out is plan
    exec_blocks = [
        b for b in out.blocks
        if b.type == "paragraph" and (b.title or "").lower().startswith("executive")
    ]
    assert exec_blocks[0].text == original_summary
    assert "findings" not in [b.type for b in out.blocks]


def test_parse_narrative_handles_dict_and_raw():
    # schema-parsed (data.response is a dict)
    assert llm_narrative._parse_narrative(
        {"data": {"response": FAKE_NARRATIVE}}
    )["executive_summary"].startswith("Revenue declined")
    # raw string JSON
    raw = "```json\n" + json.dumps(FAKE_NARRATIVE) + "\n```"
    parsed = llm_narrative._parse_narrative({"response": raw})
    assert parsed["recommendations"][0].startswith("Reallocate")
    # garbage -> None (caller keeps deterministic plan)
    assert llm_narrative._parse_narrative({"response": "not json"}) is None
    assert llm_narrative._parse_narrative(None) is None


def test_sync_wrapper_enriches():
    plan = _make_plan()
    import app.services.llm_service as svc
    import unittest.mock as mock

    with mock.patch.object(svc, "call_llm", _FakeLLM(FAKE_NARRATIVE)):
        out = llm_narrative.enrich_plan_narrative_sync(
            plan, rows=[], columns=[], request_text="exec summary", user_context={"role": "executive"}
        )
    assert "findings" in [b.type for b in out.blocks]
