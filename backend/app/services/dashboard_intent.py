"""Per-turn dashboard-intent flag carried via a ``ContextVar``.

The v3 agent stream sets this flag at entry (when the current turn is
classified as a dashboard-intent turn) and resets it on exit.  The artifact
persistence layer reads it to suppress stray analytics-path artifacts that
would otherwise be written alongside the real dashboard artifact on the same
thread (T18 regression).

Using a ``ContextVar`` (rather than threading the flag through every
``create_artifact`` call-site) means the guard works for *any* code path that
persists an artifact during a dashboard turn, including writers that we have
not yet explicitly annotated.
"""

from contextvars import ContextVar, Token

# Default False: a fresh context (or any code running outside a v3 dashboard
# turn) is never subject to the artifact drop.
dashboard_intent: ContextVar[bool] = ContextVar("dashboard_intent", default=False)


def set_dashboard_intent(value: bool) -> Token:
    """Set the per-turn dashboard-intent flag; returns a token for reset()."""
    return dashboard_intent.set(value)


def reset_dashboard_intent(token: Token) -> None:
    """Reset the flag to its previous value using the token from set_*()."""
    dashboard_intent.reset(token)


def is_dashboard_intent() -> bool:
    """Read the current flag value (defaults to False)."""
    return dashboard_intent.get()
