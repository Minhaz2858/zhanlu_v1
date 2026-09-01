"""Dynamic Enterprise Business-Data Executive Orchestrator.

Transforms any enterprise question ("Why are gross margins dropping in the
Southern region?", "Give me supply chain data for last 30 days") into a
comprehensive, truth-backed, multi-section executive report delivered as
both a rich DOCX file and an inline HTML chat response.

Pipeline (see the design spec at docs/superpowers/specs/):
1. profiler  — LLM-driven facet planner (single structured-JSON call, fail-open)
2. executor  — asyncio.gather fan-out over 3-6 facets, partial-failure isolated
3. synthesizer — deterministic transforms + recommended-actions rules (NO LLM)
4. claim_tracker — SQL-grounded verification of every numeric claim
5. renderers — dual truth-backed rendering (DOCX + inline HTML chat)

Reuses existing services (Two-Phase NL2SQL schema linker); introduces
no new SQL, pip packages, flags, or migrations.
"""

# Re-export the public pipeline entry points so the tool wrapper and
# downstream consumers can do
# ``from app.services.enterprise_orchestrator import profile_enterprise_intent``
# without reaching into the submodules. Submodule imports stay in
# each function to keep the cold-start cost low.
from app.services.enterprise_orchestrator.profiler import profile_enterprise_intent
from app.services.enterprise_orchestrator.executor import execute_facets
from app.services.enterprise_orchestrator.synthesizer import (
    synthesize_enterprise_report,
)

__all__ = [
    "profile_enterprise_intent",
    "execute_facets",
    "synthesize_enterprise_report",
]
