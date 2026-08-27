"""Command line interface."""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from .config import (
    DEFAULT_WATCHLIST,
    ConfigError,
    append_item,
    load_watchlist,
)
from .extract import METHODS, extract_all
from .fetch import Fetcher, FetchError
from .format import format_price
from .notify import EmailConfig, EmailError, send_digest
from .notify.email import render_text
from .runner import run_check
from .store import HISTORY_FILE, STATE_FILE, Store
from .events.config import DEFAULT_EVENTS_CONFIG, load_events_config
from .events.runner import check_events
from .events.store import EventStore
from .format import format_distance, format_event_when

app = typer.Typer(
    add_completion=False,
    help="Track prices of your shopping list across shops and get alerted on drops.",
)
console = Console()
log = logging.getLogger("pricetracker")

WatchlistOption = typer.Option(DEFAULT_WATCHLIST, "--watchlist", "-w", help="Path to watchlist.yaml")


def _shorten(text: str | None, width: int = 44) -> str:
    """Keep the table readable; the full text is in the history and the email."""
    if not text:
        return ""
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= width else collapsed[: width - 1] + "…"


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )


def _load(path: Path):
    try:
        return load_watchlist(path)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2)
    except ValueError as exc:  # pydantic validation
        console.print(f"[red]{path} is not valid:[/red]\n{exc}")
        raise typer.Exit(code=2)


def _deliver(alerts, events, failures, *, dry_run: bool, no_email: bool) -> bool:
    """Get the digest to the user, and report whether they were actually told.

    Three outcomes, not two. Email being switched off is a supported way to run
    — the digest goes to stdout instead, which is what `docker logs` shows — and
    that still counts as told. Only a send that was attempted and broke is an
    error worth failing the run over.
    """
    if not alerts and not events:
        return True
    if dry_run:
        console.print("[dim](dry run — nothing sent, nothing recorded)[/dim]")
        console.print(render_text(alerts, events, failures))
        return False

    reason = "--no-email" if no_email else (None if EmailConfig.configured() else "email is not configured")
    if reason:
        console.print(f"[dim]({reason} — printing the digest instead)[/dim]")
        console.print(render_text(alerts, events, failures))
        return True

    try:
        if send_digest(alerts, events, failures):
            console.print("[green]Digest email sent.[/green]")
        return True
    except EmailError as exc:
        # Genuinely broken delivery. Returning False leaves the alert unrecorded
        # so tomorrow tries again rather than burying it under the cooldown.
        console.print(f"[red]Email not sent: {exc}[/red]")
        return False


