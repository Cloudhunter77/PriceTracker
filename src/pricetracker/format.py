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
