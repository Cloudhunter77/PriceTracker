from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from pricetracker.alerts import REASON_DROP, REASON_TARGET, Alert
from pricetracker.config import Item, Source
from pricetracker.format import format_price
from pricetracker.notify.email import EmailConfig, EmailError, render_html, render_text, send_alert_email
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
    body = render_text([make_alert(previous=Decimal("1499900"))], failures=[])
    assert "Sony A7 IV" in body
    assert "1 299 900 Ft" in body
    assert "alza.hu" in body
    assert "https://alza.hu/a7" in body
    assert "-13.3%" in body


def test_html_email_contains_the_essentials():
    body = render_html([make_alert(previous=Decimal("1499900"))], failures=[])
    assert "Sony A7 IV" in body
    assert "1 299 900 Ft" in body
    assert 'href="https://alza.hu/a7"' in body


def test_drop_alert_explains_itself():
    body = render_text([make_alert(reason=REASON_DROP, median=Decimal("1600000"))], failures=[])
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
    for body in (render_text([make_alert()], [failure]), render_html([make_alert()], [failure])):
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
    body = render_html([make_alert()], [failure])
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body
    assert "broke &amp; failed" in body


def test_nothing_is_sent_when_there_are_no_alerts():
    # No SMTP config present, so this would raise if it tried to send.
    send_alert_email([], failures=[], config=None)


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
