"""Reading events off pages and calendar feeds."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from pricetracker.events.sources import SOURCE_TYPES, SourceError
from pricetracker.events.sources.ics import IcsSource
from pricetracker.events.sources.schemaorg import SchemaOrgSource


@pytest.fixture
def jsonld_events(fixture_html):
    return SchemaOrgSource(name="port.hu", url="https://example.hu").parse(
        fixture_html("events_jsonld.html")
    )


def test_both_source_types_are_registered():
    assert set(SOURCE_TYPES) == {"schemaorg", "ics"}


# ---- schema.org --------------------------------------------------------


def test_reads_events_from_a_graph(jsonld_events):
    titles = [e.title for e in jsonld_events]
    assert "Éjszakai Koncert a Dunán" in titles
    assert "Színház: Bánk bán" in titles


def test_full_event_details(jsonld_events):
    concert = next(e for e in jsonld_events if e.title.startswith("Éjszakai"))
    assert concert.starts_at == datetime(2026, 9, 4, 20, 0, tzinfo=concert.starts_at.tzinfo)
    assert concert.ends_at is not None
    assert concert.venue == "A38 Hajó"
    assert concert.lat == pytest.approx(47.4757)
    assert concert.lon == pytest.approx(19.0603)
    assert concert.url == "https://example.hu/koncert"
    assert concert.source == "port.hu"
    assert concert.category == "MusicEvent"


def test_cheapest_ticket_price_is_used(jsonld_events):
    """Listings often carry several offers; the entry price is the useful one."""
    concert = next(e for e in jsonld_events if e.title.startswith("Éjszakai"))
    assert concert.price == Decimal("4500")
    assert concert.currency == "HUF"


def test_address_is_flattened_for_geocoding(jsonld_events):
    concert = next(e for e in jsonld_events if e.title.startswith("Éjszakai"))
    assert "Petőfi híd budai hídfő" in concert.address
    assert "Budapest" in concert.address


def test_string_address_is_accepted(jsonld_events):
    play = next(e for e in jsonld_events if e.title.startswith("Színház"))
    assert play.venue == "Nemzeti Színház"
    assert play.address == "Bajor Gizi park 1, Budapest"


def test_online_only_events_are_not_nearby(jsonld_events):
    """A Zoom webinar is not something happening near you."""
    assert all("webinar" not in e.title.casefold() for e in jsonld_events)


def test_cancelled_events_are_dropped(jsonld_events):
    assert all("Elmaradt" not in e.title for e in jsonld_events)


def test_events_without_a_name_or_date_are_dropped(jsonld_events):
    assert all(e.title and e.starts_at for e in jsonld_events)
    assert len(jsonld_events) == 2


def test_page_with_no_events_yields_nothing(fixture_html):
    source = SchemaOrgSource(name="x", url="https://example.hu")
    assert source.parse(fixture_html("no_price.html")) == []


def test_product_markup_is_not_mistaken_for_an_event(fixture_html):
    """The price tracker's own fixtures must not register as events."""
    source = SchemaOrgSource(name="x", url="https://example.hu")
    assert source.parse(fixture_html("jsonld_product.html")) == []


def test_broken_json_ld_does_not_raise():
    source = SchemaOrgSource(name="x", url="https://example.hu")
    assert source.parse("<html><script type='application/ld+json'>{oops</script></html>") == []


# ---- ics ---------------------------------------------------------------


@pytest.fixture
def ics_events(fixture_text):
    return IcsSource(name="Trafó", url="https://example.hu/x.ics").parse(fixture_text("venue.ics"))


def test_reads_calendar_events(ics_events):
    assert [e.title for e in ics_events] == ["Kortárs tánc: Fivefold", "Jazz est"]


def test_calendar_event_details(ics_events):
    dance = ics_events[0]
    assert dance.starts_at == datetime(2026, 9, 12, 19, 0, tzinfo=timezone.utc)
    assert dance.ends_at is not None
    assert "Trafó Ház" in dance.venue
    assert dance.lat == pytest.approx(47.4842)
    assert dance.url == "https://example.hu/trafo/fivefold"
    assert dance.category == "Tánc, Színház"


def test_feed_uid_is_used_as_the_identity(ics_events):
    """A feed's own UID is stabler than anything we could derive from the text."""
    assert ics_events[0].uid == "trafo-2026-0912@example.hu"


def test_all_day_event_becomes_midnight_utc(ics_events):
    """A DATE with no time must still compare against timezone-aware datetimes."""
    jazz = ics_events[1]
    assert jazz.starts_at == datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc)
    assert jazz.starts_at.tzinfo is not None


