from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from pricetracker.alerts import REASON_DROP, REASON_TARGET, Alert
from pricetracker.config import Item, Source
from pricetracker.format import format_price
from pricetracker.notify.email import (
    EmailConfig,
    EmailError,
    render_html,
    render_text,
    send_digest,
    subject,
)
from pricetracker.store import Reading

NOW = datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc)


def make_alert(reason=REASON_TARGET, previous=None, median=None) -> Alert:
    item = Item(
        name="Sony A7 IV",
        target_price=Decimal("1500000"),
        currency="HUF",
        cooldown_days=3,
        drop_alert_pct=10.0,
        alert_on_out_of_stock=False,
        sources=[Source(url="https://alza.hu/a7", currency="HUF")],
    )
    best = Reading(
        checked_at=NOW,
        item=item.name,
        url="https://alza.hu/a7",
        shop="alza.hu",
        price=Decimal("1299900"),
        currency="HUF",
        availability="in_stock",
        method="json-ld",
    )
    return Alert(
        item=item, best=best, reason=reason, target=item.target_price, previous=previous, median=median
    )


@pytest.mark.parametrize(
    "amount,currency,expected",
    [
        (Decimal("1299900"), "HUF", "1 299 900 Ft"),
        (Decimal("1299.00"), "USD", "$1,299.00"),
        (Decimal("2499.5"), "EUR", "€2,499.50"),
        (Decimal("1699"), "GBP", "£1,699.00"),
        (Decimal("500"), "PLN", "500.00 PLN"),
        (None, "HUF", "—"),
    ],
)
def test_price_formatting(amount, currency, expected):
    assert format_price(amount, currency) == expected


def test_text_email_contains_the_essentials():
    body = render_text([make_alert(previous=Decimal("1499900"))], [], [])
    assert "Sony A7 IV" in body
    assert "1 299 900 Ft" in body
    assert "alza.hu" in body
    assert "https://alza.hu/a7" in body
    assert "-13.3%" in body


def test_html_email_contains_the_essentials():
    body = render_html([make_alert(previous=Decimal("1499900"))], [], [])
    assert "Sony A7 IV" in body
    assert "1 299 900 Ft" in body
    assert 'href="https://alza.hu/a7"' in body


def test_drop_alert_explains_itself():
    body = render_text([make_alert(reason=REASON_DROP, median=Decimal("1600000"))], [], [])
    assert "sharp drop" in body
    assert "1 600 000 Ft" in body


def test_failures_are_reported_alongside_alerts():
    failure = Reading(
        checked_at=NOW,
        item="Tripod",
        url="https://shop.example/x",
        shop="shop.example",
        status="error",
        error="HTTP 403",
    )
    for body in (render_text([make_alert()], [], [failure]), render_html([make_alert()], [], [failure])):
        assert "Tripod" in body
        assert "HTTP 403" in body


def test_untrusted_text_is_escaped_in_html():
    """Item names and error text end up in HTML; they must not inject markup."""
    failure = Reading(
        checked_at=NOW,
        item="<script>alert(1)</script>",
        url="https://x.example",
        shop="x",
        status="error",
        error="broke & failed",
    )
    body = render_html([make_alert()], [], [failure])
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body
    assert "broke &amp; failed" in body


def test_nothing_is_sent_when_there_is_nothing_to_report():
    # No SMTP config present, so this would raise if it tried to send.
    assert send_digest([], [], [], config=None) is False


def test_missing_credentials_explain_what_to_set():
    with pytest.raises(EmailError, match="GMAIL_USER"):
        EmailConfig.from_env({})


def test_config_defaults_to_gmail_and_self():
    config = EmailConfig.from_env({"GMAIL_USER": "me@gmail.com", "GMAIL_APP_PASSWORD": "x" * 16})
    assert config.host == "smtp.gmail.com"
    assert config.port == 465
    assert config.recipients == ["me@gmail.com"]
    assert config.sender == "me@gmail.com"


def test_config_accepts_multiple_recipients_and_overrides():
    config = EmailConfig.from_env(
        {
            "GMAIL_USER": "me@gmail.com",
            "GMAIL_APP_PASSWORD": "x",
            "ALERT_EMAIL_TO": "a@x.com, b@y.com",
            "SMTP_HOST": "smtp.fastmail.com",
            "SMTP_PORT": "587",
            "SMTP_USE_SSL": "false",
        }
    )
    assert config.recipients == ["a@x.com", "b@y.com"]
    assert config.host == "smtp.fastmail.com"
    assert config.port == 587
    assert config.use_ssl is False


