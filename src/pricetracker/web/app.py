"""The web UI: everything the CLI does, in a browser.

Server-rendered Jinja templates with HTMX for the interactive bits, so there is
no build step and no JavaScript bundle to keep in sync with the Python.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..config import (
    DEFAULT_WATCHLIST,
    ConfigError,
    Watchlist,
    append_item,
    edit_watchlist,
    find_raw_item,
    load_watchlist,
    to_yaml_number,
)
from ..events.config import DEFAULT_EVENTS_CONFIG, EventsConfig, load_events_config, load_raw
from ..events.runner import check_events
from ..events.store import EventStore
from ..extract import METHODS, extract_all
from ..fetch import Fetcher, FetchError
from ..format import format_distance, format_event_when, format_price
from ..runner import run_check
from ..store import Store
from . import charts, gitsync
from ..config import _yaml

TEMPLATES = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES))
templates.env.filters["money"] = format_price
templates.env.filters["when"] = format_event_when
templates.env.filters["distance"] = format_distance

app = FastAPI(title="Price Tracker", docs_url=None, redoc_url=None)


# ---- background jobs ---------------------------------------------------


@dataclass
class Job:
    """A check running in the background, polled by the page that started it."""

    id: str
    label: str
    done: bool = False
    ok: bool = True
    message: str = "Running…"
    lines: list[str] = field(default_factory=list)


JOBS: dict[str, Job] = {}
_JOBS_LOCK = threading.Lock()


def _start_job(label: str, work) -> Job:
    job = Job(id=uuid.uuid4().hex[:12], label=label)
    with _JOBS_LOCK:
        JOBS[job.id] = job

    def run() -> None:
        try:
            work(job)
            job.ok = True
        except Exception as exc:  # a failed check must not take the server down
            job.ok = False
            job.message = f"{type(exc).__name__}: {exc}"
        finally:
            job.done = True

    threading.Thread(target=run, daemon=True).start()
    return job


# ---- shared context ----------------------------------------------------


def _watchlist() -> Watchlist:
    try:
        return load_watchlist(DEFAULT_WATCHLIST)
    except (ConfigError, ValueError):
        return Watchlist()


def _events_config() -> EventsConfig | None:
    try:
        return load_events_config(DEFAULT_EVENTS_CONFIG)
    except (ConfigError, ValueError):
        return None


def _base(request: Request, **extra):
    context = {
        "request": request,
        "git": gitsync.status(),
        "flash": request.query_params.get("flash"),
        "flash_error": request.query_params.get("error"),
    }
    context.update(extra)
    return context


def _redirect(path: str, flash: str | None = None, error: str | None = None) -> RedirectResponse:
    from urllib.parse import quote

    query = []
    if flash:
        query.append(f"flash={quote(flash)}")
    if error:
        query.append(f"error={quote(error)}")
    url = path + ("?" + "&".join(query) if query else "")
    # 303 so the browser follows a POST with a GET and refresh doesn't resubmit.
    return RedirectResponse(url, status_code=303)


# ---- dashboard ---------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    config = _watchlist()
    store = Store()
    event_store = EventStore()

    cards = []
    for item in config.items:
        readings = store.for_item(item.name)
        ok = [r for r in readings if r.ok]
        best = min(ok, key=lambda r: r.price) if ok else None
        latest_run = max((r.checked_at for r in readings), default=None)
        cards.append(
            {
                "item": item,
                "best": best,
                "below_target": bool(best and best.price <= item.target_price),
                "spark": charts.sparkline(readings),
                "checked_at": latest_run,
                "shops": len(item.sources),
                "failing": [r for r in readings if latest_run and r.checked_at == latest_run and r.status == "error"],
            }
        )

    return templates.TemplateResponse(
        request, "dashboard.html",
        _base(
            request,
            cards=cards,
            events=event_store.upcoming()[:8],
            has_events_config=_events_config() is not None,
        ),
    )


# ---- items -------------------------------------------------------------


@app.get("/items/new", response_class=HTMLResponse)
def new_item(request: Request):
    config = _watchlist()
    return templates.TemplateResponse(
        request, "item_new.html",
        _base(request, currencies=_currency_choices(config), default_currency=config.defaults.currency),
    )


@app.post("/items/test", response_class=HTMLResponse)
def test_item_url(request: Request, url: str = Form(...), selector: str = Form("")):
    """Probe a URL and report what each extraction method found.

    This is the reason the UI beats the CLI for adding things: you see whether a
    shop is readable before you commit it to the watchlist.
    """
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return templates.TemplateResponse(
            request, "_probe.html", {"request": request, "error": "Enter a full http(s) URL."}
        )

    config = _watchlist()
    try:
        with Fetcher(user_agent=config.defaults.user_agent) as fetcher:
            page = fetcher.fetch(url)
    except FetchError as exc:
        return templates.TemplateResponse(
            request, "_probe.html", {"request": request, "error": f"Could not fetch: {exc}"}
        )

    found = extract_all(page.html, selector.strip() or None)
    winner = next((m for m in METHODS if m in found), None)
    return templates.TemplateResponse(
        request, "_probe.html",
        {
            "request": request,
            "methods": METHODS,
            "found": found,
            "winner": winner,
            "selector": selector.strip(),
            "suggest_price": found[winner].price if winner else None,
            "suggest_currency": found[winner].currency if winner else None,
        },
    )


@app.post("/items")
def create_item(
    name: str = Form(...),
    url: str = Form(...),
    target: str = Form(...),
    currency: str = Form(""),
    selector: str = Form(""),
):
    try:
        target_price = Decimal(target.replace(" ", "").replace(",", ""))
    except InvalidOperation:
        return _redirect("/items/new", error=f"{target!r} is not a number.")

    gitsync.pull()
    try:
        append_item(
            DEFAULT_WATCHLIST,
            name=name.strip(),
            url=url.strip(),
            target_price=target_price,
            currency=currency.strip() or None,
            selector=selector.strip() or None,
        )
    except (ConfigError, ValueError) as exc:
        return _redirect("/items/new", error=str(exc))
    return _redirect(f"/items/{name.strip()}", flash=f"Added {name.strip()}.")


@app.get("/items/{name}", response_class=HTMLResponse)
def item_detail(request: Request, name: str):
    config = _watchlist()
    item = config.find(name)
    if item is None:
        return _redirect("/", error=f"No item named {name!r}.")

    store = Store()
    readings = store.for_item(item.name)
    ok = [r for r in readings if r.ok]
    return templates.TemplateResponse(
        request, "item_detail.html",
        _base(
            request,
            item=item,
            chart=charts.line_chart(readings, item.currency, item.target_price),
            readings=list(reversed(readings))[:60],
            best=min(ok, key=lambda r: r.price) if ok else None,
            lowest_ever=min((r.price for r in ok), default=None),
            currencies=_currency_choices(config),
        ),
    )


@app.post("/items/{name}/update")
def update_item(
    name: str,
    target: str = Form(...),
    currency: str = Form(""),
    cooldown_days: str = Form(""),
    drop_alert_pct: str = Form(""),
    enabled: str = Form(""),
):
    gitsync.pull()
    try:
        with edit_watchlist(DEFAULT_WATCHLIST) as raw:
            entry = find_raw_item(raw, name)
            if entry is None:
                return _redirect("/", error=f"No item named {name!r}.")
            entry["target_price"] = to_yaml_number(Decimal(target.replace(" ", "").replace(",", "")))
            _set_or_clear(entry, "currency", currency.strip().upper() or None)
            _set_or_clear(entry, "cooldown_days", int(cooldown_days) if cooldown_days.strip() else None)
            _set_or_clear(
                entry, "drop_alert_pct", float(drop_alert_pct) if drop_alert_pct.strip() else None
            )
            entry["enabled"] = enabled == "on"
    except (ConfigError, ValueError, InvalidOperation) as exc:
        return _redirect(f"/items/{name}", error=str(exc))
    return _redirect(f"/items/{name}", flash="Saved.")


@app.post("/items/{name}/sources")
def add_item_source(name: str, url: str = Form(...), selector: str = Form("")):
    gitsync.pull()
    try:
        with edit_watchlist(DEFAULT_WATCHLIST) as raw:
            entry = find_raw_item(raw, name)
            if entry is None:
                return _redirect("/", error=f"No item named {name!r}.")
            urls = {str(s.get("url")) for s in entry.get("sources", [])}
            if url.strip() in urls:
                return _redirect(f"/items/{name}", error="That shop is already tracked here.")
            source: dict[str, str] = {"url": url.strip()}
            if selector.strip():
                source["selector"] = selector.strip()
            entry.setdefault("sources", []).append(source)
    except (ConfigError, ValueError) as exc:
        return _redirect(f"/items/{name}", error=str(exc))
    return _redirect(f"/items/{name}", flash="Shop added.")


@app.post("/items/{name}/sources/remove")
def remove_item_source(name: str, url: str = Form(...)):
    gitsync.pull()
    try:
        with edit_watchlist(DEFAULT_WATCHLIST) as raw:
            entry = find_raw_item(raw, name)
            if entry is None:
                return _redirect("/", error=f"No item named {name!r}.")
            sources = entry.get("sources", [])
            if len(sources) <= 1:
                return _redirect(
                    f"/items/{name}",
                    error="An item needs at least one shop — delete the item instead.",
                )
            entry["sources"] = [s for s in sources if str(s.get("url")) != url]
    except (ConfigError, ValueError) as exc:
        return _redirect(f"/items/{name}", error=str(exc))
    return _redirect(f"/items/{name}", flash="Shop removed.")


@app.post("/items/{name}/delete")
def delete_item(name: str):
    gitsync.pull()
    try:
        with edit_watchlist(DEFAULT_WATCHLIST) as raw:
            raw["items"] = [
                i for i in raw.get("items", []) if str(i.get("name", "")).casefold() != name.casefold()
            ]
    except (ConfigError, ValueError) as exc:
        return _redirect(f"/items/{name}", error=str(exc))
    return _redirect("/", flash=f"Deleted {name}.")


# ---- events ------------------------------------------------------------


@app.get("/events", response_class=HTMLResponse)
def events_page(request: Request):
    config = _events_config()
    store = EventStore()
    upcoming = store.upcoming()

    by_interest: dict[str, int] = {}
    for event in upcoming:
        for interest in event.interests or ["Uncategorised"]:
            by_interest[interest] = by_interest.get(interest, 0) + 1

    return templates.TemplateResponse(
        request, "events.html",
        _base(request, config=config, events=upcoming, by_interest=by_interest),
    )


@app.post("/events/sources/test", response_class=HTMLResponse)
def test_event_source(request: Request, url: str = Form(...), kind: str = Form("schemaorg")):
    """Probe an event source before adding it.

    Essential here: whether a listing site publishes readable event data cannot
    be known without trying it.
    """
    from ..events.sources import SOURCE_TYPES, SourceError

    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return templates.TemplateResponse(
            request, "_event_probe.html", {"request": request, "error": "Enter a full http(s) URL."}
        )
    if kind not in SOURCE_TYPES:
        return templates.TemplateResponse(
            request, "_event_probe.html", {"request": request, "error": f"Unknown source type {kind!r}."}
        )

    source = SOURCE_TYPES[kind](name="test", url=url)
    try:
        with Fetcher(user_agent="PriceTracker/0.1") as fetcher:
            events = source.fetch(fetcher)
    except SourceError as exc:
        return templates.TemplateResponse(
            request, "_event_probe.html", {"request": request, "error": str(exc)}
        )

    return templates.TemplateResponse(
        request, "_event_probe.html", {"request": request, "events": events[:10], "total": len(events), "kind": kind}
    )


@app.post("/events/sources")
def add_event_source(name: str = Form(...), url: str = Form(...), kind: str = Form("schemaorg")):
    gitsync.pull()
    raw = load_raw(DEFAULT_EVENTS_CONFIG)
    if not raw:
        return _redirect("/events", error="No events.yaml yet — create one first.")
    raw.setdefault("sources", [])
    if any(str(s.get("name")) == name.strip() for s in raw["sources"]):
        return _redirect("/events", error=f"A source called {name.strip()!r} already exists.")
    raw["sources"].append({"name": name.strip(), "type": kind, "url": url.strip()})
    try:
        _save_events(raw)
    except ValueError as exc:
        return _redirect("/events", error=str(exc))
    return _redirect("/events", flash=f"Added {name.strip()}.")


@app.post("/events/sources/remove")
def remove_event_source(name: str = Form(...)):
    gitsync.pull()
    raw = load_raw(DEFAULT_EVENTS_CONFIG)
    raw["sources"] = [s for s in raw.get("sources", []) if str(s.get("name")) != name]
    try:
        _save_events(raw)
    except ValueError as exc:
        return _redirect("/events", error=str(exc))
    return _redirect("/events", flash=f"Removed {name}.")


@app.post("/events/settings")
def update_event_settings(
    lat: str = Form(...), lon: str = Form(...), radius_km: str = Form(...), window_days: str = Form(...)
):
    gitsync.pull()
    raw = load_raw(DEFAULT_EVENTS_CONFIG)
    if not raw:
        return _redirect("/events", error="No events.yaml yet.")
    try:
        raw.setdefault("home", {})
        raw["home"]["lat"] = float(lat)
        raw["home"]["lon"] = float(lon)
        raw["home"]["radius_km"] = float(radius_km)
        raw["window_days"] = int(window_days)
        _save_events(raw)
    except ValueError as exc:
        return _redirect("/events", error=str(exc))
    return _redirect("/events", flash="Saved.")


# ---- running checks ----------------------------------------------------


@app.post("/check", response_class=HTMLResponse)
def run_price_check(request: Request):
    def work(job: Job) -> None:
        outcome = run_check(_watchlist(), Store())
        job.lines = [
            f"{r.item} @ {r.shop}: "
            + (format_price(r.price, r.currency) if r.ok else (r.error or "failed"))
            for r in outcome.readings
        ]
        alerts = len(outcome.alerts)
        job.message = (
            f"Checked {len(outcome.readings)} shop(s) — {len(outcome.successes)} ok, "
            f"{len(outcome.failures)} failed, {alerts} alert(s)."
        )

    job = _start_job("Checking prices", work)
    return templates.TemplateResponse(request, "_job.html", {"request": request, "job": job})


@app.post("/events/check", response_class=HTMLResponse)
def run_event_check(request: Request):
    def work(job: Job) -> None:
        config = _events_config()
        if config is None:
            job.message = "No valid events.yaml to check."
            return
        outcome = check_events(config, EventStore())
        job.lines = [f"{f.name}: {f.error}" for f in outcome.failures]
        job.message = (
            f"Scanned {outcome.scanned} event(s) from {len(config.active_sources)} source(s) — "
            f"{len(outcome.matched)} match, {len(outcome.new)} new."
        )

    job = _start_job("Finding events", work)
    return templates.TemplateResponse(request, "_job.html", {"request": request, "job": job})


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_status(request: Request, job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        return HTMLResponse('<div class="job">That job is no longer available.</div>')
    return templates.TemplateResponse(request, "_job.html", {"request": request, "job": job})


# ---- settings and sync -------------------------------------------------


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    import os

    config = _watchlist()
    email_ready = bool(os.environ.get("GMAIL_USER") and os.environ.get("GMAIL_APP_PASSWORD"))
    return templates.TemplateResponse(
        request, "settings.html",
        _base(
            request,
            defaults=config.defaults,
            email_ready=email_ready,
            email_user=os.environ.get("GMAIL_USER", ""),
            watchlist_path=DEFAULT_WATCHLIST.resolve(),
            events_path=DEFAULT_EVENTS_CONFIG.resolve(),
        ),
    )


@app.post("/settings")
def update_settings(
    currency: str = Form(...),
    cooldown_days: str = Form(...),
    drop_alert_pct: str = Form(...),
    alert_on_out_of_stock: str = Form(""),
):
    gitsync.pull()
    try:
        with edit_watchlist(DEFAULT_WATCHLIST) as raw:
            defaults = raw.setdefault("defaults", {})
            defaults["currency"] = currency.strip().upper()
            defaults["cooldown_days"] = int(cooldown_days)
            defaults["drop_alert_pct"] = float(drop_alert_pct)
            defaults["alert_on_out_of_stock"] = alert_on_out_of_stock == "on"
    except (ConfigError, ValueError) as exc:
        return _redirect("/settings", error=str(exc))
    return _redirect("/settings", flash="Saved.")


@app.post("/git/push")
def push_changes():
    ok, message = gitsync.commit_and_push("chore: update watchlist from the UI")
    return _redirect("/", flash=message if ok else None, error=None if ok else message)


# ---- helpers -----------------------------------------------------------


def _set_or_clear(entry: dict, key: str, value) -> None:
    """Write a value, or drop the key so the item inherits the default again."""
    if value is None:
        entry.pop(key, None)
    else:
        entry[key] = value


def _save_events(raw: dict) -> None:
    EventsConfig.model_validate(raw)
    with DEFAULT_EVENTS_CONFIG.open("w", encoding="utf-8") as fh:
        _yaml().dump(raw, fh)


def _currency_choices(config: Watchlist) -> list[str]:
    seen = {config.defaults.currency, "HUF", "EUR", "USD", "GBP"}
    seen.update(i.currency for i in config.items if i.currency)
    return sorted(seen)


@app.get("/health")
def health():
    return {"ok": True, "now": datetime.now().isoformat()}
