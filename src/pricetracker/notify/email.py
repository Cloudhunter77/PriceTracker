"""Send the daily digest by email over SMTP.

One message covers both halves of the tool: price drops worth acting on, and
events worth putting in the calendar. Configured entirely from the environment
so the same code works locally and in GitHub Actions, where the credentials come
from repository secrets.
"""

from __future__ import annotations

import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from html import escape

from ..alerts import REASON_TARGET, Alert
from ..events.models import Event
from ..format import format_distance, format_event_when, format_price
from ..store import Reading


class EmailError(Exception):
    """Email is not configured, or the send failed."""


@dataclass(slots=True)
class EmailConfig:
    host: str
    port: int
    username: str
    password: str
    sender: str
    recipients: list[str]
    use_ssl: bool = True

    @staticmethod
    def configured(env: dict[str, str] | None = None) -> bool:
        """Whether credentials are present, without raising.

        Running with no email is a legitimate choice, not a failure, so callers
        need a way to ask rather than catching an exception to find out.
        """
        env = os.environ if env is None else env
        return bool(env.get("GMAIL_USER", "").strip() and env.get("GMAIL_APP_PASSWORD", "").strip())

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> EmailConfig:
        """Read SMTP settings from the environment.

        Defaults target Gmail, which needs an app password rather than your
        account password (see the README).
        """
        env = dict(os.environ if env is None else env)
        username = env.get("GMAIL_USER", "").strip()
        password = env.get("GMAIL_APP_PASSWORD", "").strip()
        if not username or not password:
            raise EmailError(
                "email is not configured: set GMAIL_USER and GMAIL_APP_PASSWORD "
                "in the environment"
            )
        recipients = [
            addr.strip()
            for addr in env.get("ALERT_EMAIL_TO", username).split(",")
            if addr.strip()
        ]
        port = int(env.get("SMTP_PORT", "465"))
        return cls(
            host=env.get("SMTP_HOST", "smtp.gmail.com"),
            port=port,
            username=username,
            password=password,
            sender=env.get("ALERT_EMAIL_FROM", username),
            recipients=recipients,
            use_ssl=env.get("SMTP_USE_SSL", "").lower() not in ("0", "false", "no"),
        )


