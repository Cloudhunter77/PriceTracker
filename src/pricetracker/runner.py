"""The daily check: fetch every source, record it, decide what to alert on."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from .alerts import Alert, apply_suspect_guard, evaluate
from .config import Item, Source, Watchlist
from .extract import ExtractionError, SellerOffer, extract_offers, extract_price
from .fetch import BrowserFetcher, Fetcher, FetchError
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


def check_source(item: Item, source: Source, fetcher, store: Store, now: datetime) -> list[Reading]:
    """Fetch and parse one source, returning readings either way.

    A list, because a price-comparison page yields one reading per shop it
    names. An ordinary product page yields exactly one.

    Any failure is recorded rather than raised: one dead shop must not stop the
    other items from being checked.
    """
    reading = Reading(checked_at=now, item=item.name, url=source.url, shop=source.display_name)
    try:
        page = fetcher.fetch(source.url)
    except FetchError as exc:
        reading.status = STATUS_ERROR
        reading.error = f"fetch failed: {exc}"
        return [reading]

    if source.type == "aggregator":
        readings = _aggregator_readings(item, source, page.html, store, now)
        if readings:
            return readings
        # No seller markup on the page. The market low is still worth having, so
        # fall through and read the page as an ordinary product page.
        log.info("%s named no sellers; falling back to the market low", source.url)

    try:
        extraction = extract_price(page.html, source.selector)
    except ExtractionError as exc:
        reading.status = STATUS_ERROR
        reading.error = str(exc)
        path = store.save_debug_html(source.url, page.html)
        log.info("saved unparsed page to %s", path)
        return [reading]

    currency = extraction.currency or source.currency
    if extraction.currency and item.target_for(extraction.currency) is None:
        reading.status = STATUS_ERROR
        reading.price = extraction.price
        reading.currency = extraction.currency
        reading.method = extraction.method
        reading.error = _no_target_message(item, extraction.currency)
        return [reading]

    reading.price = extraction.price
    reading.currency = currency
    reading.availability = extraction.availability
    reading.method = extraction.method
    return [apply_suspect_guard(reading, store)]


# ---- price-comparison pages --------------------------------------------


def _aggregator_readings(
    item: Item, source: Source, html: str, store: Store, now: datetime
) -> list[Reading]:
    """One reading per shop named on a comparison page.

    Empty when the page names no sellers, which tells the caller to read it as
    an ordinary page and take the market low instead.
    """
    offers = extract_offers(html)
    if not offers:
        return []

    readings: list[Reading] = []
    untracked: dict[str, int] = {}
    for offer in offers:
        currency = offer.currency or source.currency
        if currency and item.target_for(currency) is None:
            # Counted and reported once below rather than once per shop: thirty
            # identical errors would say nothing thirty times.
            untracked[currency] = untracked.get(currency, 0) + 1
            continue
        readings.append(_seller_reading(item, source, offer, currency, store, now))

    for currency, count in sorted(untracked.items()):
        error = Reading(
            checked_at=now,
            item=item.name,
            url=f"{source.url}#{currency.lower()}",
            shop=f"{source.display_name} ({currency})",
            status=STATUS_ERROR,
            currency=currency,
            error=(
                f"no target for {currency}: {count} shop(s) on this page price in "
                f"{currency} but this item only targets {', '.join(item.tracked_currencies)}"
            ),
        )
        readings.append(error)
    return readings


def _seller_reading(
    item: Item,
    source: Source,
    offer: SellerOffer,
    currency: str | None,
    store: Store,
    now: datetime,
) -> Reading:
    reading = Reading(
        checked_at=now,
        item=item.name,
        # History and the suspect guard are keyed by URL, so every shop needs a
        # distinct one. The offer's own link when it has one, otherwise the
        # comparison page with the shop as a fragment — still a working link.
        url=offer.url or f"{source.url}#{_slug(offer.seller)}",
        shop=_shop_name(offer.seller),
        price=offer.price,
        currency=currency,
        availability=offer.availability,
        method="aggregator",
    )
    return apply_suspect_guard(reading, store)


def _shop_name(seller: str | None) -> str:
    name = " ".join((seller or "unknown shop").split())
    return name[:60]


def _slug(seller: str | None) -> str:
    cleaned = "".join(c if c.isalnum() else "-" for c in (seller or "shop").casefold())
    return "-".join(part for part in cleaned.split("-") if part) or "shop"


def _no_target_message(item: Item, currency: str) -> str:
    # Comparing a EUR price against a HUF target would be worse than useless. A
    # currency is only acceptable once the item has a target for it — add one
    # under `targets:` to start tracking that currency.
    return (
        f"no target for {currency}: the page prices in {currency} but this item "
        f"only targets {', '.join(item.tracked_currencies)}"
    )


def run_check(
    watchlist: Watchlist,
    store: Store,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
    fetcher: Fetcher | None = None,
    browser_fetcher: BrowserFetcher | None = None,
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

    # Chromium costs a second or two to start and a few hundred MB of RAM, so it
    # is created on first use. A watchlist of ordinary shops never launches it.
    browser: BrowserFetcher | None = None

    def engine_for(source: Source):
        nonlocal browser
        if not source.needs_browser:
            return fetcher
        if browser is None:
            browser = browser_fetcher or BrowserFetcher(
                user_agent=watchlist.defaults.user_agent,
                per_domain_delay=watchlist.defaults.per_domain_delay,
            )
        return browser

    try:
        for item in items:
            readings: list[Reading] = []
            for source in item.sources:
                readings.extend(check_source(item, source, engine_for(source), store, now))
            outcome.readings.extend(readings)

            alerts, outcome.state = evaluate(item, readings, store, outcome.state, now)
            outcome.alerts.extend(alerts)
    finally:
        if owns_fetcher:
            fetcher.close()
        if browser is not None and browser is not browser_fetcher:
            browser.close()

    if not dry_run:
        # Appended after evaluation so that "previous price" comparisons are
        # made against earlier runs, not against this one.
        store.append(outcome.readings)
        if save_alert_state:
            store.save_state(outcome.state)

    return outcome
