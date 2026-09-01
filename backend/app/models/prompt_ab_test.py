"""Prompt A/B test result model."""

from datetime import datetime, timezone
from sqlalchemy import Column, String, Integer, Float, Text, DateTime
from app.models.base import TimestampedBase


class PromptABTest(TimestampedBase):
    __tablename__ = "prompt_ab_tests"

    test_id = Column(String(32), unique=True, nullable=False, index=True)
    prompt_version_a = Column(Text, nullable=True)
    prompt_version_b = Column(Text, nullable=True)
    total_queries = Column(Integer, default=0)
    wins_a = Column(Integer, default=0)
    wins_b = Column(Integer, default=0)
    ties = Column(Integer, default=0)
    winner = Column(String(8), default="tie")
    confidence = Column(Float, default=0.0)
    mean_score_a = Column(Float, default=0.0)
    mean_score_b = Column(Float, default=0.0)
    per_query_results = Column(Text, nullable=True)  # JSON string
