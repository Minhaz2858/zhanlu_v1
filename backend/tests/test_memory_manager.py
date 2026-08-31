"""Tests for memory manager -- semantic dedup and consolidation."""
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.memory_manager import (
    ConsolidationReport,
    find_semantic_duplicates,
    merge_semantic_duplicates,
    remove_expired,
    archive_stale,
    promote_frequently_used,
    run_consolidation,
    SEMANTIC_DUP_THRESHOLD,
)


def _make_memory(id, content, embedding=None, importance=0, usage=0, created=None, ttl=None):
    """Create a mock AgentMemory."""
    m = MagicMock()
    m.id = id
    m.content = content
    m.embedding = embedding
    m.importance = importance
    m.usage_count = usage
    m.is_deleted = False
    m.created_date = created or datetime.utcnow()
    m.ttl_days = ttl
    return m


def test_find_semantic_duplicates_with_embeddings():
    """Finds pairs with cosine similarity >= threshold."""
    emb_a = [1.0, 0.0, 0.0]
    emb_b = [0.99, 0.01, 0.0]  # very similar to a
    emb_c = [0.0, 1.0, 0.0]  # orthogonal to a and b

    memories = [
        _make_memory("1", "content a", embedding=emb_a),
        _make_memory("2", "content b", embedding=emb_b),
        _make_memory("3", "content c", embedding=emb_c),
    ]

    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = memories

    pairs = find_semantic_duplicates(db, "agent-1", threshold=0.85)
    # a and b should be duplicates, a and c should not, b and c should not
    pair_ids = {(p[0].id, p[1].id) for p in pairs}
    assert ("1", "2") in pair_ids or ("2", "1") in pair_ids


def test_find_semantic_duplicates_no_embeddings():
    """Returns empty list when no memories have embeddings."""
    memories = [_make_memory("1", "content"), _make_memory("2", "content")]
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = memories

    pairs = find_semantic_duplicates(db, "agent-1")
    assert pairs == []


def test_find_semantic_duplicates_single_memory():
    """Returns empty list with only one memory."""
    memories = [_make_memory("1", "content", embedding=[1.0, 0.0])]
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = memories

    pairs = find_semantic_duplicates(db, "agent-1")
    assert pairs == []


def test_remove_expired():
    """Soft-deletes expired memories."""
    expired = _make_memory("1", "old", ttl=1, created=datetime.utcnow() - timedelta(days=10))
    fresh = _make_memory("2", "new", ttl=30, created=datetime.utcnow())

    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [expired, fresh]

    count = remove_expired(db, "agent-1")
    assert count == 1
    assert expired.is_deleted is True
    assert fresh.is_deleted is False


def test_archive_stale():
    """Archives low-importance, unused, old memories."""
    stale = _make_memory("1", "stale", importance=0, usage=0, created=datetime.utcnow() - timedelta(days=60))
    important = _make_memory("2", "important", importance=3, usage=0, created=datetime.utcnow() - timedelta(days=60))
    used = _make_memory("3", "used", importance=0, usage=5, created=datetime.utcnow() - timedelta(days=60))
    fresh = _make_memory("4", "fresh", importance=0, usage=0, created=datetime.utcnow())

    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [stale, important, used, fresh]

    count = archive_stale(db, "agent-1", stale_days=30)
    assert count == 1
    assert stale.is_deleted is True
    assert important.is_deleted is False
    assert used.is_deleted is False
    assert fresh.is_deleted is False


def test_archive_stale_aware_created_no_crash():
    """Regression (2026-08-28): an AWARE created_date must not raise
    'can't compare offset-naive and offset-aware datetimes'. Observed live
    as the recurring 'Memory consolidation failed for <agent>' log line."""
    stale = _make_memory(
        "1", "stale", importance=0, usage=0,
        created=datetime.now(timezone.utc) - timedelta(days=60),
    )
    fresh = _make_memory(
        "2", "fresh", importance=0, usage=0,
        created=datetime.now(timezone.utc),
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [stale, fresh]

    count = archive_stale(db, "agent-1", stale_days=30)
    assert count == 1
    assert stale.is_deleted is True
    assert fresh.is_deleted is False


def test_is_expired_aware_created_no_crash():
    """Regression (2026-08-28): is_expired must handle AWARE created dates
    (normalized to naive UTC) without raising naive/aware TypeError."""
    from app.services.memory_advanced import is_expired

    expired = _make_memory(
        "1", "old", ttl=1,
        created=datetime.now(timezone.utc) - timedelta(days=10),
    )
    fresh = _make_memory(
        "2", "new", ttl=30,
        created=datetime.now(timezone.utc),
    )
    assert is_expired(expired) is True
    assert is_expired(fresh) is False


def test_promote_frequently_used():
    """Promotes high-usage memories to importance 3."""
    high_usage = _make_memory("1", "popular", importance=1, usage=5)
    low_usage = _make_memory("2", "rare", importance=1, usage=1)
    already_high = _make_memory("3", "already", importance=4, usage=10)

    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [high_usage, low_usage, already_high]

    count = promote_frequently_used(db, "agent-1", promotion_threshold=3)
    assert count == 1
    assert high_usage.importance == 3
    assert low_usage.importance == 1  # not promoted
    assert already_high.importance == 4  # not changed


def test_consolidation_report_to_dict():
    """ConsolidationReport serializes correctly."""
    report = ConsolidationReport(
        semantic_duplicates_found=3, semantic_duplicates_merged=2,
        expired_removed=1, stale_archived=5, promoted=2,
        total_before=20, total_after=12,
    )
    d = report.to_dict()
    assert d["semantic_duplicates_merged"] == 2
    assert d["total_before"] == 20
    assert d["total_after"] == 12