def send_digest(
    alerts: list[Alert],
    events: list[Event] | None = None,
    failures: list[Reading] | None = None,
    config: EmailConfig | None = None,
) -> bool:
    """Send the digest. Does nothing when there is nothing to report.

    Returns True if a message was actually sent.
    """
    events = events or []
    failures = failures or []
    if not alerts and not events:
        return False
    config = config or EmailConfig.from_env()

    message = EmailMessage()
    message["Subject"] = subject(alerts, events)
    message["From"] = config.sender
    message["To"] = ", ".join(config.recipients)
    message.set_content(render_text(alerts, events, failures))
    message.add_alternative(render_html(alerts, events, failures), subtype="html")

    try:
        if config.use_ssl:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(config.host, config.port, context=context) as smtp:
                smtp.login(config.username, config.password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(config.host, config.port) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(config.username, config.password)
                smtp.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailError(f"could not send mail via {config.host}:{config.port}: {exc}") from exc
    return True


def subject(alerts: list[Alert], events: list[Event]) -> str:
    """Lead with the price drop if there is one — that's the time-sensitive half."""
    if alerts:
        first = alerts[0]
        assert first.best.price is not None
        line = f"{first.item.name} is {format_price(first.best.price, first.item.currency)}"
        extras = []
        if len(alerts) > 1:
            extras.append(f"+{len(alerts) - 1} more")
        if events:
            extras.append(f"{len(events)} event{'s' if len(events) != 1 else ''}")
        suffix = f" ({', '.join(extras)})" if extras else ""
        return f"Price alert: {line}{suffix}"
    count = len(events)
    return f"{count} event{'s' if count != 1 else ''} coming up near you"


def _reason_text(alert: Alert) -> str:
    if alert.reason == REASON_TARGET:
        return f"at or below your target of {format_price(alert.target, alert.item.currency)}"
    if alert.median is not None:
        return (
            f"sharp drop — {alert.item.drop_alert_pct:g}%+ below its recent median of "
            f"{format_price(alert.median, alert.item.currency)}"
        )
    pct = alert.pct_change
    return f"dropped {pct:.1f}%" if pct is not None else "dropped"


def _event_where(event: Event) -> str:
    """Venue and distance, whichever of them we actually know."""
    bits = [b for b in (event.venue, format_distance(event.distance_km)) if b]
    return " · ".join(bits)


# ---- plain text --------------------------------------------------------


def render_text(alerts: list[Alert], events: list[Event], failures: list[Reading]) -> str:
    lines: list[str] = []

    if alerts:
        lines += ["PRICE ALERTS", ""]
        for alert in alerts:
            currency = alert.item.currency
            assert alert.best.price is not None
            lines.append(alert.item.name)
            lines.append(f"  {format_price(alert.best.price, currency)} at {alert.best.shop}")
            lines.append(f"  {_reason_text(alert)}")
            if alert.previous is not None:
                change = alert.pct_change
                suffix = f" ({change:+.1f}%)" if change is not None else ""
                lines.append(f"  previous: {format_price(alert.previous, currency)}{suffix}")
            lines.append(f"  {alert.best.url}")
            lines.append("")

    if events:
        lines += ["COMING UP NEARBY", ""]
        for event in events:
            lines.append(f"{format_event_when(event.starts_at, event.ends_at)} — {event.title}")
            where = _event_where(event)
            if where:
                lines.append(f"  {where}")
            details = []
            if event.price is not None:
                details.append(format_price(event.price, event.currency))
            if event.interests:
                details.append(", ".join(event.interests))
            if details:
                lines.append(f"  {' · '.join(details)}")
            if event.url:
                lines.append(f"  {event.url}")
            lines.append("")

    if failures:
        lines.append("Sources that could not be checked:")
        for reading in failures:
            lines.append(f"  {reading.item} @ {reading.shop}: {reading.error}")
        lines.append("")

    return "\n".join(lines)


# ---- html --------------------------------------------------------------

_CARD = (
    "background:#ffffff;border:1px solid #e2e2e2;border-radius:8px"
)
_HEADING = (
    "font-size:13px;font-weight:600;letter-spacing:.08em;"
    "text-transform:uppercase;color:#777;padding:8px 0 14px"
)


def _price_card(alert: Alert) -> str:
    currency = alert.item.currency
    assert alert.best.price is not None
    change = alert.pct_change
    change_html = ""
    if alert.previous is not None and change is not None:
        colour = "#128a52" if change < 0 else "#8a1212"
        change_html = (
            f'<div style="font-size:14px;color:#555">was '
            f"{escape(format_price(alert.previous, currency))} "
            f'<span style="color:{colour};font-weight:600">({change:+.1f}%)</span></div>'
        )
    return f"""
        <tr><td style="padding:0 0 20px 0">
          <table width="100%" cellpadding="0" cellspacing="0" style="{_CARD}">
            <tr><td style="padding:18px 20px">
              <div style="font-size:17px;font-weight:600;color:#111">
                {escape(alert.item.name)}</div>
              <div style="font-size:30px;font-weight:700;color:#128a52;padding:8px 0 2px">
                {escape(format_price(alert.best.price, currency))}</div>
              <div style="font-size:14px;color:#555">at {escape(alert.best.shop)} &middot;
                {escape(_reason_text(alert))}</div>
              {change_html}
              <div style="padding-top:14px">
                <a href="{escape(alert.best.url)}"
                   style="background:#111;color:#fff;text-decoration:none;padding:9px 16px;
                          border-radius:6px;font-size:14px;display:inline-block">View offer</a>
              </div>
            </td></tr>
          </table>
        </td></tr>"""


def _event_row(event: Event) -> str:
    where = _event_where(event)
    details = []
    if event.price is not None:
        details.append(escape(format_price(event.price, event.currency)))
    if event.interests:
        details.append(escape(", ".join(event.interests)))
    title = escape(event.title)
    if event.url:
        title = (
            f'<a href="{escape(event.url)}" style="color:#111;text-decoration:none">{title}</a>'
        )
    return f"""
        <tr><td style="padding:0 0 10px 0">
          <table width="100%" cellpadding="0" cellspacing="0" style="{_CARD}">
            <tr><td style="padding:14px 18px">
              <div style="font-size:12px;font-weight:600;color:#8a5a12;
                          text-transform:uppercase;letter-spacing:.05em">
                {escape(format_event_when(event.starts_at, event.ends_at))}</div>
              <div style="font-size:16px;font-weight:600;color:#111;padding:4px 0 2px">
                {title}</div>
              <div style="font-size:13px;color:#666">{escape(where)}</div>
              <div style="font-size:13px;color:#666">{' &middot; '.join(details)}</div>
            </td></tr>
          </table>
        </td></tr>"""


def render_html(alerts: list[Alert], events: list[Event], failures: list[Reading]) -> str:
    blocks: list[str] = []

    if alerts:
        blocks.append(f'<tr><td style="{_HEADING}">Price alerts</td></tr>')
        blocks += [_price_card(a) for a in alerts]

    if events:
        blocks.append(f'<tr><td style="{_HEADING}">Coming up nearby</td></tr>')
        blocks += [_event_row(e) for e in events]

    if failures:
        rows = "".join(
            f"<li>{escape(r.item)} @ {escape(r.shop)}: {escape(r.error or 'unknown error')}</li>"
            for r in failures
        )
        blocks.append(
            '<tr><td><div style="font-size:13px;color:#777;padding-top:12px">'
            "<strong>Could not be checked:</strong>"
            f'<ul style="margin:6px 0 0 18px;padding:0">{rows}</ul></div></td></tr>'
        )

    return f"""<!doctype html>
<html><body style="margin:0;padding:24px;background:#f6f6f6;
      font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif">
  <table width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;margin:0 auto">
    <tr><td style="font-size:13px;font-weight:600;letter-spacing:.08em;
                   text-transform:uppercase;color:#777;padding-bottom:4px">
      Price Tracker</td></tr>
    {"".join(blocks)}
  </table>
</body></html>"""
