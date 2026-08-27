"""Getting the digest to the user, and what counts as having told them.

Running with no email is a legitimate way to use this — the digest goes to
stdout instead. What must never happen is an alert being marked delivered when
it wasn't, because the cooldown would then bury it for days.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import typer

from pricetracker.alerts import REASON_TARGET, Alert
from pricetracker.cli import _deliver
from pricetracker.config import Item, Source
from pricetracker.store import Reading

NOW = datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc)


def make_alert() -> Alert:
    item = Item(
        name="Sony A7 IV váz",
        target_price=Decimal("800000"),
        currency="HUF",
        cooldown_days=3,
        drop_alert_pct=10.0,
        alert_on_out_of_stock=False,
        sources=[Source(url="https://tripont.hu/a7", currency="HUF")],
    )
    best = Reading(
        checked_at=NOW,
        item=item.name,
        url="https://tripont.hu/a7",
        shop="tripont.hu",
        price=Decimal("748999"),
        currency="HUF",
        availability="in_stock",
        method="json-ld",
    )
    return Alert(item=item, best=best, reason=REASON_TARGET, target=item.target_price)


@pytest.fixture
def no_credentials(monkeypatch):
    monkeypatch.delenv("GMAIL_USER", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)


def test_unconfigured_email_is_not_a_failure(no_credentials, capsys):
    """The bug this fixes: a perfectly good run exited 1 and the scheduler
    logged it as a crash, purely because email was deliberately switched off."""
    told = _deliver([make_alert()], [], [], dry_run=False, no_email=False)

    assert told is True
    assert "email is not configured" in capsys.readouterr().out


def test_the_digest_is_printed_when_email_is_off(no_credentials, capsys):
    """stdout is the delivery channel for a no-email setup, so it has to carry
    the same content the email would have."""
    _deliver([make_alert()], [], [], dry_run=False, no_email=False)

    out = capsys.readouterr().out
    assert "Sony A7 IV váz" in out
    assert "748 999 Ft" in out
    assert "tripont.hu" in out


def test_no_email_flag_also_prints_and_counts_as_told(capsys):
    told = _deliver([make_alert()], [], [], dry_run=False, no_email=True)
    assert told is True
    assert "Sony A7 IV váz" in capsys.readouterr().out


def test_a_broken_send_is_not_counted_as_told(monkeypatch, capsys):
    """A send that was attempted and failed must leave the alert unrecorded so
    tomorrow tries again."""
    monkeypatch.setenv("GMAIL_USER", "me@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "x" * 16)

    from pricetracker import cli
    from pricetracker.notify import EmailError

    def explode(*args, **kwargs):
        raise EmailError("could not send mail via smtp.gmail.com:465: refused")

    monkeypatch.setattr(cli, "send_digest", explode)

    told = _deliver([make_alert()], [], [], dry_run=False, no_email=False)
    assert told is False
    assert "Email not sent" in capsys.readouterr().out


def test_a_successful_send_counts_as_told(monkeypatch):
    monkeypatch.setenv("GMAIL_USER", "me@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "x" * 16)

    from pricetracker import cli

    monkeypatch.setattr(cli, "send_digest", lambda *a, **k: True)
    assert _deliver([make_alert()], [], [], dry_run=False, no_email=False) is True


def test_nothing_to_report_is_trivially_told():
    assert _deliver([], [], [], dry_run=False, no_email=False) is True


def test_dry_run_shows_the_digest_but_does_not_count_as_told(capsys):
    """--dry-run must not let the alert state advance, or the real run would
    then think you had already been told."""
    told = _deliver([make_alert()], [], [], dry_run=True, no_email=False)
    assert told is False
    assert "Sony A7 IV váz" in capsys.readouterr().out


def test_delivery_never_raises_so_the_scheduler_keeps_running(no_credentials):
    """typer.Exit subclasses RuntimeError, so anything raised here was caught by
    the scheduler's `except Exception` and logged as a failed run."""
    try:
        _deliver([make_alert()], [], [], dry_run=False, no_email=False)
    except typer.Exit as exc:  # pragma: no cover - the regression itself
        pytest.fail(f"_deliver raised typer.Exit({exc.exit_code}) on a healthy run")
