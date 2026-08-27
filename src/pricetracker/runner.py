"""The daily check: fetch every source, record it, decide what to alert on."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from .alerts import Alert, apply_suspect_guard, evaluate
from .config import Item, Source, Watchlist
from .extract import ExtractionError, extract_price
from .fetch import Fetcher, FetchError
from .store import STATUS_ERROR, AlertState, Reading, Store, utcnow

log = logging.getLogger(__name__)


@dataclass
class CheckOutcome:
    readings: list[Reading] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)
    state: dict[str, AlertState] = field(default_factory=dict)

    @property
    def failures(self) -> list[Reading]:
        return [r for r in self.readings if r.status == STATUS_ERROR]

    @property
    def successes(self) -> list[Reading]:
        return [r for r in self.readings if r.ok]


def check_source(item: Item, source: Source, fetcher: Fetcher, store: Store, now: datetime) -> Reading:
    """Fetch and parse one source, returning a Reading either way.

    Any failure is recorded rather than raised: one dead shop must not stop the
    other items from being checked.
    """
    reading = Reading(checked_at=now, item=item.name, url=source.url, shop=source.display_name)
    try:
        page = fetcher.fetch(source.url)
    except FetchError as exc:
        reading.status = STATUS_ERROR
        reading.error = f"fetch failed: {exc}"
        return reading

    try:
        extraction = extract_price(page.html, source.selector)
    except ExtractionError as exc:
        reading.status = STATUS_ERROR
        reading.error = str(exc)
        path = store.save_debug_html(source.url, page.html)
        log.info("saved unparsed page to %s", path)
        return reading

    currency = extraction.currency or source.currency
    if extraction.currency and item.target_for(extraction.currency) is None:
        # Comparing a EUR price against a HUF target would be worse than
        # useless. A currency is only acceptable once the item has a target for
        # it — add one under `targets:` to start tracking that currency.
        reading.status = STATUS_ERROR
        reading.price = extraction.price
        reading.currency = extraction.currency
        reading.method = extraction.method
        reading.error = (
            f"no target for {extraction.currency}: the page prices in "
            f"{extraction.currency} but this item only targets "
            f"{', '.join(item.tracked_currencies)}"
        )
        return reading

    reading.price = extraction.price
    reading.currency = currency
    reading.availability = extraction.availability
    reading.method = extraction.method
    return apply_suspect_guard(reading, store)


def run_check(
    watchlist: Watchlist,
    store: Store,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
    fetcher: Fetcher | None = None,
    only: str | None = None,
    save_alert_state: bool = True,
) -> CheckOutcome:
    """Check every enabled item and work out which ones deserve an alert.

    `save_alert_state` exists because "we have told the user" is only true once
    delivery has actually happened. Callers that send a digest should pass False
    and call `store.save_state(outcome.state)` themselves afterwards; otherwise a
    failed send would mark the alert as delivered and the cooldown would bury it
    for days.
    """
    now = now or utcnow()
    outcome = CheckOutcome(state=store.load_state())

    items = watchlist.active_items
    if only:
        items = [i for i in items if i.name.casefold() == only.casefold()]

    owns_fetcher = fetcher is None
    fetcher = fetcher or Fetcher(
        user_agent=watchlist.defaults.user_agent,
        timeout=watchlist.defaults.request_timeout,
        per_domain_delay=watchlist.defaults.per_domain_delay,
        respect_robots=watchlist.defaults.respect_robots,
    )
    try:
        for item in items:
            readings = [check_source(item, source, fetcher, store, now) for source in item.sources]
            outcome.readings.extend(readings)

            alerts, outcome.state = evaluate(item, readings, store, outcome.state, now)
            outcome.alerts.extend(alerts)
    finally:
        if owns_fetcher:
            fetcher.close()

    if not dry_run:
        # Appended after evaluation so that "previous price" comparisons are
        # made against earlier runs, not against this one.
        store.append(outcome.readings)
        if save_alert_state:
            store.save_state(outcome.state)

    return outcome