@app.command()
def check(
    watchlist: Path = WatchlistOption,
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Check prices but write nothing and send no email"
    ),
    no_email: bool = typer.Option(False, "--no-email", help="Record prices but skip the email"),
    item: Optional[str] = typer.Option(None, "--item", help="Check only this item, by name"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Check every source once and alert on anything that dropped."""
    _setup_logging(verbose)
    config = _load(watchlist)
    store = Store()

    outcome = run_check(config, store, dry_run=dry_run, only=item, save_alert_state=False)

    table = Table(title="Prices checked", header_style="bold")
    table.add_column("Item")
    table.add_column("Shop")
    table.add_column("Price", justify="right")
    table.add_column("Target", justify="right")
    table.add_column("Stock")
    table.add_column("Via")
    for reading in outcome.readings:
        target = config.find(reading.item)
        if reading.status == "ok":
            price = format_price(reading.price, reading.currency)
        elif reading.status == "suspect":
            price = f"[yellow]{format_price(reading.price, reading.currency)} (unconfirmed)[/yellow]"
        else:
            price = f"[red]{_shorten(reading.error)}[/red]"
        stock = {True: "yes", False: "[red]no[/red]", None: "?"}[reading.in_stock]
        table.add_row(
            reading.item,
            reading.shop,
            price,
            format_price(target.target_price, target.currency) if target else "",
            stock,
            reading.method or "",
        )
    console.print(table)

    if outcome.alerts:
        console.print()
        for alert in outcome.alerts:
            console.print(
                f"[bold green]ALERT[/bold green] {alert.item.name}: "
                f"{format_price(alert.best.price, alert.item.currency)} at {alert.best.shop} "
                f"({alert.reason})\n  {alert.best.url}"
            )
    else:
        console.print("\nNo alerts — nothing crossed its threshold.")

    if outcome.failures:
        console.print(f"\n[yellow]{len(outcome.failures)} source(s) could not be read.[/yellow]")

    if _deliver(outcome.alerts, [], outcome.failures, dry_run=dry_run, no_email=no_email) and not dry_run:
        store.save_state(outcome.state)


@app.command("add")
def add_source(
    url: str = typer.Argument(..., help="Product page URL"),
    name: str = typer.Option(..., "--name", "-n", help="Item name (reuse to add a second shop)"),
    target: str = typer.Option(..., "--target", "-t", help="Alert when the price hits this"),
    currency: Optional[str] = typer.Option(None, "--currency", "-c", help="e.g. HUF, EUR, USD"),
    selector: Optional[str] = typer.Option(
        None, "--selector", "-s", help="CSS/XPath for shops without price metadata"
    ),
    watchlist: Path = WatchlistOption,
) -> None:
    """Add a product URL to the watchlist."""
    try:
        target_price = Decimal(target.replace(" ", "").replace(",", ""))
    except InvalidOperation:
        console.print(f"[red]--target must be a number, got {target!r}[/red]")
        raise typer.Exit(code=2)

    try:
        append_item(
            watchlist,
            name=name,
            url=url,
            target_price=target_price,
            currency=currency,
            selector=selector,
        )
    except (ConfigError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2)

    console.print(f"[green]Added[/green] {name} → {url}")
    console.print(f"Check it parses with: [bold]pricetracker test-url {url}[/bold]")


@app.command("list")
def list_items(watchlist: Path = WatchlistOption) -> None:
    """Show everything on the watchlist and its latest price."""
    config = _load(watchlist)
    store = Store()

    table = Table(header_style="bold")
    table.add_column("Item")
    table.add_column("Target", justify="right")
    table.add_column("Latest", justify="right")
    table.add_column("Shops")
    table.add_column("On")
    for item in config.items:
        readings = [r for r in store.for_item(item.name) if r.ok]
        latest = format_price(min(r.price for r in readings), item.currency) if readings else "—"
        table.add_row(
            item.name,
            format_price(item.target_price, item.currency),
            latest,
            ", ".join(s.display_name for s in item.sources),
            "yes" if item.enabled else "no",
        )
    console.print(table)


@app.command()
def history(
    item: Optional[str] = typer.Argument(None, help="Item name; omit for all items"),
    limit: int = typer.Option(30, "--limit", "-l", help="Most recent N entries"),
) -> None:
    """Show recorded price history."""
    store = Store()
    readings = store.for_item(item) if item else store.readings()
    if not readings:
        console.print(f"No history yet in {HISTORY_FILE}.")
        return

    table = Table(header_style="bold")
    table.add_column("When")
    table.add_column("Item")
    table.add_column("Shop")
    table.add_column("Price", justify="right")
    table.add_column("Status")
    for reading in readings[-limit:]:
        table.add_row(
            reading.checked_at.strftime("%Y-%m-%d %H:%M"),
            reading.item,
            reading.shop,
            format_price(reading.price, reading.currency),
            reading.status if reading.status == "ok" else f"[yellow]{reading.status}[/yellow]",
        )
    console.print(table)


@app.command("test-url")
def test_url(
    url: str = typer.Argument(..., help="Product page URL to probe"),
    selector: Optional[str] = typer.Option(None, "--selector", "-s", help="CSS/XPath to try"),
    save: bool = typer.Option(False, "--save", help="Write the fetched HTML to data/debug/"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Probe one URL and report what every extraction method finds.

    Use this when adding a shop: if only the selector row has a price, keep the
    selector in the watchlist; if a metadata row works, drop the selector.
    """
    _setup_logging(verbose)
    with Fetcher(user_agent=_load_user_agent()) as fetcher:
        try:
            page = fetcher.fetch(url)
        except FetchError as exc:
            console.print(f"[red]Could not fetch: {exc}[/red]")
            raise typer.Exit(code=1)

    console.print(f"Fetched [bold]{page.url}[/bold] (HTTP {page.status_code}, {len(page.html):,} bytes)\n")
    results = extract_all(page.html, selector)

    table = Table(header_style="bold")
    table.add_column("Method")
    table.add_column("Price", justify="right")
    table.add_column("Currency")
    table.add_column("Stock")
    table.add_column("Raw value")
    for method in METHODS:
        found = results.get(method)
        if found is None:
            reason = "no selector given" if method == "selector" and not selector else "nothing found"
            table.add_row(method, f"[dim]{reason}[/dim]", "", "", "")
        else:
            stock = {True: "yes", False: "no", None: "?"}[found.in_stock]
            table.add_row(
                f"[green]{method}[/green]",
                str(found.price),
                found.currency or "?",
                stock,
                found.raw[:40],
            )
    console.print(table)

    if save:
        path = Store().save_debug_html(url, page.html)
        console.print(f"\nSaved HTML to {path}")

    if not results:
        console.print(
            "\n[yellow]Nothing found.[/yellow] The page probably renders its price with "
            "JavaScript, or blocked us. Open it in a browser, copy a CSS selector for the "
            "price element, and retry with [bold]--selector[/bold]."
        )
        raise typer.Exit(code=1)

    winner = next(m for m in METHODS if m in results)
    if winner == "selector":
        console.print("\n[yellow]Only the selector worked[/yellow] — keep `selector:` for this source.")
    else:
        console.print(
            f"\n[green]Works out of the box via {winner}[/green] — no selector needed."
        )


def _load_user_agent() -> str:
    """Use the watchlist's user agent when there is one, else the default."""
    from .config import DEFAULT_USER_AGENT

    try:
        return load_watchlist().defaults.user_agent
    except (ConfigError, ValueError):
        return DEFAULT_USER_AGENT


@app.command()
def where() -> None:
    """Show where the data files live."""
    for label, path in (("watchlist", DEFAULT_WATCHLIST), ("history", HISTORY_FILE), ("alert state", STATE_FILE)):
        mark = "" if path.exists() else " [dim](not created yet)[/dim]"
        console.print(f"{label:>12}: {path.resolve()}{mark}")


# ---- events ------------------------------------------------------------

events_app = typer.Typer(add_completion=False, help="Find interesting events happening nearby.")
app.add_typer(events_app, name="events")

EventsOption = typer.Option(DEFAULT_EVENTS_CONFIG, "--config", "-c", help="Path to events.yaml")


def _load_events(path: Path):
    try:
        return load_events_config(path)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2)
    except ValueError as exc:
        console.print(f"[red]{path} is not valid:[/red]\n{exc}")
        raise typer.Exit(code=2)


