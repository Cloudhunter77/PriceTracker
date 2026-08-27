"""Decide when a price is actually worth an email.

Two things matter more than the threshold check itself:

  * Not repeating yourself. An alert that arrives every morning for the same
    unchanged price gets muted within a week, which makes the whole tool
    useless. Once we've alerted, we stay quiet until the price drops materially
    further or a cooldown passes.
  * Not trusting a wild reading. A price far below everything seen before is
    usually a mis-parse (an accessory, or a "from" price), not a fire sale, so
    it has to show up twice before we act on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from .config import Item
from .store import STATUS_SUSPECT, AlertState, Reading, Store

# A price this far below the last known one is treated as a parse error until a
# second run confirms it.
SUSPECT_DROP_RATIO = Decimal("0.30")
# How close a repeat reading must be to count as confirming a suspect price.
SUSPECT_CONFIRM_TOLERANCE = Decimal("0.05")
# After alerting, a further drop of at least this much alerts again immediately.
RE_ALERT_DROP = Decimal("0.01")

REASON_TARGET = "target"
REASON_DROP = "drop"


@dataclass(slots=True)
class Alert:
    """A price movement worth telling the user about."""

    item: Item
    best: Reading
    reason: str
    target: Decimal
    previous: Decimal | None = None
    median: Decimal | None = None

    @property
    def pct_below_target(self) -> Decimal | None:
        if not self.target:
            return None
        assert self.best.price is not None
        return (self.target - self.best.price) / self.target * 100

    @property
    def pct_change(self) -> Decimal | None:
        """Change against the previous run, negative when the price fell."""
        if self.previous is None or not self.previous:
            return None
        assert self.best.price is not None
        return (self.best.price - self.previous) / self.previous * 100


def apply_suspect_guard(reading: Reading, store: Store) -> Reading:
    """Flag implausibly large drops so a mis-parse can't trigger an alert.

    The flag clears itself: if the next run reads roughly the same low price,
    it was real and the reading is accepted.
    """
    if not reading.ok or reading.price is None:
        return reading
    last_ok = store.last_ok_for_url(reading.url)
    if last_ok is None or last_ok.price is None or last_ok.price <= 0:
        return reading
    if last_ok.currency != reading.currency:
        return reading
    if reading.price >= last_ok.price * SUSPECT_DROP_RATIO:
        return reading

    previous = store.last_reading_for_url(reading.url)
    if (
        previous is not None
        and previous.status == STATUS_SUSPECT
        and previous.price is not None
        and previous.price > 0
        and abs(reading.price - previous.price) / previous.price <= SUSPECT_CONFIRM_TOLERANCE
    ):
        # Seen twice at the same level — believe it.
        return reading

    reading.status = STATUS_SUSPECT
    reading.error = (
        f"price {reading.price} is more than "
        f"{(1 - SUSPECT_DROP_RATIO) * 100:.0f}% below the last known {last_ok.price}; "
        "holding until the next run confirms it"
    )
    return reading


def eligible_readings(
    item: Item, readings: list[Reading], currency: str | None = None
) -> list[Reading]:
    """Readings that may be acted on: parsed cleanly, priced in a currency the
    item has a target for, and buyable.

    A currency without a target is excluded rather than compared, since there
    is no number to compare it against.
    """
    out = []
    for reading in readings:
        if not reading.ok or reading.price is None:
            continue
        code = reading.currency or item.currency
        if currency is not None and code != currency:
            continue
        if item.target_for(code) is None:
            continue
        if reading.in_stock is False and not item.alert_on_out_of_stock:
            continue
        out.append(reading)
    return out


def best_reading(
    item: Item, readings: list[Reading], currency: str | None = None
) -> Reading | None:
    """The cheapest usable price, within one currency.

    Prices in different currencies are never compared to each other — no
    exchange rate is applied anywhere, by design — so "cheapest" is only
    meaningful inside a single currency.
    """
    usable = eligible_readings(item, readings, currency)
    if not usable:
        return None
    return min(usable, key=lambda r: r.price)  # type: ignore[arg-type,return-value]


def currencies_present(item: Item, readings: list[Reading]) -> list[str]:
    """Which of the item's tracked currencies actually came back priced."""
    seen = {r.currency or item.currency for r in eligible_readings(item, readings)}
    return [c for c in item.tracked_currencies if c in seen]


def state_key(item: Item, currency: str) -> str:
    """Alert state is per item *and* currency.

    Keying on the item alone would let a forint alert silence a euro one for
    the length of the cooldown, hiding a genuinely separate offer.
    """
    return item.name if currency == item.currency else f"{item.name} [{currency}]"


def evaluate(
    item: Item,
    readings: list[Reading],
    store: Store,
    state: dict[str, AlertState],
    now: datetime,
) -> tuple[list[Alert], dict[str, AlertState]]:
    """Decide which of this item's prices warrant an alert right now.

    Each currency is judged separately against its own target, so a Slovak shop
    in euros and a Hungarian one in forints can each raise their own alert
    without either being converted into the other.
    """
    state = dict(state)
    alerts: list[Alert] = []

    for currency in currencies_present(item, readings):
        alert, state = _evaluate_currency(item, readings, store, state, now, currency)
        if alert is not None:
            alerts.append(alert)
    return alerts, state


def _evaluate_currency(
    item: Item,
    readings: list[Reading],
    store: Store,
    state: dict[str, AlertState],
    now: datetime,
    currency: str,
) -> tuple[Alert | None, dict[str, AlertState]]:
    """The original single-currency rule, applied to one currency's readings."""
    key = state_key(item, currency)
    best = best_reading(item, readings, currency)
    if best is None or best.price is None:
        return None, state

    target = item.target_for(currency)
    if target is None:
        return None, state

    median = store.median_best(item.name, currency, now=now)
    previous = store.previous_best(item.name, currency)

    reason: str | None = None
    if best.price <= target:
        reason = REASON_TARGET
    elif median is not None and item.drop_alert_pct:
        threshold = median * (Decimal(1) - Decimal(str(item.drop_alert_pct)) / Decimal(100))
        if best.price <= threshold:
            reason = REASON_DROP

    if reason is None:
        # Price is back to normal — forget that we alerted, so the next drop
        # gets through instead of being swallowed by the cooldown.
        state.pop(key, None)
        return None, state

    if not _should_notify(item, best.price, state.get(key), now):
        return None, state

    state[key] = AlertState(price=best.price, at=now, reason=reason)
    return (
        Alert(item=item, best=best, reason=reason, target=target, previous=previous, median=median),
        state,
    )


def _should_notify(
    item: Item, price: Decimal, previous: AlertState | None, now: datetime
) -> bool:
    """Suppress a repeat of an alert the user has already seen."""
    if previous is None:
        return True
    if price <= previous.price * (Decimal(1) - RE_ALERT_DROP):
        return True  # dropped meaningfully further since we last wrote
    return now - previous.at >= timedelta(days=item.cooldown_days)
