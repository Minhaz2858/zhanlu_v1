"""Depth Analysis package — bounded hypothesis→query→validate→refine loop."""

from app.services.depth_analysis.loop import (
    DepthAnalysisResult,
    run_depth_loop,
)

__all__ = ["DepthAnalysisResult", "run_depth_loop"]