# ---- events in the digest ----------------------------------------------


def make_event(title="Éjszakai Koncert", **kwargs):
    from datetime import timedelta

    from pricetracker.events.models import Event

    defaults = dict(
        starts_at=NOW + timedelta(days=3),
        source="port.hu",
        venue="A38 Hajó",
        url="https://example.hu/koncert",
        price=Decimal("4500"),
        currency="HUF",
        distance_km=2.9,
        interests=["Live music"],
    )
    defaults.update(kwargs)
    return Event(title=title, **defaults)


def test_events_appear_in_both_renderings():
    for body in (render_text([], [make_event()], []), render_html([], [make_event()], [])):
        assert "Éjszakai Koncert" in body
        assert "A38 Hajó" in body
        assert "4 500 Ft" in body
        assert "2.9 km away" in body


def test_event_without_a_known_distance_says_nothing_about_it():
    body = render_text([], [make_event(distance_km=None)], [])
    assert "km away" not in body
    assert "A38 Hajó" in body


def test_digest_carries_prices_and_events_together():
    for body in (
        render_text([make_alert()], [make_event()], []),
        render_html([make_alert()], [make_event()], []),
    ):
        assert "Sony A7 IV" in body
        assert "Éjszakai Koncert" in body


def test_event_titles_are_escaped_in_html():
    body = render_html([], [make_event("<b>Koncert</b>")], [])
    assert "<b>Koncert</b>" not in body
    assert "&lt;b&gt;Koncert&lt;/b&gt;" in body


@pytest.mark.parametrize(
    "alerts,events,expected",
    [
        (1, 0, "Price alert: Sony A7 IV is 1 299 900 Ft"),
        (2, 0, "Price alert: Sony A7 IV is 1 299 900 Ft (+1 more)"),
        (1, 3, "Price alert: Sony A7 IV is 1 299 900 Ft (3 events)"),
        (0, 1, "1 event coming up near you"),
        (0, 4, "4 events coming up near you"),
    ],
)
def test_subject_line(alerts, events, expected):
    assert subject([make_alert()] * alerts, [make_event()] * events) == expected


def test_a_digest_with_only_events_is_still_sent():
    """Events alone are worth an email, even with no price drops."""
    from pricetracker.notify.email import EmailConfig

    config = EmailConfig(
        host="localhost", port=1, username="u", password="p",
        sender="a@b.c", recipients=["d@e.f"], use_ssl=False,
    )
    with pytest.raises(EmailError, match="could not send"):
        send_digest([], [make_event()], [], config=config)


# ---- email being off is a supported state ------------------------------


@pytest.mark.parametrize(
    "env,expected",
    [
        ({"GMAIL_USER": "me@gmail.com", "GMAIL_APP_PASSWORD": "x" * 16}, True),
        ({}, False),
        ({"GMAIL_USER": "me@gmail.com"}, False),
        ({"GMAIL_APP_PASSWORD": "x"}, False),
        ({"GMAIL_USER": "  ", "GMAIL_APP_PASSWORD": "x"}, False),
    ],
)
def test_configured_answers_without_raising(env, expected):
    """Callers need to ask whether email is set up, not catch an exception."""
    assert EmailConfig.configured(env) is expected


def test_a_euro_alert_reads_in_euros_not_forints():
    """The item's primary currency is HUF; the alert came from a Slovak shop.
    Rendering 1 150 as forints would be wildly misleading."""
    from pricetracker.config import Item, Source

    item = Item(
        name="Sony A6700 váz",
        target_price=Decimal("500000"),
        targets={"EUR": Decimal("1200")},
        currency="HUF",
        cooldown_days=3,
        drop_alert_pct=10.0,
        alert_on_out_of_stock=False,
        sources=[Source(url="https://obchod.sk/a6700", currency="EUR")],
    )
    best = Reading(
        checked_at=NOW,
        item=item.name,
        url="https://obchod.sk/a6700",
        shop="obchod.sk",
        price=Decimal("1150"),
        currency="EUR",
        availability="in_stock",
        method="json-ld",
    )
    alert = Alert(item=item, best=best, reason=REASON_TARGET, target=Decimal("1200"))

    for body in (render_text([alert], [], []), render_html([alert], [], [])):
        assert "€1,150.00" in body
        assert "EUR target of €1,200.00" in body
        assert "Ft" not in body

    assert subject([alert], []) == "Price alert: Sony A6700 váz is €1,150.00"
