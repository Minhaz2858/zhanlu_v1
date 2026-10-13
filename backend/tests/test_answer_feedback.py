"""Answer feedback summary tests (IBM Gap 3, 2026-10-13).

The chat UI's 👍/👎 loop already captures feedback durably as
ExperienceEntry (entry_type="user_feedback", outcome positive/negative)
and feeds recipe/cache/HITL learning. The Gap was the BUSINESS view:
these tests cover the aggregation that puts acceptance rates on the ROI
and portfolio boards.
"""


def _fresh_db(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import app.models  # noqa: F401
    from app.database import Base

    engine = create_engine(f"sqlite:///{tmp_path}/feedback_test.db")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed_feedback(db, org_id, agent_app_id, outcome, days_ago=0):
    from datetime import datetime, timedelta, timezone

    from app.models.experience_entry import ExperienceEntry

    db.add(ExperienceEntry(
        org_id=org_id,
        agent_app_id=agent_app_id,
        entry_type="user_feedback",
        outcome=outcome,  # positive | negative
        user_rating=1 if outcome == "positive" else -1,
        created_date=datetime.now(timezone.utc) - timedelta(days=days_ago),
    ))
    db.commit()


def test_summary_acceptance_rate_and_org_scope(tmp_path):
    from app.services.answer_feedback import feedback_summary

    db = _fresh_db(tmp_path)
    # org-a: analyst 3 up, 1 down → 0.75
    for outcome in ["positive", "positive", "positive", "negative"]:
        _seed_feedback(db, "org-a", "analyst", outcome)
    # org-b must not leak
    _seed_feedback(db, "org-b", "analyst", "negative")

    s = feedback_summary(db, org_id="org-a", days=30)
    assert s["totals"]["up"] == 3
    assert s["totals"]["down"] == 1
    assert s["totals"]["acceptance_rate"] == 0.75
    assert s["by_agent"]["analyst"]["acceptance_rate"] == 0.75


def test_summary_window_excludes_old(tmp_path):
    from app.services.answer_feedback import feedback_summary

    db = _fresh_db(tmp_path)
    _seed_feedback(db, "org-a", "analyst", "negative", days_ago=90)
    s = feedback_summary(db, org_id="org-a", days=30)
    assert s["totals"]["acceptance_rate"] is None


def test_unknown_outcome_ignored(tmp_path):
    from app.services.answer_feedback import feedback_summary

    db = _fresh_db(tmp_path)
    _seed_feedback(db, "org-a", "analyst", "neutral-ish")
    s = feedback_summary(db, org_id="org-a", days=30)
    assert s["totals"]["acceptance_rate"] is None


def test_roi_summary_carries_acceptance_rate(tmp_path):
    """The quality dimension lands on the ROI board per agent — even for
    agents with no conversations/costs in the window."""
    from app.services.answer_feedback import feedback_summary  # noqa: F401
    from app.services.roi_service import roi_summary

    db = _fresh_db(tmp_path)
    _seed_feedback(db, "org-a", "analyst", "positive")
    _seed_feedback(db, "org-a", "analyst", "negative")

    roi = roi_summary(db, org_id="org-a", days=30)
    assert roi["totals"]["acceptance_rate"] == 0.5
    agents = {a["agent_name"]: a for a in roi["by_agent"]}
    assert agents["analyst"]["acceptance_rate"] == 0.5


def test_empty_feedback_no_rate(tmp_path):
    from app.services.answer_feedback import feedback_summary

    db = _fresh_db(tmp_path)
    s = feedback_summary(db, org_id="org-empty", days=30)
    assert s["totals"]["acceptance_rate"] is None
    assert s["by_agent"] == {}
