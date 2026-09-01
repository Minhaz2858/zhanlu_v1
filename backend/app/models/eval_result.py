"""LLM evaluation result model — stores quality eval pipeline results."""

from datetime import datetime, timezone
from sqlalchemy import Column, String, Float, Text, DateTime
from app.models.base import TimestampedBase


class EvalResult(TimestampedBase):
    __tablename__ = "eval_results"

    conversation_id = Column(String(64), nullable=False, index=True)
    user_message = Column(Text, nullable=True)
    assistant_text = Column(Text, nullable=True)
    scores = Column(Text, nullable=True)          # JSON string
    verdict = Column(String(32), nullable=False, default="pending")
    model = Column(String(64), nullable=True)

    # Timestamps handled by Base (created_date, updated_date)