def _events_table(events, title: str = "Events") -> Table:
    table = Table(title=title, header_style="bold")
    table.add_column("When")
    table.add_column("Event")
    table.add_column("Where")
    table.add_column("Price", justify="right")
    table.add_column("Matches")
    for event in events:
        table.add_row(
            format_event_when(event.starts_at, event.ends_at),
            _shorten(event.title, 46),
            _shorten(" ".join(filter(None, [event.venue, format_distance(event.distance_km)])), 30),
            format_price(event.price, event.currency) if event.price is not None else "",
            ", ".join(event.interests),
        )
    return table


@events_app.command("check")
def events_check(
    config_path: Path = EventsOption,
    dry_run: bool = typer.Option(False, "--dry-run", help="Find events but record nothing"),
    no_email: bool = typer.Option(False, "--no-email", help="Record events but skip the email"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Scan every source for events matching your interests."""
    _setup_logging(verbose)
    config = _load_events(config_path)
    store = EventStore()

    outcome = check_events(config, store, dry_run=dry_run)

    console.print(
        f"Scanned {outcome.scanned} event(s) from {len(config.active_sources)} source(s); "
        f"{len(outcome.matched)} match your interests, {len(outcome.new)} are new."
    )
    if outcome.matched:
        console.print(_events_table(outcome.matched[: config.max_per_email], "Coming up nearby"))
    if outcome.failures:
        console.print()
        for failure in outcome.failures:
            console.print(f"[red]{failure.name}[/red]: {failure.error}")

    _deliver([], outcome.new, [], dry_run=dry_run, no_email=no_email)


@events_app.command("list")
def events_list(limit: int = typer.Option(40, "--limit", "-l")) -> None:
    """Show upcoming events already found."""
    upcoming = EventStore().upcoming()
    if not upcoming:
        console.print("Nothing recorded yet. Run [bold]pricetracker events check[/bold].")
        return
    console.print(_events_table(upcoming[:limit], "Upcoming"))


@events_app.command("test-source")
def events_test_source(
    url: str = typer.Argument(..., help="Listing page or .ics feed to probe"),
    kind: str = typer.Option("schemaorg", "--type", "-t", help="schemaorg or ics"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Probe one source and show the events it yields.

    Use this before adding a source: it tells you whether a site publishes
    event data we can actually read.
    """
    _setup_logging(verbose)
    from .events.sources import SOURCE_TYPES, SourceError

    if kind not in SOURCE_TYPES:
        console.print(f"[red]Unknown type {kind!r}. Known: {', '.join(sorted(SOURCE_TYPES))}[/red]")
        raise typer.Exit(code=2)

    source = SOURCE_TYPES[kind](name="test", url=url)
    with Fetcher(user_agent=_load_user_agent()) as fetcher:
        try:
            events = source.fetch(fetcher)
        except SourceError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)

    if not events:
        console.print(
            f"[yellow]No events found via {kind}.[/yellow]\n"
            "The page may render its listings with JavaScript, or publish no structured "
            "data. Try [bold]--type ics[/bold] if the site offers a calendar feed, or look "
            "for a different listing page."
        )
        raise typer.Exit(code=1)

    console.print(f"[green]Found {len(events)} event(s) via {kind}.[/green]")
    console.print(_events_table(events[:20], url))
    located = sum(1 for e in events if e.lat is not None)
    console.print(f"{located}/{len(events)} include coordinates; the rest are geocoded by address.")


# ---- the combined daily run --------------------------------------------


@app.command()
def daily(
    watchlist: Path = WatchlistOption,
    config_path: Path = EventsOption,
    dry_run: bool = typer.Option(False, "--dry-run", help="Check everything, change nothing"),
    no_email: bool = typer.Option(False, "--no-email", help="Record, but send no email"),
    skip_events: bool = typer.Option(False, "--skip-events", help="Prices only"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Check prices and events, then send one combined email.

    This is what the daily workflow runs.
    """
    _setup_logging(verbose)
    _run_daily(
        watchlist, config_path, dry_run=dry_run, no_email=no_email, skip_events=skip_events
    )


def _run_daily(
    watchlist: Path,
    config_path: Path,
    *,
    dry_run: bool = False,
    no_email: bool = False,
    skip_events: bool = False,
) -> None:
    """One full pass: prices, then events, then a single email.

    Shared by the `daily` command and the scheduler so a timed run behaves
    identically to one you start by hand.
    """
    price_config = _load(watchlist)
    store = Store()
    outcome = run_check(price_config, store, dry_run=dry_run, save_alert_state=False)
    console.print(
        f"Checked {len(outcome.readings)} source(s): {len(outcome.successes)} ok, "
        f"{len(outcome.failures)} failed, {len(outcome.alerts)} alert(s)."
    )
    for alert in outcome.alerts:
        console.print(
            f"[bold green]ALERT[/bold green] {alert.item.name}: "
            f"{format_price(alert.best.price, alert.item.currency)} at {alert.best.shop}"
        )

    new_events = []
    if not skip_events:
        # Events are optional: no config just means the feature isn't set up yet,
        # which must not stop price alerts going out.
        if not config_path.exists():
            console.print(f"[dim]No {config_path} — skipping events.[/dim]")
        else:
            events_config = _load_events(config_path)
            event_outcome = check_events(events_config, EventStore(), dry_run=dry_run)
            new_events = event_outcome.new[: events_config.max_per_email]
            console.print(
                f"Scanned {event_outcome.scanned} event(s); {len(event_outcome.matched)} match, "
                f"{len(event_outcome.new)} new."
            )
            for failure in event_outcome.failures:
                console.print(f"[red]{failure.name}[/red]: {failure.error}")

    told = _deliver(outcome.alerts, new_events, outcome.failures, dry_run=dry_run, no_email=no_email)
    if told and not dry_run:
        # Only now is "the user has been told" true.
        store.save_state(outcome.state)


# ---- the web UI ---------------------------------------------------------


@app.command()
def ui(
    port: int = typer.Option(8420, "--port", "-p"),
    host: str = typer.Option("127.0.0.1", "--host"),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open a browser window"),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes"),
) -> None:
    """Start the web UI so you can manage everything in a browser."""
    import threading
    import webbrowser

    import uvicorn

    url = f"http://{host}:{port}"
    console.print(f"[bold green]Price Tracker[/bold green] running at [bold]{url}[/bold]")
    console.print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run("pricetracker.web.app:app", host=host, port=port, reload=reload, log_level="warning")


# ---- the scheduler ------------------------------------------------------


@app.command()
def schedule(
    at: str = typer.Option("08:00", "--at", help="Local time to run each day, HH:MM"),
    watchlist: Path = WatchlistOption,
    config_path: Path = EventsOption,
    run_now: bool = typer.Option(False, "--run-now", help="Run once immediately, then wait"),
    no_email: bool = typer.Option(False, "--no-email", help="Record, but send no email"),
) -> None:
    """Run the daily check on a timer, forever.

    This is the container's long-running process. It keeps the environment (so
    the SMTP credentials are actually present, unlike under cron) and logs each
    run to stdout where `docker logs` will show it.
    """
    import time as _time

    from .schedule import ScheduleError, parse_time, seconds_until

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        target = parse_time(at)
    except ScheduleError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2)

    log.info("scheduler started; running daily at %02d:%02d local time", target.hour, target.minute)
    pending = run_now

    while True:
        if not pending:
            wait = seconds_until(target, datetime.now())
            log.info("next run in %.1f hours", wait / 3600)
            _time.sleep(wait)

        pending = False
        started = datetime.now()
        try:
            _run_daily(watchlist, config_path, dry_run=False, no_email=no_email)
        except Exception as exc:
            # A failed run must never kill the scheduler; tomorrow may work.
            log.error("run failed: %s: %s", type(exc).__name__, exc, exc_info=True)
        log.info("run finished in %.0fs", (datetime.now() - started).total_seconds())


if __name__ == "__main__":
    app()
