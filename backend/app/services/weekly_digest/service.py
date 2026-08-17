"""Weekly digest service facade.

Keeps the HTTP router free of python-pptx imports: it delegates deck assembly
to :class:`WeeklyDigestPptxBuilder` behind a single, testable entry point.
"""

from __future__ import annotations

import io

from sqlalchemy.orm import Session

from app.services.weekly_digest.pptx_exporter import WeeklyDigestPptxBuilder


def build_pptx_for_report(db: Session, report_id: str) -> io.BytesIO:
    """Render a weekly digest report into an in-memory ``.pptx`` deck."""
    return WeeklyDigestPptxBuilder(db).build(report_id)
