"""Schedule parser — converts human-readable schedule strings to cron.

Supports two input formats:
  1. **Standard cron** — "0 9 * * 1" (Mon at 9 AM), "*/30 * * * *" (every 30m).
  2. **Natural language** — "every Monday at 9 AM", "daily at 8:00",
     "every 2 hours", "weekly", "monthly", "every weekday at 17:00".

The parser tries (in order):
  a) A regex rule-based table for common English patterns (no LLM cost).
  b) An LLM call to interpret the schedule (uses ``chat_completion_json_sync``).
  c) Raises ``ScheduleParseError`` if both fail.

After parsing, ``next_run_at(cron, after=...)`` computes the next firing time
in UTC using the ``croniter`` library (added to requirements.txt if missing).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


class ScheduleParseError(ValueError):
    """Raised when a schedule string cannot be parsed."""


# ---------------------------------------------------------------------------
# 1. Standard cron detection
# ---------------------------------------------------------------------------

_CRON_FIELD_RE = re.compile(
    r"^\s*(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*$"
)


def _looks_like_cron(s: str) -> bool:
    """A cron expression is 5 whitespace-separated fields, each with cron chars."""
    if not s:
        return False
    m = _CRON_FIELD_RE.match(s)
    if not m:
        return False
    # Each field must be a number, *, */N, or comma-list thereof.
    for field in m.groups():
        if not re.match(r"^[\d\*\/\,\-\d]+$", field):
            return False
    return True


# ---------------------------------------------------------------------------
# 2. Rule-based natural language patterns
# ---------------------------------------------------------------------------

# Map weekday name → cron day-of-week (0=Sun).
_WEEKDAY_MAP = {
    "sunday": 0, "sun": 0,
    "monday": 1, "mon": 1,
    "tuesday": 2, "tue": 2, "tues": 2,
    "wednesday": 3, "wed": 3,
    "thursday": 4, "thu": 4, "thurs": 4,
    "friday": 5, "fri": 5,
    "saturday": 6, "sat": 6,
}


def _parse_time_token(token: str) -> Optional[int]:
    """Return hour (0-23) from tokens like '9', '9am', '09:00', '17:30', '5pm'."""
    token = token.strip().lower().replace(" ", "")
    # 24h "HH:MM"
    m = re.match(r"^(\d{1,2}):(\d{2})$", token)
    if m:
        h, mn = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mn <= 59:
            return h * 60 + mn
    # 12h "9am", "5pm", "9:30am"
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?(am|pm)$", token)
    if m:
        h = int(m.group(1)) % 12
        if m.group(3) == "pm":
            h += 12
        mn = int(m.group(2) or 0)
        if 0 <= h <= 23 and 0 <= mn <= 59:
            return h * 60 + mn
    # Plain hour "9", "09", "17"
    m = re.match(r"^(\d{1,2})$", token)
    if m:
        h = int(m.group(1))
        if 0 <= h <= 23:
            return h * 60
    return None


def _minutes_to_cron(total_minutes: int) -> Tuple[str, str]:
    """Convert total minutes-since-midnight to (minute_field, hour_field)."""
    return f"{total_minutes % 60}", f"{total_minutes // 60}"


def _rule_based_parse(s: str) -> Optional[str]:
    """Try a handful of common English patterns. Returns cron string or None."""
    text = s.strip().lower()
    if not text:
        return None

    # ── "every N minutes/hours" ──
    m = re.match(
        r"^every\s+(\d+)\s*(minute|minutes|min|mins|hour|hours|hr|hrs)$",
        text,
    )
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit.startswith("m"):
            if 1 <= n <= 59:
                return f"*/{n} * * * *"
        else:
            if 1 <= n <= 23:
                return f"0 */{n} * * *"
        return None

    # ── "every minute" / "every hour" / "hourly at :MM" ──
    if text in ("every minute", "each minute", "per minute"):
        return "* * * * *"
    if text in ("every hour", "hourly", "each hour"):
        return "0 * * * *"
    # "hourly at :15" — fire at minute 15 of every hour
    m = re.match(r"^hourly\s+at\s+:(\d{1,2})$", text)
    if m:
        mm = int(m.group(1))
        if 0 <= mm <= 59:
            return f"{mm} * * * *"

    # ── "daily" / "every day" / "each day" — optionally followed by a time ──
    # Accepts: "daily", "Daily 08:00", "Daily at 8 AM", "every day 17:30",
    # "each day at 9pm", etc. The time token (if present) may be glued to
    # "daily" or separated by "at".
    m = re.match(
        r"^(?:daily|every\s+day|each\s+day)"
        r"(?:\s+(?:at\s+)?(.+))?$",
        text,
    )
    if m:
        time_str = (m.group(1) or "").strip()
        if time_str:
            minutes = _parse_time_token(time_str)
            if minutes is not None:
                mn, hr = _minutes_to_cron(minutes)
                return f"{mn} {hr} * * *"
            # time_str didn't parse — treat as plain "daily" below.
        # "daily" with no (parseable) time defaults to midnight.
        return "0 0 * * *"

    # ── "every weekday at HH(:MM)?" ──
    m = re.match(
        r"^(?:every\s+)?weekdays?(?:\s+at\s+(.+))?$",
        text,
    )
    if m:
        if m.group(1):
            minutes = _parse_time_token(m.group(1))
            if minutes is None:
                return None
            mn, hr = _minutes_to_cron(minutes)
            return f"{mn} {hr} * * 1-5"
        return "0 9 * * 1-5"  # 9 AM weekdays default
    if text in ("mon-fri", "monday through friday", "monday to friday"):
        return "0 9 * * 1-5"

    # ── "weekly" ──
    if text in ("weekly", "every week", "once a week"):
        return "0 9 * * 1"  # Mondays at 9 AM default

    # ── "monthly" ──
    if text in ("monthly", "every month", "once a month"):
        return "0 0 1 * *"  # 1st of month at midnight

    # ── "monthly on the Nth at HH:MM" / "monthly 1st 09:00" / "monthly at 09:00" ──
    # Cron DOM is 1-31, so we clamp at 28 to avoid scheduling the 30th/31st
    # of a month that doesn't have it. Default = 1st at midnight.
    m = re.match(
        r"^monthly(?:\s+(?:on\s+the\s+)?(\d+)(?:st|nd|rd|th)?)?"
        r"(?:\s+(?:at\s+)?(.+))?$",
        text,
    )
    if m:
        day = int(m.group(1)) if m.group(1) else 1
        time_str = (m.group(2) or "").strip()
        # Clamp day to 1-28 to keep cron valid in every month.
        day = max(1, min(28, day))
        if time_str:
            minutes = _parse_time_token(time_str)
            if minutes is None:
                return None
            mn, hr = _minutes_to_cron(minutes)
            return f"{mn} {hr} {day} * *"
        return f"0 0 {day} * *"

    # ── "every <weekday> at HH(:MM)?" ──
    m = re.match(
        r"^(?:every|each|on)\s+(sunday|sun|monday|mon|tuesday|tue|tues|wednesday|wed|"
        r"thursday|thu|thurs|friday|fri|saturday|sat)(?:\s+at\s+(.+))?$",
        text,
    )
    if m:
        dow = _WEEKDAY_MAP[m.group(1)]
        if m.group(2):
            minutes = _parse_time_token(m.group(2))
            if minutes is None:
                return None
            mn, hr = _minutes_to_cron(minutes)
            return f"{mn} {hr} * * {dow}"
        return f"0 9 * * {dow}"

    # ── "weekly <weekday> HH:MM" / "weekly monday 10:00" ──
    m = re.match(
        r"^weekly\s+(sunday|sun|monday|mon|tuesday|tue|tues|wednesday|wed|"
        r"thursday|thu|thurs|friday|fri|saturday|sat)(?:\s+(?:at\s+)?(.+))?$",
        text,
    )
    if m:
        dow = _WEEKDAY_MAP[m.group(1)]
        if m.group(2):
            minutes = _parse_time_token(m.group(2))
            if minutes is None:
                return None
            mn, hr = _minutes_to_cron(minutes)
            return f"{mn} {hr} * * {dow}"
        return f"0 9 * * {dow}"

    # ── "at HH:MM daily" / "at <time> every day" ──
    m = re.match(r"^at\s+(.+?)\s+(?:daily|every\s+day|each\s+day)$", text)
    if m:
        minutes = _parse_time_token(m.group(1))
        if minutes is None:
            return None
        mn, hr = _minutes_to_cron(minutes)
        return f"{mn} {hr} * * *"

    # ── "at <time> on <weekday>" ──
    m = re.match(
        r"^at\s+(.+?)\s+on\s+(sunday|sun|monday|mon|tuesday|tue|tues|wednesday|wed|"
        r"thursday|thu|thurs|friday|fri|saturday|sat)$",
        text,
    )
    if m:
        minutes = _parse_time_token(m.group(1))
        dow = _WEEKDAY_MAP[m.group(2)]
        if minutes is None:
            return None
        mn, hr = _minutes_to_cron(minutes)
        return f"{mn} {hr} * * {dow}"

    return None


# ---------------------------------------------------------------------------
# 3. LLM-based fallback for tricky cases
# ---------------------------------------------------------------------------

_LLM_PARSE_PROMPT = """You are a schedule parser. Convert the user's schedule description into a standard 5-field cron expression (minute hour day-of-month month day-of-week).

