"""AI literacy tests (IBM Gap 4, 2026-10-13).

starter_prompts mines the tenant's own successful asks ("questions that
worked for your team") — teaching by example, grounded in their data.
coach_question rewrites rough questions, failing open.
"""
import pytest


def _fresh_db(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import app.models  # noqa: F401
    from app.database import Base

    engine = create_engine(f"sqlite:///{tmp_path}/literacy_test.db")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed_log(db, org_id, question, outcome="success", feedback=None, days_ago=0):
    from datetime import datetime, timedelta, timezone

    from app.models.nl2sql_query_log import Nl2sqlQueryLog

    db.add(Nl2sqlQueryLog(
        org_id=org_id,
        question=question,
        outcome=outcome,
        user_feedback=feedback,
        created_date=datetime.now(timezone.utc) - timedelta(days=days_ago),
    ))
    db.commit()


# ── starter prompts ───────────────────────────────────────────────────────


def test_starters_prefer_thumbs_up_and_dedupe(tmp_path):
    from app.services.ai_literacy import starter_prompts

    db = _fresh_db(tmp_path)
    _seed_log(db, "org-a", "show top products by revenue", feedback="thumbs_up")
    _seed_log(db, "org-a", "show top products by revenue")  # duplicate — ignored
    _seed_log(db, "org-a", "monthly trend of orders", feedback=None)
    _seed_log(db, "org-a", "failed query example", outcome="error")  # excluded

    s = starter_prompts(db, org_id="org-a", days=90)
    questions = [x["question"] for x in s["starters"]]
    assert questions[0] == "show top products by revenue"  # thumbs-up ranked first
    assert "failed query example" not in questions
    assert s["worked_count"] == 2
    # your-team starters come before generic suggestions
    sources = [x["source"] for x in s["starters"]]
    assert sources[0] == "your_team"


def test_starters_org_scoped(tmp_path):
    from app.services.ai_literacy import starter_prompts

    db = _fresh_db(tmp_path)
    _seed_log(db, "org-b", "other tenant secret query")
    s = starter_prompts(db, org_id="org-a", days=90)
    assert all("secret" not in x["question"] for x in s["starters"])
    assert s["worked_count"] == 0


def test_starters_fallback_when_no_history(tmp_path):
    from app.services.ai_literacy import starter_prompts

    db = _fresh_db(tmp_path)
    s = starter_prompts(db, org_id="org-empty", days=90)
    assert s["worked_count"] == 0
    assert len(s["starters"]) >= 3
    assert all(x["source"] == "suggested" for x in s["starters"])


# ── prompt coach ──────────────────────────────────────────────────────────


async def test_coach_parses_llm_variants(monkeypatch):
    from app.services import ai_literacy as al

    async def fake_call(**kwargs):
        return {"data": {"improved": [
            "Revenue by product for last month, as a ranked table",
            "Top 10 products by revenue, 2026-09, with month-over-month",
        ], "tip": "Name the measure, window, and output shape."}}

    import app.services.llm_service as ls

    monkeypatch.setattr(ls, "call_llm", fake_call)
    out = await al.coach_question("show me product stuff")
    assert len(out["improved"]) == 2
    assert "measure" in out["tip"].lower()
    assert out["original"] == "show me product stuff"


async def test_coach_fails_open(monkeypatch):
    from app.services import ai_literacy as al

    async def fake_call(**kwargs):
        raise RuntimeError("provider down")

    import app.services.llm_service as ls

    monkeypatch.setattr(ls, "call_llm", fake_call)
    out = await al.coach_question("show me product stuff")
    assert out["improved"] == []          # nothing invented
    assert out["tip"]                     # generic guidance still given
    assert out["original"] == "show me product stuff"


async def test_coach_empty_question(tmp_path):
    from app.services.ai_literacy import coach_question

    out = await coach_question("   ")
    assert out["improved"] == []
