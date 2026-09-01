"""DataExecutionService — high-level lookups for DataExecution rows."""
from __future__ import annotations
from typing import Optional
from sqlalchemy.orm import Session

from app.models.data_execution import DataExecution


class DataExecutionService:
    @staticmethod
    def get_by_id(db: Session, execution_id: str) -> Optional[DataExecution]:
        try:
            return (
                db.query(DataExecution)
                .filter(
                    DataExecution.id == execution_id,
                    DataExecution.is_deleted == False,  # noqa: E712
                )
                .first()
            )
        except Exception:
            return None