Rules:
- Use 24-hour times. "9 AM" → hour=9. "5 PM" → hour=17.
- Day of week: 0=Sunday, 1=Monday, ..., 6=Saturday.
- For "every Monday", use "0 9 * * 1" (default 9 AM if no time specified).
- For "daily at 8", use "0 8 * * *".
- For "every 30 minutes", use "*/30 * * * *".
- For "every weekday at 17:00", use "0 17 * * 1-5".
- For "monthly on the 1st", use "0 0 1 * *".

Respond with ONLY a JSON object: {"cron": "<expression>", "explanation": "<short human description>"}."""


def _llm_parse(s: str) -> Optional[str]:
    """Last-resort: ask the LLM to interpret the schedule. Returns cron or None."""
    try:
        from app.services.llm_service import chat_completion_json_sync
        full_prompt = f"{_LLM_PARSE_PROMPT}\n\nUser schedule: {s}\n\nRespond with ONLY a JSON object."
        result = chat_completion_json_sync(
            prompt=full_prompt,
            schema={
                "type": "object",
                "properties": {
                    "cron": {"type": "string"},
                    "explanation": {"type": "string"},
                },
                "required": ["cron"],
            },
            temperature=0.0,
        )
        if isinstance(result, dict):
            cron = result.get("cron")
            if isinstance(cron, str) and _looks_like_cron(cron):
                return cron
            # Some LLMs nest the schedule under another key.
            for v in result.values():
                if isinstance(v, str) and _looks_like_cron(v):
                    return v
    except Exception as e:
        logger.warning("LLM schedule parse failed for %r: %s", s, e)
    return None


# ---------------------------------------------------------------------------
# 4. Public API
# ---------------------------------------------------------------------------

def parse_schedule(s: str) -> str:
    """Parse a schedule string (cron OR natural language) → 5-field cron.

    Raises ``ScheduleParseError`` if the string can't be interpreted.
    """
    if s is None or not s.strip():
        raise ScheduleParseError("Empty schedule")

    s = s.strip()

    # 1. Direct cron
    if _looks_like_cron(s):
        return s

    # 2. Rule-based
    cron = _rule_based_parse(s)
    if cron:
        return cron

    # 3. LLM fallback
    cron = _llm_parse(s)
    if cron:
        return cron

    raise ScheduleParseError(
        f"Could not parse schedule: {s!r}. "
        "Try a cron expression like '0 9 * * 1' or natural language like "
        "'every Monday at 9 AM'."
    )


def safe_parse_schedule(s: str) -> Optional[str]:
    """Like parse_schedule but returns None on failure (for soft paths).

    WARNING: this may call the LLM as a last resort. Do NOT call it from
    the dispatcher tick — an LLM call there can eat the entire tick budget
    and stall every other due task. Use :func:`safe_parse_schedule_rules_only`
    inside hot paths instead.
    """
    try:
        return parse_schedule(s)
    except ScheduleParseError:
        return None


def parse_schedule_rules_only(s: str) -> Optional[str]:
    """Parse using ONLY direct-cron detection + the rule table.

    Never calls the LLM. Safe to invoke from the dispatcher tick (hot
    path). Returns the cron string or ``None`` if the rule table can't
    interpret it — the caller should then fall back to a backoff.

    The LLM-based parse is intentionally reserved for task *creation*
    time (via the API), where latency is acceptable and the result is
    persisted on ``AutomationTask.cron_expression`` so the tick never
    has to re-parse.
    """
    if s is None or not s.strip():
        return None
    s = s.strip()
    if _looks_like_cron(s):
        return s
    return _rule_based_parse(s)


def safe_parse_schedule_rules_only(s: str) -> Optional[str]:
    """Alias kept for naming symmetry with :func:`safe_parse_schedule`."""
    return parse_schedule_rules_only(s)


# ---------------------------------------------------------------------------
# 5. next_run_at — compute the next firing time
# ---------------------------------------------------------------------------

def _get_croniter():
    """Lazy import of croniter (third-party). Install if missing."""
    try:
        from croniter import croniter
        return croniter
    except ImportError:
        logger.warning("croniter not installed — falling back to basic scheduler")
        return None


def _resolve_tz(tz_name: Optional[str]):
    """Return a zoneinfo.ZoneInfo for ``tz_name``, or None if unknown/invalid.

    Falls back gracefully so a bad timezone never breaks scheduling — the
    caller treats None as "use UTC".
    """
    if not tz_name:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(tz_name)
    except Exception:
        logger.warning("unknown timezone %r — falling back to UTC", tz_name)
        return None


def next_run_at(cron: str, after: Optional[datetime] = None, tz_name: Optional[str] = None) -> datetime:
    """Return the next datetime (UTC-naive) when ``cron`` will fire after ``after``.

    When ``tz_name`` is a valid IANA timezone (e.g. "Asia/Shanghai"), the
    cron is interpreted in that timezone — so "0 8 * * *" with
    tz_name="Asia/Shanghai" means 08:00 Shanghai, converted to UTC for
    storage. This fixes the P0-6 bug where every schedule fired at the
    wall-clock time in UTC regardless of the user's location.

    ``after`` is UTC-naive (the dispatcher passes ``datetime.now(timezone.utc)``).
    The returned value is always UTC-naive so the existing UTC comparison
    (``next_run_at <= now``) is unchanged.

    Falls back to ``after + 1 hour`` if croniter is unavailable, and to UTC
    semantics if the timezone is invalid.
    """
    croniter = _get_croniter()
    base = after or datetime.now(timezone.utc).replace(tzinfo=None)
    if croniter is None:
        return base + timedelta(hours=1)
    try:
        tz = _resolve_tz(tz_name)
        if tz is not None:
            # Interpret base (UTC) in the user's timezone, advance the cron
            # there, then convert back to UTC-naive for storage.
            base_local = base.replace(tzinfo=timezone.utc).astimezone(tz)
            itr = croniter(cron, base_local)
            nxt_local = itr.get_next(datetime)  # tz-aware in user tz
            return nxt_local.astimezone(timezone.utc).replace(tzinfo=None)
        itr = croniter(cron, base)
        return itr.get_next(datetime)
    except Exception as e:
        logger.warning("croniter failed for %r (tz=%s): %s — falling back to +1h", cron, tz_name, e)
        return base + timedelta(hours=1)


def describe_schedule(cron: str) -> str:
    """Return a short human description like 'Every Monday at 09:00'."""
    if not cron:
        return "Manual"
    # Best-effort: don't try to be perfect, just give the user a hint.
    parts = cron.split()
    if len(parts) != 5:
        return cron
    mn, hr, dom, mon, dow = parts
    days = {
        "0": "Sun", "1": "Mon", "2": "Tue", "3": "Wed",
        "4": "Thu", "5": "Fri", "6": "Sat", "7": "Sun",
    }
    if dow in days:
        day = days[dow]
        if hr.isdigit() and mn.isdigit():
            return f"Every {day} at {int(hr):02d}:{int(mn):02d}"
        return f"Every {day}"
    if dow == "1-5":
        if hr.isdigit() and mn.isdigit():
            return f"Weekdays at {int(hr):02d}:{int(mn):02d}"
        return "Weekdays"
    if dow == "0,6" or dow == "6,0":
        return "Weekends"
    if dom == "*" and mon == "*" and dow == "*":
        if hr == "*" and mn == "*":
            return "Every minute"
        if hr == "*" and mn.startswith("*/"):
            return f"Every {mn[2:]} minutes"
        if hr.startswith("*/") and mn == "0":
            return f"Every {hr[2:]} hours"
        if hr.isdigit() and mn.isdigit():
            return f"Daily at {int(hr):02d}:{int(mn):02d}"
        return f"Daily (cron: {cron})"
    if dom != "*" and mon == "*" and dow == "*":
        return f"Monthly on day {dom}"
    if mon != "*":
        return f"Cron: {cron}"
    return f"Cron: {cron}"


__all__ = [
    "ScheduleParseError",
    "parse_schedule",
    "safe_parse_schedule",
    "parse_schedule_rules_only",
    "safe_parse_schedule_rules_only",
    "next_run_at",
    "describe_schedule",
]
