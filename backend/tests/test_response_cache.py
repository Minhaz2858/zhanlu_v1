"""Tests for the semantic response cache (experience layer, Phase B).

Uses an in-memory sqlite session so the cache service can be tested
without a live PostgreSQL database. Embedding vectors are supplied
directly (bypassing the embedding service) to keep the tests
deterministic.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.base import Base
from app.models.response_cache_entry import (
    CACHE_SCOPE_SHARED,
    CACHE_SCOPE_USER,
    ResponseCacheEntry,
)
from app.services import response_cache as rc


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()
    engine.dispose()


AGENT = "agent-1"
USER_A = "user-a"
USER_B = "user-b"
DV = "2026-08-08"


def _v(*coords):
    """Build a unit-ish embedding vector from coords (values ~ 1.0 / 0.0)."""
    return list(coords)


# --- cosine similarity -----------------------------------------------------
class TestCosineSimilarity:
    def test_identical(self):
        assert rc._cosine_similarity([1.0, 0.0, 0.0], [1.0, 0.0, 0.0]) == pytest.approx(1.0)

    def test_orthogonal(self):
        assert rc._cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_len_mismatch(self):
        assert rc._cosine_similarity([1.0], [1.0, 0.0]) == 0.0

    def test_empty(self):
        assert rc._cosine_similarity([], []) == 0.0
        assert rc._cosine_similarity(None, [1.0]) == 0.0

    def test_partial(self):
        a = [1.0, 0.0, 0.0]
        b = [0.99, 0.1, 0.0]
        assert rc._cosine_similarity(a, b) > 0.9


# --- market data version ----------------------------------------------------
class TestDataVersion:
    def test_fallback_shape(self):
        dv = rc.get_market_data_version()
        assert isinstance(dv, str)
        assert len(dv) == 10
        assert dv.count("-") == 2


# --- store / lookup ---------------------------------------------------------
class TestStoreAndLookup:
    def test_store_and_hit_same_embedding(self, db):
        rc.store_cached_response(
            db,
            agent_app_id=AGENT,
            user_id=None,
            question_text="苯乙烯今日价格",
            intent_class="price_report",
            embedding=_v(1, 0, 0),
            response_content="今日苯乙烯价格 9800 元/吨。",
            data_version=DV,
        )
        hit = rc.lookup_cached_response(
            db,
            agent_app_id=AGENT,
            user_id=None,
            question_text="苯乙烯今天价格",
            intent_class="price_report",
            embedding=_v(1, 0, 0),
            data_version=DV,
        )
        assert hit is not None
        assert hit.response_content == "今日苯乙烯价格 9800 元/吨。"

    def test_hit_increments_counter(self, db):
        rc.store_cached_response(
            db,
            agent_app_id=AGENT,
            user_id=None,
            question_text="q",
            intent_class="price_report",
            embedding=_v(1, 0, 0),
            response_content="r",
            data_version=DV,
        )
        rc.lookup_cached_response(
            db,
            agent_app_id=AGENT,
            user_id=None,
            question_text="q",
            intent_class="price_report",
            embedding=_v(1, 0, 0),
            data_version=DV,
        )
        entry = db.query(ResponseCacheEntry).one()
        assert entry.hit_count == 1

    def test_dissimilar_question_misses(self, db):
        rc.store_cached_response(
            db,
            agent_app_id=AGENT,
            user_id=None,
            question_text="q",
            intent_class="price_report",
            embedding=_v(1, 0, 0),
            response_content="r",
            data_version=DV,
        )
        hit = rc.lookup_cached_response(
            db,
            agent_app_id=AGENT,
            user_id=None,
            question_text="completely different topic",
            intent_class="price_report",
            embedding=_v(0, 1, 0),  # orthogonal -> sim 0
            data_version=DV,
        )
        assert hit is None

    def test_embedding_unavailable_falls_back_to_miss(self, db):
        rc.store_cached_response(
            db,
            agent_app_id=AGENT,
            user_id=None,
            question_text="q",
            intent_class="price_report",
            embedding=_v(1, 0, 0),
            response_content="r",
            data_version=DV,
        )
        assert (
            rc.lookup_cached_response(
                db,
                agent_app_id=AGENT,
                user_id=None,
                question_text="q",
                intent_class="price_report",
                embedding=None,  # embedding service down
                data_version=DV,
            )
            is None
        )

    def test_store_requires_content(self, db):
        assert (
            rc.store_cached_response(
                db,
                agent_app_id=AGENT,
                user_id=None,
                question_text="q",
                intent_class="price_report",
                embedding=_v(1, 0, 0),
                response_content="   ",
                data_version=DV,
            )
            is None
        )

    def test_store_requires_embedding(self, db):
        assert (
            rc.store_cached_response(
                db,
                agent_app_id=AGENT,
                user_id=None,
                question_text="q",
                intent_class="price_report",
                embedding=None,
                response_content="r",
                data_version=DV,
            )
            is None
        )


# --- freshness guards --------------------------------------------------------
class TestFreshness:
    def test_data_version_mismatch_misses(self, db):
        rc.store_cached_response(
            db,
            agent_app_id=AGENT,
            user_id=None,
            question_text="q",
            intent_class="price_report",
            embedding=_v(1, 0, 0),
            response_content="old prices",
            data_version="2026-08-01",
        )
        hit = rc.lookup_cached_response(
            db,
            agent_app_id=AGENT,
            user_id=None,
            question_text="q",
            intent_class="price_report",
            embedding=_v(1, 0, 0),
            data_version="2026-08-02",
        )
        assert hit is None

    def test_agent_isolation(self, db):
        rc.store_cached_response(
            db,
            agent_app_id=AGENT,
            user_id=None,
            question_text="q",
            intent_class="price_report",
            embedding=_v(1, 0, 0),
            response_content="r",
            data_version=DV,
        )
        assert (
            rc.lookup_cached_response(
                db,
                agent_app_id="other-agent",
                user_id=None,
                question_text="q",
                intent_class="price_report",
                embedding=_v(1, 0, 0),
                data_version=DV,
            )
            is None
        )

    def test_expired_entry_misses(self, db):
        entry = rc.store_cached_response(
            db,
            agent_app_id=AGENT,
            user_id=None,
            question_text="q",
            intent_class="price_report",
            embedding=_v(1, 0, 0),
            response_content="r",
            data_version=DV,
        )
        entry.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db.commit()
        assert (
            rc.lookup_cached_response(
                db,
                agent_app_id=AGENT,
                user_id=None,
                question_text="q",
                intent_class="price_report",
                embedding=_v(1, 0, 0),
                data_version=DV,
            )
            is None
        )
        # prune soft-deletes the expired row
        assert rc.prune_expired(db) >= 1

    def test_ttl_default_24h(self, db):
        entry = rc.store_cached_response(
            db,
            agent_app_id=AGENT,
            user_id=None,
            question_text="q",
            intent_class="price_report",
            embedding=_v(1, 0, 0),
            response_content="r",
            data_version=DV,
        )
        delta = entry.expires_at - datetime.now(timezone.utc)
        assert timedelta(hours=23) < delta <= timedelta(hours=24)


# --- scope isolation ---------------------------------------------------------
class TestScopeIsolation:
    def test_shared_entry_not_served_to_user_scope(self, db):
        rc.store_cached_response(
            db,
            agent_app_id=AGENT,
            user_id=None,
            question_text="帮我写个报告",
            intent_class="market_analysis",
            embedding=_v(1, 0, 0),
            response_content="shared report",
            data_version=DV,
        )
        assert (
            rc.lookup_cached_response(
                db,
                agent_app_id=AGENT,
                user_id=USER_A,
                question_text="帮我写个报告",
                intent_class="conversational",
                embedding=_v(1, 0, 0),
                data_version=DV,
            )
            is None
        )

    def test_user_scoped_entry_served_to_owner_only(self, db):
        rc.store_cached_response(
            db,
            agent_app_id=AGENT,
            user_id=USER_A,
            question_text="今天心情如何",
            intent_class="conversational",
            embedding=_v(1, 0, 0),
            response_content="user A answer",
            data_version=DV,
        )
        assert (
            rc.lookup_cached_response(
                db,
                agent_app_id=AGENT,
                user_id=USER_A,
                question_text="今天心情如何",
                intent_class="conversational",
                embedding=_v(1, 0, 0),
                data_version=DV,
            )
            is not None
        )
        assert (
            rc.lookup_cached_response(
                db,
                agent_app_id=AGENT,
                user_id=USER_B,
                question_text="今天心情如何",
                intent_class="conversational",
                embedding=_v(1, 0, 0),
                data_version=DV,
            )
            is None
        )

    def test_scope_assignment(self, db):
        rc.store_cached_response(
            db,
            agent_app_id=AGENT,
            user_id=None,
            question_text="q",
            intent_class="forecast_question",
            embedding=_v(1, 0, 0),
            response_content="r",
            data_version=DV,
        )
        rc.store_cached_response(
            db,
            agent_app_id=AGENT,
            user_id=USER_A,
            question_text="hi",
            intent_class="conversational",
            embedding=_v(1, 0, 0),
            response_content="hello",
            data_version=DV,
        )
        by_intent = {e.intent_class: e for e in db.query(ResponseCacheEntry).all()}
        assert by_intent["forecast_question"].scope == CACHE_SCOPE_SHARED
        assert by_intent["forecast_question"].user_id is None
        assert by_intent["conversational"].scope == CACHE_SCOPE_USER
        assert by_intent["conversational"].user_id == USER_A

    def test_conversational_without_user_not_stored(self, db):
        assert (
            rc.store_cached_response(
                db,
                agent_app_id=AGENT,
                user_id=None,
                question_text="hi",
                intent_class="conversational",
                embedding=_v(1, 0, 0),
                response_content="hello",
                data_version=DV,
            )
            is None
        )


# --- feedback ---------------------------------------------------------------
class TestFeedback:
    def test_thumbs_down_evicts_below_floor(self, db):
        entry = rc.store_cached_response(
            db,
            agent_app_id=AGENT,
            user_id=None,
            question_text="q",
            intent_class="price_report",
            embedding=_v(1, 0, 0),
            response_content="r",
            data_version=DV,
        )
        rc.apply_feedback_score(db, entry.id, -1)
        rc.apply_feedback_score(db, entry.id, -1)  # score == -2 -> evict
        assert (
            rc.lookup_cached_response(
                db,
                agent_app_id=AGENT,
                user_id=None,
                question_text="q",
                intent_class="price_report",
                embedding=_v(1, 0, 0),
                data_version=DV,
            )
            is None
        )

    def test_thumbs_up_reinforces(self, db):
        entry = rc.store_cached_response(
            db,
            agent_app_id=AGENT,
            user_id=None,
            question_text="q",
            intent_class="price_report",
            embedding=_v(1, 0, 0),
            response_content="r",
            data_version=DV,
        )
        rc.apply_feedback_score(db, entry.id, 1)
        fresh = db.query(ResponseCacheEntry).filter_by(id=entry.id).one()
        assert fresh.feedback_score == 1.0
        assert not fresh.is_deleted

    def test_evict_soft_deletes(self, db):
        entry = rc.store_cached_response(
            db,
            agent_app_id=AGENT,
            user_id=None,
            question_text="q",
            intent_class="price_report",
            embedding=_v(1, 0, 0),
            response_content="r",
            data_version=DV,
        )
        assert rc.evict_cache_entry(db, entry.id) is True
        assert db.query(ResponseCacheEntry).filter_by(id=entry.id).one().is_deleted
