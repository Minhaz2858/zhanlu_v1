"""Regression test for the T11 LISTEN/NOTIFY callback signature.

asyncpg invokes notification listeners with FOUR positional arguments:
(connection, pid, channel, payload). The generated realtime template declared
a 3-arg ``_on_notify(conn, pid, payload)`` → TypeError on the first NOTIFY,
so the push layer silently degraded to interval polling.

Assert the TEMPLATE (source of truth for every generated app) carries the
correct 4-arg signature.
"""

from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[3] / "app" / "dashboards" / "_template" / "realtime.py.jinja2"


def test_on_notify_accepts_channel_arg():
    text = TEMPLATE.read_text()
    assert "def _on_notify(conn, pid, channel, payload):" in text, (
        "asyncpg listener callbacks are called with (conn, pid, channel, payload); "
        "a 3-arg callback raises TypeError on every NOTIFY and the T11 push never fires."
    )
