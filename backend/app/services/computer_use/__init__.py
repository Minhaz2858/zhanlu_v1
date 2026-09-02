"""computer_use — managed-browser primitive facade.

Exposes the shared per-conversation session store and the action
dispatcher used by the ``computer_use`` tool; ``agent_browser`` reuses the
same session store so one chat never spawns more than one browser.
"""

from app.services.computer_use.manager import (
    ALLOWED_ACTIONS,
    dispatch,
    is_available,
    maybe_json,
    run_cli,
)
from app.services.computer_use.session import (
    BROWSER_DIR,
    cleanup_session,
    get_session,
    is_exhausted,
    register_action,
)

__all__ = [
    "ALLOWED_ACTIONS",
    "BROWSER_DIR",
    "cleanup_session",
    "dispatch",
    "get_session",
    "is_available",
    "is_exhausted",
    "maybe_json",
    "register_action",
    "run_cli",
]
