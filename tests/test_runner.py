"""End-to-end checks of the daily run, with HTTP stubbed out."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from pricetracker.config import Watchlist
from pricetracker.fetch import FetchError, FetchResult
from pricetracker.runner import run_check
from pricetracker.store import Reading, Store

NOW = datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures"

ALZA = "https://www.alza.hu/sony-a7-iv"
EMAG = "https://www.emag.hu/sony-a7-iv"


class StubFetcher:
    """Serves canned pages instead of hitting the network."""

    def __init__(self, pages: dict[str, str | Exception]):
        self.pages = pages
        self.requested: list[str] = []

    def fetch(self, url: str) -> FetchResult:
        self.requested.append(url)
        page = self.pages[url]
        if isinstance(page, Exception):
            raise page
        return FetchResult(url=url, html=page, status_code=200)

    def close(self) -> None:
        pass


def html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def watchlist(**overrides) -> Watchlist:
    data = {
        "defaults": {"currency": "HUF"},
        "items": [
            {
                "name": "Sony A7 IV",
                "target_price": 1_500_000,
                "sources": [{"url": ALZA}, {"url": EMAG}],
            }
        ],
    }
    data.update(overrides)
    return Watchlist.model_validate(data)


def test_records_a_reading_per_source(store):
    fetcher = StubFetcher({ALZA: html("jsonld_product.html"), EMAG: html("jsonld_product.html")})

    outcome = run_check(watchlist(), store, fetcher=fetcher, now=NOW)

    assert fetcher.requested == [ALZA, EMAG]
    assert len(outcome.readings) == 2
    assert all(r.ok and r.price == Decimal("1299900") for r in outcome.readings)
    assert [r.shop for r in outcome.readings] == ["alza.hu", "emag.hu"]


def test_history_is_written_and_reloads(store, tmp_path):
    fetcher = StubFetcher({ALZA: html("jsonld_product.html"), EMAG: html("jsonld_product.html")})
    run_check(watchlist(), store, fetcher=fetcher, now=NOW)

    reloaded = Store(history_path=store.history_path, state_path=store.state_path)
    readings = reloaded.readings()
    assert len(readings) == 2
    assert readings[0].price == Decimal("1299900")
    assert readings[0].currency == "HUF"
    assert readings[0].checked_at == NOW
    assert readings[0].method == "json-ld"


def test_dry_run_writes_nothing(store):
    fetcher = StubFetcher({ALZA: html("jsonld_product.html"), EMAG: html("jsonld_product.html")})

    outcome = run_check(watchlist(), store, fetcher=fetcher, now=NOW, dry_run=True)

    assert outcome.alerts, "still evaluates so --dry-run can show what would happen"
    assert not store.history_path.exists()
    assert not store.state_path.exists()


def test_alert_uses_the_cheaper_shop(store):
    fetcher = StubFetcher(
        {ALZA: html("jsonld_product.html"), EMAG: html("selector_only.html")}
    )
    config = watchlist(
        items=[
            {
                "name": "Sony A7 IV",
                "target_price": 1_500_000,
                "sources": [{"url": ALZA}, {"url": EMAG, "selector": ".ar-vegleges"}],
            }
        ]
    )

    outcome = run_check(config, store, fetcher=fetcher, now=NOW)

    assert len(outcome.alerts) == 1
    assert outcome.alerts[0].best.price == Decimal("389900")
    assert outcome.alerts[0].best.shop == "emag.hu"


def test_one_dead_shop_does_not_stop_the_others(store):
    fetcher = StubFetcher({ALZA: FetchError("HTTP 503"), EMAG: html("jsonld_product.html")})

    outcome = run_check(watchlist(), store, fetcher=fetcher, now=NOW)

    assert len(outcome.failures) == 1
    assert "HTTP 503" in outcome.failures[0].error
    assert len(outcome.successes) == 1
    assert outcome.alerts, "the reachable shop still produces its alert"


def test_unparseable_page_is_recorded_and_saved_for_debugging(store):
    fetcher = StubFetcher({ALZA: html("no_price.html"), EMAG: html("jsonld_product.html")})

    outcome = run_check(watchlist(), store, fetcher=fetcher, now=NOW)

    failure = outcome.failures[0]
    assert "no price found" in failure.error
    saved = list(store.debug_dir.glob("*.html"))
    assert len(saved) == 1, "the page we could not parse must be kept for diagnosis"


def test_currency_mismatch_is_an_error_not_a_bargain(store):
    """A 2499 EUR camera must never look like a bargain against a HUF target."""
    fetcher = StubFetcher(
        {ALZA: html("jsonld_graph.html"), EMAG: html("jsonld_product.html")}
    )

    outcome = run_check(watchlist(), store, fetcher=fetcher, now=NOW)

    mismatch = next(r for r in outcome.readings if r.url == ALZA)
    assert mismatch.status == "error"
    assert "currency mismatch" in mismatch.error
    assert all(a.best.url != ALZA for a in outcome.alerts)


def test_second_run_is_silent(store):
    pages = {ALZA: html("jsonld_product.html"), EMAG: html("jsonld_product.html")}

    first = run_check(watchlist(), store, fetcher=StubFetcher(pages), now=NOW)
    assert len(first.alerts) == 1

    tomorrow = NOW + timedelta(days=1)
    second = run_check(watchlist(), store, fetcher=StubFetcher(pages), now=tomorrow)
    assert second.alerts == [], "the same price must not be emailed again the next day"
    assert len(store.readings()) == 4, "but it is still recorded"


def test_only_filters_to_one_item(store):
    config = watchlist(
        items=[
            {"name": "Sony A7 IV", "target_price": 1_500_000, "sources": [{"url": ALZA}]},
            {"name": "Other", "target_price": 10, "sources": [{"url": EMAG}]},
        ]
    )
    fetcher = StubFetcher({ALZA: html("jsonld_product.html")})

    outcome = run_check(config, store, fetcher=fetcher, now=NOW, only="sony a7 iv")

    assert fetcher.requested == [ALZA]
    assert len(outcome.readings) == 1


def test_disabled_items_are_not_fetched(store):
    config = watchlist(
        items=[
            {
                "name": "Sony A7 IV",
                "target_price": 1_500_000,
                "enabled": False,
                "sources": [{"url": ALZA}],
            }
        ]
    )
    fetcher = StubFetcher({})

    outcome = run_check(config, store, fetcher=fetcher, now=NOW)

    assert fetcher.requested == []
    assert outcome.readings == []


def test_out_of_stock_is_recorded_but_not_alerted(store):
    """The Canon fixture is out of stock; in EUR to match, so only stock decides."""
    config = Watchlist.model_validate(
        {
            "defaults": {"currency": "EUR"},
            "items": [{"name": "Canon", "target_price": 3000, "sources": [{"url": ALZA}]}],
        }
    )
    fetcher = StubFetcher({ALZA: html("jsonld_graph.html")})

    outcome = run_check(config, store, fetcher=fetcher, now=NOW)

    assert outcome.readings[0].ok
    assert outcome.readings[0].in_stock is False
    assert outcome.alerts == [], "no point alerting on something you cannot buy"


def test_corrupt_history_line_does_not_lose_the_rest(store, capsys):
    good = Reading(checked_at=NOW, item="x", url=ALZA, shop="s", price=Decimal("10"), currency="HUF")
    store.append([good])
    with store.history_path.open("a", encoding="utf-8") as fh:
        fh.write("{not json}\n")
    store.append([good])

    reloaded = Store(history_path=store.history_path, state_path=store.state_path)
    assert len(reloaded.readings()) == 2
    assert "skipping" in capsys.readouterr().out


@pytest.mark.parametrize("payload", ["", "   \n", "{}"])
def test_unreadable_state_file_is_treated_as_empty(store, payload):
    store.state_path.parent.mkdir(parents=True, exist_ok=True)
    store.state_path.write_text(payload, encoding="utf-8")
    assert store.load_state() == {}
