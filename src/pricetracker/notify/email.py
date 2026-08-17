"""Send the price-drop digest by email over SMTP.

Configured entirely from the environment so the same code works locally and in
GitHub Actions, where the credentials come from repository secrets.
"""

from __future__ import annotations

import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from html import escape

from ..alerts import REASON_TARGET, Alert
from ..format import format_price
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
                "(repository secrets in CI, environment variables locally)"
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


def send_alert_email(
    alerts: list[Alert],
    failures: list[Reading],
    config: EmailConfig | None = None,
) -> None:
    """Send the digest. Does nothing when there is nothing to report."""
    if not alerts:
        return
    config = config or EmailConfig.from_env()

    message = EmailMessage()
    message["Subject"] = _subject(alerts)
    message["From"] = config.sender
    message["To"] = ", ".join(config.recipients)
    message.set_content(render_text(alerts, failures))
    message.add_alternative(render_html(alerts, failures), subtype="html")

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


def _subject(alerts: list[Alert]) -> str:
    first = alerts[0]
    assert first.best.price is not None
    headline = f"{first.item.name} is {format_price(first.best.price, first.item.currency)}"
    if len(alerts) > 1:
        headline += f" (+{len(alerts) - 1} more)"
    return f"Price alert: {headline}"


def _reason_text(alert: Alert) -> str:
    if alert.reason == REASON_TARGET:
        return f"at or below your target of {format_price(alert.target, alert.item.currency)}"
    pct = alert.pct_change
    if alert.median is not None:
        return (
            f"sharp drop — {alert.item.drop_alert_pct:g}%+ below its recent median of "
            f"{format_price(alert.median, alert.item.currency)}"
        )
    return f"dropped {pct:.1f}%" if pct is not None else "dropped"


def render_text(alerts: list[Alert], failures: list[Reading]) -> str:
    lines = ["Price alerts", ""]
    for alert in alerts:
        currency = alert.item.currency
        assert alert.best.price is not None
        lines.append(f"{alert.item.name}")
        lines.append(f"  {format_price(alert.best.price, currency)} at {alert.best.shop}")
        lines.append(f"  {_reason_text(alert)}")
        if alert.previous is not None:
            change = alert.pct_change
            suffix = f" ({change:+.1f}%)" if change is not None else ""
            lines.append(f"  previous: {format_price(alert.previous, currency)}{suffix}")
        lines.append(f"  {alert.best.url}")
        lines.append("")
    if failures:
        lines.append("Sources that could not be checked:")
        for reading in failures:
            lines.append(f"  {reading.item} @ {reading.shop}: {reading.error}")
        lines.append("")
    return "\n".join(lines)


def render_html(alerts: list[Alert], failures: list[Reading]) -> str:
    cards = []
    for alert in alerts:
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
        cards.append(
            f"""
        <tr><td style="padding:0 0 20px 0">
          <table width="100%" cellpadding="0" cellspacing="0"
                 style="background:#ffffff;border:1px solid #e2e2e2;border-radius:8px">
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
        )

    failures_html = ""
    if failures:
        rows = "".join(
            f"<li>{escape(r.item)} @ {escape(r.shop)}: {escape(r.error or 'unknown error')}</li>"
            for r in failures
        )
        failures_html = (
            '<div style="font-size:13px;color:#777;padding-top:8px">'
            "<strong>Could not be checked:</strong>"
            f'<ul style="margin:6px 0 0 18px;padding:0">{rows}</ul></div>'
        )

    return f"""<!doctype html>
<html><body style="margin:0;padding:24px;background:#f6f6f6;
      font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif">
  <table width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;margin:0 auto">
    <tr><td style="font-size:13px;font-weight:600;letter-spacing:.08em;
                   text-transform:uppercase;color:#777;padding-bottom:16px">
      Price Tracker</td></tr>
    {"".join(cards)}
    <tr><td>{failures_html}</td></tr>
  </table>
</body></html>"""
