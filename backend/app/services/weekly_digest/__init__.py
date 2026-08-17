"""Weekly Summary & Forecast Digest service package."""

from app.services.weekly_digest.generator import (
    generate_weekly_digest,
    generate_weekly_digest_parallel,
)
from app.services.weekly_digest.service import build_pptx_for_report

__all__ = [
    "build_pptx_for_report",
    "generate_weekly_digest",
    "generate_weekly_digest_parallel",
]
