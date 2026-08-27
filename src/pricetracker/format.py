"""Money formatting shared by the CLI and the email templates."""

from __future__ import annotations

from decimal import Decimal

# Currencies conventionally written without minor units.
_ZERO_DECIMAL = {"HUF", "JPY", "KRW", "CLP", "ISK", "VND"}

_SUFFIX = {"HUF": " Ft"}
_PREFIX = {"USD": "$", "EUR": "€", "GBP": "£"}


def format_price(amount: Decimal | None, currency: str | None) -> str:
    """Render a price the way the shop would, e.g. '1 299 900 Ft' or '$1,299.00'."""
    if amount is None:
        return "—"
    code = (currency or "").upper()
    if code in _ZERO_DECIMAL:
        # A plain space, not U+202F: it survives copy-paste out of a terminal
        # and renders as itself in every mail client.
        number = f"{amount:,.0f}".replace(",", " ")
    else:
        number = f"{amount:,.2f}"
    if code in _SUFFIX:
        return f"{number}{_SUFFIX[code]}"
    if code in _PREFIX:
        return f"{_PREFIX[code]}{number}"
    return f"{number} {code}".strip()


def format_event_when(starts_at, ends_at=None) -> str:
    """Render an event's time as 'Fri 4 Sep, 20:00', or a range across days."""
    day = starts_at.strftime("%a %-d %b")
    time = starts_at.strftime("%H:%M")
    # Midnight almost always means "date only", not an event starting at 00:00.
    start = day if time == "00:00" else f"{day}, {time}"
    if ends_at is None or ends_at.date() == starts_at.date():
        return start
    return f"{start} – {ends_at.strftime('%a %-d %b')}"


def format_distance(km: float | None) -> str:
    """Distance from home, or an honest blank when the venue wasn't resolvable."""
    if km is None:
        return ""
    if km < 1:
        return f"{km * 1000:.0f} m away"
    return f"{km:.1f} km away"
