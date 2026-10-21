"""DFM package — deterministic manufacturability rules (roadmap P1)."""
from app.services.dfm.rules import (  # noqa: F401
    MATERIALS,
    PROCESSES,
    get_material,
    run_dfm_check,
)
