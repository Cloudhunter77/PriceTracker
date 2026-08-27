"""Finding the same product at other shops.

The expensive mistake this feature can make is quoting a kit-lens bundle as if
it were the body — those differ by ~180 000 Ft. Most of what follows exists to
make sure that never scores as a confident match.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from pricetracker.discover import (
    DEFAULT_SHOPS,
    MAX_CANDIDATES_PER_SHOP,
    Candidate,
    ShopSearch,
    find_product_links,
    load_shops,
    looks_like_bundle,
    normalise_model,
    score_candidate,
    search_shop,
    search_shops,
    title_similarity,
)
from pricetracker.extract import extract_product
from pricetracker.fetch import FetchError, FetchResult

FIXTURES = Path(__file__).parent / "fixtures"

SHOP = ShopSearch(domain="emag.hu", search="https://www.emag.hu/search/{query}", product_path="/pd/")
SEARCH_URL = "https://www.emag.hu/search/A6700"
BODY = "https://www.emag.hu/sony-alpha-6700-vaz-fekete-ilce6700b-cec/pd/AAAA1/"
KIT = "https://www.emag.hu/sony-alpha-6700-16-50mm-kit-ilce6700l-cec/pd/BBBB2/"
BATTERY = "https://www.emag.hu/sony-np-fz100-akkumulator/pd/CCCC3/"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class StubFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.requested = []

    def fetch(self, url):
        self.requested.append(url)
        page = self.pages.get(url)
        if page is None:
            raise FetchError("HTTP 404")
        if isinstance(page, Exception):
            raise page
        return FetchResult(url=url, html=page, status_code=200)

    def close(self):
        pass


@pytest.fixture
def pages():
    return {
        SEARCH_URL: fixture("search_results.html"),
        BODY: fixture("product_a6700_body.html"),
        KIT: fixture("product_a6700_kit.html"),
        BATTERY: fixture("product_battery.html"),
    }


def candidate(url, fixture_name) -> Candidate:
    product = extract_product(fixture(fixture_name))
    return Candidate(url=url, shop="emag.hu", title=product.name, product=product)


# ---- model numbers -----------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("ILCE-6700", "ilce6700"),
        ("ilce 6700", "ilce6700"),
        ("ILCE6700", "ilce6700"),
        ("  A6700  ", "a6700"),
        ("ILCE-7M4B", "ilce7m4b"),
        (None, ""),
        ("", ""),
    ],
)
def test_model_numbers_normalise_past_punctuation(text, expected):
    assert normalise_model(text) == expected


# ---- the mistake that matters ------------------------------------------


def test_the_body_is_a_confident_match():
    scored = score_candidate(candidate(BODY, "product_a6700_body.html"), "ILCE-6700B", "Sony A6700 váz")
    assert scored.score == 1.0
    assert scored.suggested
    assert "part number" in scored.reason or "URL" in scored.reason


def test_a_kit_bundle_is_never_a_confident_match():
    """The 180 000 Ft mistake. The kit shares a model prefix and most of its
    title with the body, so only the bundle wording separates them."""
    scored = score_candidate(candidate(KIT, "product_a6700_kit.html"), "ILCE-6700B", "Sony A6700 váz")

    assert not scored.suggested, "a kit must never be pre-ticked as the body"
    assert "kit" in scored.reason
    assert scored.price == Decimal("729900")


def test_the_kit_is_still_offered_not_hidden():
    """Sometimes the kit is what you want — it is demoted, not suppressed."""
    scored = score_candidate(candidate(KIT, "product_a6700_kit.html"), "ILCE-6700B", "Sony A6700 váz")
    assert scored.score > 0


def test_an_accessory_does_not_match_the_camera():
    scored = score_candidate(candidate(BATTERY, "product_battery.html"), "ILCE-6700B", "Sony A6700 váz")
    assert not scored.suggested
    assert scored.score < 0.6


@pytest.mark.parametrize(
    "title", ["Sony A6700 váz + 16-50mm KIT", "Sony A6700 szett", "Sony A6700 használt", "A6700 bundle"]
)
def test_bundle_and_used_wording_is_recognised(title):
    assert looks_like_bundle(title)


def test_a_plain_body_is_not_a_bundle():
    assert not looks_like_bundle("Sony Alpha 6700 váz")


# ---- where the model number is found -----------------------------------


def test_model_found_in_the_url_slug_alone():
    """No structured data at all, but the slug carries the code."""
    bare = Candidate(url=BODY, shop="emag.hu", title=None, product=None)
    scored = score_candidate(bare, "ILCE-6700B", "Sony A6700 váz")
    assert "URL" in scored.reason


def test_model_found_in_the_part_number():
    product = extract_product(fixture("product_a6700_body.html"))
    scored = score_candidate(
        Candidate(url="https://shop.example/x/pd/1/", shop="x", title=product.name, product=product),
        "ILCE-6700B",
        "Sony A6700 váz",
    )
    assert scored.score == 1.0


def test_falls_back_to_title_similarity_without_a_model():
    scored = score_candidate(candidate(BODY, "product_a6700_body.html"), None, "Sony Alpha 6700 váz")
    assert scored.score > 0.9
    assert "title" in scored.reason


def test_title_similarity_ignores_accents_and_case():
    assert title_similarity("Sony Alpha 6700 váz", "sony alpha 6700 vaz") > 0.95


def test_a_page_with_no_price_is_not_suggested():
    bare = Candidate(url=BODY, shop="emag.hu", title="Sony Alpha 6700 váz", product=None)
    scored = score_candidate(bare, "ILCE-6700B", "Sony A6700 váz")
    assert not scored.suggested
    assert "no price" in scored.reason


# ---- reading the search page -------------------------------------------


def test_product_links_are_found_and_scoped():
    links = find_product_links(fixture("search_results.html"), SEARCH_URL, SHOP, "ILCE-6700B")

    assert BODY in links
    assert not any("masik-bolt.example" in link for link in links), "must not leave the shop"
    assert not any("/hirek/" in link for link in links), "non-product paths ignored"
    assert len(links) == len(set(links)), "query strings deduplicated"


def test_links_carrying_the_model_number_are_tried_first():
    """Filtering on the slug before fetching is what keeps this cheap."""
    links = find_product_links(fixture("search_results.html"), SEARCH_URL, SHOP, "ILCE-6700B")
    assert links[0] == BODY
    assert links.index(BODY) < links.index(BATTERY)


def test_link_count_is_capped():
    many = "".join(f'<a href="/thing-{i}/pd/{i}/">x</a>' for i in range(50))
    links = find_product_links(f"<html><body>{many}</body></html>", SEARCH_URL, SHOP, None)
    assert len(links) == MAX_CANDIDATES_PER_SHOP


def test_a_search_page_with_no_products_yields_nothing():
    html = "<html><body><p>Nincs találat</p></body></html>"
    assert find_product_links(html, SEARCH_URL, SHOP, "ILCE-6700B") == []


# ---- searching end to end ----------------------------------------------


def test_search_scores_every_candidate(pages):
    found = search_shop(SHOP, "A6700", StubFetcher(pages), model="ILCE-6700B", title="Sony A6700 váz")

    by_url = {c.url: c for c in found}
    assert by_url[BODY].suggested
    assert not by_url[KIT].suggested
    assert not by_url[BATTERY].suggested


def test_search_visits_the_results_page_then_the_products(pages):
    fetcher = StubFetcher(pages)
    search_shop(SHOP, "A6700", fetcher, model="ILCE-6700B")
    assert fetcher.requested[0] == SEARCH_URL
    assert BODY in fetcher.requested


def test_a_dead_product_page_does_not_lose_the_others(pages):
    del pages[KIT]
    found = search_shop(SHOP, "A6700", StubFetcher(pages), model="ILCE-6700B")
    assert BODY in {c.url for c in found}


def test_one_blocked_shop_does_not_stop_the_rest(pages):
    blocked = ShopSearch(domain="alza.hu", search="https://www.alza.hu/s/{query}", product_path="/d")
    fetcher = StubFetcher(pages)

    outcome = search_shops([blocked, SHOP], "A6700", fetcher, model="ILCE-6700B")

    assert [shop for shop, _ in outcome.failures] == ["alza.hu"]
    assert outcome.candidates, "the working shop still returned results"


def test_results_are_ranked_best_first(pages):
    outcome = search_shops([SHOP], "A6700", StubFetcher(pages), model="ILCE-6700B", title="Sony A6700 váz")
    scores = [c.score for c in outcome.candidates]
    assert scores == sorted(scores, reverse=True)
    assert outcome.candidates[0].url == BODY


def test_shops_already_tracked_are_skipped(pages):
    outcome = search_shops([SHOP], "A6700", StubFetcher(pages), model="ILCE-6700B", skip_domains={"emag.hu"})
    assert outcome.candidates == []


def test_disabled_shops_are_skipped(pages):
    off = ShopSearch(domain="emag.hu", search=SHOP.search, product_path="/pd/", enabled=False)
    assert search_shops([off], "A6700", StubFetcher(pages)).candidates == []


def test_query_is_url_encoded():
    assert SHOP.search_url("Sony A6700 váz").startswith("https://www.emag.hu/search/Sony%20A6700")


# ---- shop configuration ------------------------------------------------


def test_defaults_are_used_without_a_config_file(tmp_path):
    shops = load_shops(tmp_path / "absent.yaml")
    assert [s.domain for s in shops] == [s.domain for s in DEFAULT_SHOPS]


def test_only_emag_ships_marked_verified():
    """The others are guesses; test-search has to confirm them first."""
    verified = {s.domain for s in DEFAULT_SHOPS if s.verified}
    assert verified == {"emag.hu"}


def test_a_config_file_overrides_the_defaults(tmp_path):
    path = tmp_path / "shops.yaml"
    path.write_text(
        "shops:\n"
        "  - domain: fotoplus.hu\n"
        "    search: https://fotoplus.hu/kereses?q={query}\n"
        "    product_path: /termek/\n"
        "    verified: true\n",
        encoding="utf-8",
    )
    shops = load_shops(path)
    assert [s.domain for s in shops] == ["fotoplus.hu"]
    assert shops[0].verified


def test_pages_already_tracked_are_not_offered_again(pages):
    """Offering to add a shop that is already on the item is pure noise."""
    outcome = search_shops([SHOP], "A6700", StubFetcher(pages), model="ILCE-6700B", skip_urls={BODY})
    assert BODY not in {c.url for c in outcome.candidates}
    assert KIT in {c.url for c in outcome.candidates}


def test_domain_skipping_ignores_www_and_ports(pages):
    """Source.domain strips the port and www; the shop registry may not."""
    shop = ShopSearch(domain="www.emag.hu", search=SHOP.search, product_path="/pd/")
    outcome = search_shops([shop], "A6700", StubFetcher(pages), skip_domains={"emag.hu"})
    assert outcome.candidates == []
