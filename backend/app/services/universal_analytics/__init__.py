"""Universal Analytics Engine

Gives ANY agent forecast/KPI/trend/query capabilities against ANY
connected database with zero configuration.

Tools (registered enabled_by_default=True):
    universal_describe  — describe schema for bound KBs
    universal_discover  — scan bound KBs for forecastable series
    universal_query     — execute read-only SQL
    universal_kpi       — KPI aggregation with YoY/MoM deltas
    universal_trend     — trend direction/slope/seasonality
    universal_forecast  — forecast via the 8-model ensemble

Flags (in .env):
    UNIVERSAL_ANALYTICS_ENABLED  (default ON)  — master gate
    UNIVERSAL_ANALYTICS_AUTO_DISCOVER (default ON) — zero-config auto-scan
    UNIVERSAL_ANALYTICS_NL_SQL   (default OFF) — NL→SQL translation
    UNIVERSAL_ANALYTICS_ANOMALY  (default OFF) — anomaly detection
"""

# Import tools to trigger registration (must happen at module-import time
# so that the registry picks up all 6 tools before agent chat starts).
from app.services.universal_analytics import tools  # noqa: F401, E402

# Register the auto-discovery SQLAlchemy event listener on KnowledgeBase.
# After this import, any new database-type KnowledgeBase INSERT will
# trigger a background discovery scan automatically.
from app.services.universal_analytics.auto_discover import (  # noqa: E402
    register_kb_event_listener,
)
register_kb_event_listener()
