"""The container's timer."""

from __future__ import annotations

from datetime import datetime, time, timedelta

import pytest

from pricetracker.schedule import ScheduleError, next_run, parse_time, seconds_until


@pytest.mark.parametrize(
    "text,expected",
    [("08:00", time(8, 0)), ("8:5", time(8, 5)), ("23:59", time(23, 59)), (" 07:30 ", time(7, 30))],
)
def test_parse_time(text, expected):
    assert parse_time(text) == expected


@pytest.mark.parametrize("bad", ["", "eight", "25:00", "08:99", "08", "a:b"])
def test_bad_times_are_rejected_with_an_example(bad):
    with pytest.raises(ScheduleError, match="08:00"):
        parse_time(bad)


def test_runs_later_today_when_the_time_has_not_passed():
    now = datetime(2026, 9, 1, 6, 30)
    assert next_run(time(8, 0), now) == datetime(2026, 9, 1, 8, 0)


def test_runs_tomorrow_when_the_time_has_passed():
    now = datetime(2026, 9, 1, 9, 30)
    assert next_run(time(8, 0), now) == datetime(2026, 9, 2, 8, 0)


def test_exactly_on_the_hour_waits_a_full_day():
    """Otherwise a run finishing inside its own minute triggers itself again."""
    now = datetime(2026, 9, 1, 8, 0, 0)
    assert next_run(time(8, 0), now) == datetime(2026, 9, 2, 8, 0)


def test_a_run_that_overruns_still_schedules_the_next_day():
    now = datetime(2026, 9, 1, 8, 4, 30)
    assert next_run(time(8, 0), now) == datetime(2026, 9, 2, 8, 0)


def test_seconds_until_is_positive_and_under_a_day():
    now = datetime(2026, 9, 1, 8, 0, 1)
    wait = seconds_until(time(8, 0), now)
    assert 0 < wait <= timedelta(days=1).total_seconds()


def test_seconds_until_matches_the_gap():
    now = datetime(2026, 9, 1, 6, 0)
    assert seconds_until(time(8, 30), now) == 2.5 * 3600
