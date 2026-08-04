"""Ecisco BI Decision module — post-pricing critic + audit trail.

Port of EDIA 5.1.2:
- decision_service.py (LangGraph pipeline → simplified class-based)
- decision_critic.py  (contradiction / confidence realism checks)
- decision_audit.py   (decision history + evolution tracking)
"""

from .service import DecisionService
from .critic import DecisionCritic
from .audit import DecisionAuditService
from .tool import register_decision_tools

__all__ = [
    "DecisionService",
    "DecisionCritic",
    "DecisionAuditService",
    "register_decision_tools",
]
