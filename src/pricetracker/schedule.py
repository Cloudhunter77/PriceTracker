"""Running the daily check on a timer, inside a container.

Deliberately not cron. Cron in a container does not inherit the container's
environment, which is precisely how GMAIL_APP_PASSWORD goes missing and the
alerts stop arriving with no visible error. A loop in the same process keeps the
environment, logs to stdout where `docker logs` can see it, and is testable.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta

log = logging.getLogger(__name__)


class ScheduleError(Exception):
    """The requested run time could not be understood."""


def parse_time(text: str) -> time:
    """Read an 'HH:MM' run time."""
    try:
        hour, minute = (int(part) for part in text.strip().split(":", 1))
        return time(hour=hour, minute=minute)
    except (ValueError, TypeError) as exc:
        raise ScheduleError(f"--at must look like 08:00, got {text!r}") from exc


def next_run(at: time, now: datetime) -> datetime:
    """The next occurrence of `at` strictly after `now`.

    Strictly after, so finishing a run at exactly the scheduled minute doesn't
    immediately trigger a second one.
    """
    candidate = now.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def seconds_until(at: time, now: datetime) -> float:
    return (next_run(at, now) - now).total_seconds()