def test_cancelled_and_undated_calendar_events_are_dropped(ics_events):
    titles = [e.title for e in ics_events]
    assert "Törölt program" not in titles
    assert "Dátum nélkül" not in titles


def test_garbage_feed_raises_a_clear_error():
    with pytest.raises(SourceError, match="valid iCalendar"):
        IcsSource(name="x", url="https://example.hu").parse("this is not a calendar")


# ---- crawling a listing to its detail pages ----------------------------


LISTING = "https://welovebudapest.com/programok/"
DETAIL_A = "https://welovebudapest.com/program/ejszakai-koncert-a-dunan/"
DETAIL_B = "https://welovebudapest.com/program/nemafilmvetites/"


class StubFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.requested = []

    def fetch(self, url):
        from pricetracker.fetch import FetchResult

        self.requested.append(url)
        page = self.pages.get(url)
        if page is None:
            from pricetracker.fetch import FetchError

            raise FetchError("HTTP 404")
        if isinstance(page, Exception):
            raise page
        return FetchResult(url=url, html=page, status_code=200)

    def close(self):
        pass


@pytest.fixture
def crawl_pages(fixture_html):
    return {
        LISTING: fixture_html("events_listing.html"),
        DETAIL_A: fixture_html("event_detail.html"),
        DETAIL_B: fixture_html("event_detail.html").replace(
            "Éjszakai Koncert a Dunán", "Némafilmvetítés"
        ),
    }


def test_listing_links_are_found_deduplicated_and_scoped(fixture_html):
    source = SchemaOrgSource(name="wlb", url=LISTING, follow="/program/")
    links = source.find_links(fixture_html("events_listing.html"), LISTING)

    assert links == [DETAIL_A, DETAIL_B], "query strings stripped, duplicates dropped"
    assert not any("masik.example" in link for link in links), "must not wander off-site"
    assert not any("/hirek/" in link for link in links), "non-matching paths ignored"


def test_crawl_reads_events_from_detail_pages(crawl_pages):
    """The listing itself has no event markup — that's the whole reason to crawl."""
    source = SchemaOrgSource(name="wlb", url=LISTING, follow="/program/")
    assert source.parse(crawl_pages[LISTING]) == []

    events = source.fetch(StubFetcher(crawl_pages))
    assert sorted(e.title for e in events) == ["Némafilmvetítés", "Éjszakai Koncert a Dunán"]


def test_crawled_events_keep_full_detail(crawl_pages):
    events = SchemaOrgSource(name="wlb", url=LISTING, follow="/program/").fetch(
        StubFetcher(crawl_pages)
    )
    concert = next(e for e in events if e.title.startswith("Éjszakai"))
    assert concert.venue == "A38 Hajó"
    assert concert.lat == pytest.approx(47.4757)
    assert concert.price == Decimal("4500")
    assert concert.url == DETAIL_A, "an event with no url of its own gets the page it came from"


def test_crawl_visits_the_listing_once_then_each_detail(crawl_pages):
    fetcher = StubFetcher(crawl_pages)
    SchemaOrgSource(name="wlb", url=LISTING, follow="/program/").fetch(fetcher)
    assert fetcher.requested == [LISTING, DETAIL_A, DETAIL_B]


def test_max_links_caps_the_crawl(crawl_pages):
    """A listing can link to hundreds of pages; each is a throttled request."""
    fetcher = StubFetcher(crawl_pages)
    SchemaOrgSource(name="wlb", url=LISTING, follow="/program/", max_links=1).fetch(fetcher)
    assert fetcher.requested == [LISTING, DETAIL_A]


def test_one_dead_detail_page_does_not_lose_the_others(crawl_pages):
    del crawl_pages[DETAIL_A]  # 404s
    events = SchemaOrgSource(name="wlb", url=LISTING, follow="/program/").fetch(
        StubFetcher(crawl_pages)
    )
    assert [e.title for e in events] == ["Némafilmvetítés"]


def test_a_listing_with_no_matching_links_says_so(crawl_pages):
    source = SchemaOrgSource(name="wlb", url=LISTING, follow="/nincs-ilyen/")
    with pytest.raises(SourceError, match="no links matching"):
        source.fetch(StubFetcher(crawl_pages))


def test_without_follow_only_the_given_page_is_read(crawl_pages):
    """Sources whose listing does carry markup must not start crawling."""
    fetcher = StubFetcher(crawl_pages)
    SchemaOrgSource(name="wlb", url=DETAIL_A).fetch(fetcher)
    assert fetcher.requested == [DETAIL_A]
